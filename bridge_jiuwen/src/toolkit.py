from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from plugin_engine import ContextHubJiuwenEngine


class ContextHubToolToolkit:
    """Expose ContextHub operations as regular Jiuwen tools."""

    def __init__(self, engine: ContextHubJiuwenEngine, session_id: str, request_id: str) -> None:
        self._engine = engine
        self._session_id = session_id
        self._request_id = request_id

    async def ls(self, path: str) -> str:
        return await self._engine.dispatch_tool("ls", {"path": path})

    async def read(self, uri: str, level: str | None = None, version: int | None = None) -> str:
        args: dict[str, Any] = {"uri": uri}
        if level:
            args["level"] = level
        if version is not None:
            args["version"] = version
        return await self._engine.dispatch_tool("read", args)

    async def grep(
        self,
        query: str,
        scope: list[str] | None = None,
        context_type: list[str] | None = None,
        top_k: int | None = None,
    ) -> str:
        args: dict[str, Any] = {"query": query}
        if scope:
            args["scope"] = scope
        if context_type:
            args["context_type"] = context_type
        if top_k is not None:
            args["top_k"] = top_k
        return await self._engine.dispatch_tool("grep", args)

    async def stat(self, uri: str) -> str:
        return await self._engine.dispatch_tool("stat", {"uri": uri})

    async def contexthub_store(self, content: str, tags: list[str] | None = None) -> str:
        args: dict[str, Any] = {"content": content}
        if tags:
            args["tags"] = tags
        return await self._engine.dispatch_tool("contexthub_store", args)

    async def contexthub_promote(self, uri: str, target_team: str) -> str:
        return await self._engine.dispatch_tool(
            "contexthub_promote",
            {"uri": uri, "target_team": target_team},
        )

    async def contexthub_skill_publish(
        self,
        skill_uri: str,
        content: str,
        changelog: str | None = None,
        is_breaking: bool | None = None,
    ) -> str:
        args: dict[str, Any] = {"skill_uri": skill_uri, "content": content}
        if changelog:
            args["changelog"] = changelog
        if is_breaking is not None:
            args["is_breaking"] = is_breaking
        return await self._engine.dispatch_tool("contexthub_skill_publish", args)

    def get_tools(self) -> list[Tool]:
        """Build per-request tool cards so Jiuwen can choose ContextHub tools itself."""

        def make_tool(name: str, description: str, input_params: dict[str, Any], func: Any) -> Tool:
            card = ToolCard(
                id=f"{name}_{self._session_id}_{self._request_id}",
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="ls",
                description="列出 ContextHub 某个路径下的子项。",
                input_params={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "要列出的 ContextHub 路径。"}},
                    "required": ["path"],
                },
                func=self.ls,
            ),
            make_tool(
                name="read",
                description="读取某个 ContextHub URI 的内容。",
                input_params={
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "description": "要读取的 ContextHub URI。"},
                        "level": {"type": "string", "enum": ["L0", "L1", "L2"], "description": "读取层级。"},
                        "version": {"type": "integer", "description": "技能版本号。"},
                    },
                    "required": ["uri"],
                },
                func=self.read,
            ),
            make_tool(
                name="grep",
                description="在 ContextHub 中搜索相关记忆或技能。",
                input_params={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索词。"},
                        "scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "搜索范围，例如 team、agent、user。",
                        },
                        "context_type": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "上下文类型，例如 memory、skill。",
                        },
                        "top_k": {"type": "integer", "description": "最大返回结果数。"},
                    },
                    "required": ["query"],
                },
                func=self.grep,
            ),
            make_tool(
                name="stat",
                description="查看某个 ContextHub URI 的元数据。",
                input_params={
                    "type": "object",
                    "properties": {"uri": {"type": "string", "description": "要查看的 ContextHub URI。"}},
                    "required": ["uri"],
                },
                func=self.stat,
            ),
            make_tool(
                name="contexthub_store",
                description="把用户要求记住的信息保存到 ContextHub 私有记忆。",
                input_params={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "要保存的记忆内容。"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选标签。",
                        },
                    },
                    "required": ["content"],
                },
                func=self.contexthub_store,
            ),
            make_tool(
                name="contexthub_promote",
                description="把私有记忆晋升到团队共享空间。",
                input_params={
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "description": "要晋升的私有记忆 URI。"},
                        "target_team": {"type": "string", "description": "目标团队名，例如 engineering。"},
                    },
                    "required": ["uri", "target_team"],
                },
                func=self.contexthub_promote,
            ),
            make_tool(
                name="contexthub_skill_publish",
                description="发布 ContextHub 技能的新版本。",
                input_params={
                    "type": "object",
                    "properties": {
                        "skill_uri": {"type": "string", "description": "技能 URI。"},
                        "content": {"type": "string", "description": "技能内容。"},
                        "changelog": {"type": "string", "description": "变更说明。"},
                        "is_breaking": {"type": "boolean", "description": "是否为 breaking change。"},
                    },
                    "required": ["skill_uri", "content"],
                },
                func=self.contexthub_skill_publish,
            ),
        ]
