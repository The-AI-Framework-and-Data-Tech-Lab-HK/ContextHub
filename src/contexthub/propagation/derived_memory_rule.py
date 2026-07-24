from __future__ import annotations

import json
import logging
from typing import Any

from contexthub.propagation.base import PropagationAction, PropagationRule

logger = logging.getLogger(__name__)


class DerivedMemoryRule(PropagationRule):
    """处理 dep_type='derived_from' 的使用依赖。

    从某个共享 memory 派生的私有 memory。
    当源 memory 被修改时，通知派生方。MVP 中仅日志。
    """

    async def evaluate(self, event, target) -> PropagationAction:
        change_type = event.get("change_type", "")
        if change_type == "modified":
            return PropagationAction(
                action="notify",
                reason="源 memory 已修改，派生方可能需要更新",
            )
        return PropagationAction(
            action="no_action",
            reason=f"源 memory 变更类型 {change_type} 不需要传播",
        )


_ORACLE_PROMPT = """You judge whether a stored derived fact has become stale.

An upstream fact changed:
{change}

A derived note (which was computed from the upstream fact) currently states:
{derived}

Question: does the upstream change make the derived note incorrect or outdated?
Answer with exactly one word on the first line: YES or NO.
Then a short one-line reason."""


class DerivedMemoryOracleRule(PropagationRule):
    """derived_from 的真语义失效判定器（朴素 oracle）。

    对派生依赖边上的下游节点各调一次 LLM，判断上游变更是否使其过期。
    这是最朴素的一版（每条边都上 LLM），刻意作为代价优化器的基线——
    未来在同一 evaluate 接口内加"先规则、再便宜 LLM、最后贵 LLM + 短路"
    即长成分级判定器，引擎与评测层无需改动。

    - 对 ``modified`` 事件动作（hop-1）；
    - 对 ``marked_stale`` 事件动作（级联到 hop-2+，需引擎放行 marked_stale 传播）。

    构造器注入 chat_client + repo：现有 evaluate(event, target) 签名不带二者，
    而判定需要 target 的完整内容（_fetch_dependents 只返回 id/dep_type），
    故规则自行用 repo 在 event 的 account_id 下开 RLS session 取 L2。
    """

    def __init__(self, chat_client, repo):
        self._chat = chat_client
        self._repo = repo

    async def evaluate(self, event: dict[str, Any], target: dict[str, Any]) -> PropagationAction:
        change_type = event.get("change_type", "")
        if change_type not in ("modified", "marked_stale"):
            return PropagationAction(
                action="no_action",
                reason=f"derived_from oracle 不响应变更类型 {change_type}",
            )

        account_id = event["account_id"]
        dependent_id = target["dependent_id"]

        # 取下游派生节点的完整内容（L2 优先）作为待判定对象。
        derived_text = await self._fetch_content(account_id, dependent_id)
        if not derived_text:
            return PropagationAction(
                action="no_action",
                reason="派生节点无内容可判定，跳过",
            )

        change_desc = await self._describe_change(event, account_id)

        prompt = _ORACLE_PROMPT.format(change=change_desc, derived=derived_text)
        try:
            answer = await self._chat.complete(prompt, max_tokens=100)
        except Exception:
            logger.exception("Oracle LLM call failed for dependent_id=%s", dependent_id)
            # 判定器失败时保守：不误标 fresh，交回引擎重试（partial failure）。
            raise

        verdict = (answer or "").strip().upper()
        is_stale = verdict.startswith("YES")

        if is_stale:
            return PropagationAction(
                action="mark_stale",
                # reason 成为该节点 marked_stale 事件的 diff_summary，作为下一 hop 的
                # root-change 上下文。保持 root 变更本身（不嵌套 enriched 文本），
                # 下一 hop 会把"直接上游=本节点已过期"再拼进去（见 _describe_change）。
                reason=self._describe_root_change(event),
            )
        return PropagationAction(
            action="no_action",
            reason=f"oracle 判定未过期: {answer.strip()[:120] if answer else ''}",
        )

    async def _fetch_content(self, account_id: str, context_id) -> str | None:
        async with self._repo.session(account_id) as db:
            row = await db.fetchrow(
                "SELECT l2_content, l1_content, l0_content FROM contexts WHERE id = $1",
                context_id,
            )
        if row is None:
            return None
        return row["l2_content"] or row["l1_content"] or row["l0_content"]

    async def _describe_change(self, event: dict[str, Any], account_id: str) -> str:
        """构造"上游变了什么"的描述，供 oracle prompt 使用。

        modified（hop-1）: root 变更本身（diff_summary / metadata before->after）。
        marked_stale（级联 hop-2+）: 关键——下游节点的直接上游是 *这个刚被标 stale 的
        节点*（event.context_id），而非原始 root。若只透传 root 的 diff_summary，
        hop-2 的 oracle 会拿"root 变了"去判一个与 root 字面无关的派生物（如
        health_condition 变 → 判 fitness_facility），极易误判 NO。故级联时把
        *直接上游节点的内容 + 它已因上游变更而过期* 一起告诉 oracle。
        """
        base = self._describe_root_change(event)

        if event.get("change_type") == "marked_stale":
            # event.context_id 是刚被判过期的直接上游节点。
            upstream_text = await self._fetch_content(account_id, event["context_id"])
            if upstream_text:
                return (
                    f"A note this fact was derived from has just become outdated. "
                    f"That upstream note said: \"{upstream_text}\". "
                    f"It is outdated because: {base}"
                )
        return base

    def _describe_root_change(self, event: dict[str, Any]) -> str:
        diff = event.get("diff_summary")
        if diff:
            return str(diff)

        metadata = event.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = None
        if isinstance(metadata, dict):
            before = metadata.get("before")
            after = metadata.get("after")
            if before is not None or after is not None:
                return f"An upstream fact changed from '{before}' to '{after}'."
            return json.dumps(metadata, ensure_ascii=False)

        return "An upstream fact this note depends on has changed."
