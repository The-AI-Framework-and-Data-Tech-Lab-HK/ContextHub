from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from openjiuwen.core.foundation.tool import ToolCard
from openjiuwen.core.runner import Runner

from bootstrap import bootstrap_repo_paths

bootstrap_repo_paths()

from contexthub_sdk import ContextHubClient
from jiuwenclaw.agentserver.memory.config import get_memory_mode
from jiuwenclaw.agentserver.interface import JiuWenClaw
from jiuwenclaw.config import get_config
from jiuwenclaw.extensions.sdk.base import BaseExtension
from jiuwenclaw.schema.events import AgentServerEvents
from jiuwenclaw.schema.hooks_context import MemoryHookContext

from plugin_engine import ContextHubJiuwenEngine
from toolkit import ContextHubToolToolkit

logger = logging.getLogger(__name__)


class ContextHubJiuwenExtension(BaseExtension):
    _runtime_patch_applied = False

    def __init__(self) -> None:
        super().__init__()
        self._engine: ContextHubJiuwenEngine | None = None
        self._config: dict[str, Any] = {}

    async def initialize(self, config: Any) -> None:
        self._config = self._load_config()
        client = ContextHubClient(
            url=str(self._config["contexthub_url"]).rstrip("/"),
            api_key=str(self._config["api_key"]),
            account_id=str(self._config.get("account_id", "acme")),
            agent_id=str(self._config.get("agent_id", "jiuwenclaw")),
        )
        self._engine = ContextHubJiuwenEngine(client)

        from jiuwenclaw.extensions.registry import ExtensionRegistry

        registry = ExtensionRegistry.get_instance()
        registry.register(AgentServerEvents.MEMORY_BEFORE_CHAT, self._on_memory_before_chat, priority=200)
        registry.register(AgentServerEvents.MEMORY_AFTER_CHAT, self._on_memory_after_chat, priority=200)
        self._install_runtime_tool_patch()
        logger.info("[ContextHubJiuwen] initialized enabled=%s", self._config.get("enabled", True))

    async def shutdown(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    def _load_config(self) -> dict[str, Any]:
        root = Path(getattr(self, "extension_dir", Path(__file__).resolve().parents[1]))
        cfg_path = root / "config.yaml"
        data: dict[str, Any] = {}
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

        env_cfg = {
            "enabled": os.getenv("CONTEXTHUB_ENABLED"),
            "contexthub_url": os.getenv("CONTEXTHUB_URL"),
            "api_key": os.getenv("CONTEXTHUB_API_KEY"),
            "account_id": os.getenv("CONTEXTHUB_ACCOUNT_ID"),
            "agent_id": os.getenv("CONTEXTHUB_AGENT_ID"),
            "top_k": os.getenv("CONTEXTHUB_TOP_K"),
            "auto_capture": os.getenv("CONTEXTHUB_AUTO_CAPTURE"),
        }
        for key, value in env_cfg.items():
            if value in (None, ""):
                continue
            if key in {"enabled", "auto_capture"}:
                data[key] = str(value).lower() not in {"0", "false", "no", "off"}
            elif key == "top_k":
                data[key] = int(value)
            else:
                data[key] = value
        return data

    async def _on_memory_before_chat(self, ctx: MemoryHookContext) -> None:
        if self._engine is None or not self._config.get("enabled", True):
            return
        query = ""
        extra = getattr(ctx, "extra", None)
        if isinstance(extra, dict):
            query = str(extra.get("query", "")).strip()
        if not query:
            return
        result = await self._engine.assemble(
            sessionId=str(getattr(ctx, "session_id", "")),
            messages=[{"role": "user", "content": query}],
            tokenBudget=int(self._config.get("top_k", 3)) * 512,
        )
        addition = str(result.get("systemPromptAddition", "")).strip()
        if addition:
            ctx.memory_blocks.append(addition)
            ctx.metadata["contexthub_recall"] = True

    async def _on_memory_after_chat(self, ctx: MemoryHookContext) -> None:
        if self._engine is None or not self._config.get("enabled", True):
            return
        await self._engine.afterTurn(
            sessionId=str(getattr(ctx, "session_id", "")),
            messages=[{"role": "assistant", "content": getattr(ctx, "assistant_message", "")}],
            prePromptMessageCount=0,
        )

    def _install_runtime_tool_patch(self) -> None:
        """Inject ContextHub tools into Jiuwen's per-request tool registration path."""
        if ContextHubJiuwenExtension._runtime_patch_applied:
            return

        original = JiuWenClaw._register_runtime_tools
        extension = self

        async def patched_register_runtime_tools(
            jiuwen: JiuWenClaw,
            session_id: str | None,
            channel_id: str | None,
            request_id: str | None,
            mode: str = "plan",
            request_params: dict[str, Any] | None = None,
        ) -> None:
            await original(jiuwen, session_id, channel_id, request_id, mode, request_params)

            if extension._engine is None or not extension._config.get("enabled", True):
                return
            if get_memory_mode(get_config()) != "cloud":
                return
            if jiuwen._instance is None:
                return

            for tool in list(jiuwen._instance.ability_manager.list()):
                if isinstance(tool, ToolCard) and tool.name in {
                    "ls",
                    "read",
                    "grep",
                    "stat",
                    "contexthub_store",
                    "contexthub_promote",
                    "contexthub_skill_publish",
                }:
                    jiuwen._instance.ability_manager.remove(tool.name)

            toolkit = ContextHubToolToolkit(
                engine=extension._engine,
                session_id=session_id or "default",
                request_id=request_id or "request",
            )
            for tool in toolkit.get_tools():
                if not Runner.resource_mgr.get_tool(tool.card.id):
                    Runner.resource_mgr.add_tool(tool)
                jiuwen._instance.ability_manager.add(tool.card)

        JiuWenClaw._register_runtime_tools = patched_register_runtime_tools
        ContextHubJiuwenExtension._runtime_patch_applied = True
