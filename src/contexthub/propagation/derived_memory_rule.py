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
    """derived_from 的真语义失效判定器（可选 soundness 方向级联）。

    默认（``cheap_chat=None``）：每条边只调一次 ``chat_client``，即最朴素的
    单档 oracle（做法乙前的基线，E.8 的 P2 用的就是这个）——逐字节不变。

    传入 ``cheap_chat`` 时启用**做法乙 soundness 方向的两级级联**（§2.2 / 附录
    G.1 / D.5c）：先用便宜档判，**判 stale 即采信并短路**（省下贵档调用），
    **只有便宜档判 fresh 的边才升级贵档复核**。方向朝 soundness——因为
    false-fresh（真过期却放过）是唯一致命的错误，故要复核便宜档说"没事"的边；
    这与 §4.2 为压 precision 而设的"便宜档说 stale 才升级"级联方向相反。
    便宜档漏判（把真 stale 判成 fresh）会被贵档复核补回，故级联 false-fresh
    不高于全量贵档；便宜档过度标记（判 stale）被直接采信 → false-stale 升高，
    这是 soundness 方向省钱的代价（重算便宜时可接受，见 D.5c）。

    - 对 ``modified`` 事件动作（hop-1）；
    - 对 ``marked_stale`` 事件动作（级联到 hop-2+，需引擎放行 marked_stale 传播）。

    构造器注入 chat_client + repo：现有 evaluate(event, target) 签名不带二者，
    而判定需要 target 的完整内容（_fetch_dependents 只返回 id/dep_type），
    故规则自行用 repo 在 event 的 account_id 下开 RLS session 取 L2。
    """

    def __init__(self, chat_client, repo, cheap_chat=None, event_sink=None):
        self._chat = chat_client          # 贵档（复核档）
        self._repo = repo
        self._cheap_chat = cheap_chat     # 便宜档；None = 单档回归
        # 可选：每条边判定一次就 append 一条记录的 list。只做观测，不改判定。
        # 没有它的话，"便宜档判 stale 短路了几条 / 升档几条"只存在于进程内计数
        # 器里，落盘的 oracle_calls 又只计贵档，于是 oracle_calls=0 分不清是
        # "便宜档短路"还是"传播没走到这条边"（两者修法相反）。见 P2 复跑要求。
        self._event_sink = event_sink

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

        cheap_verdict: str | None = None
        escalated = False
        if self._cheap_chat is None:
            # 单档回归：只调贵档一次。
            is_stale, answer = await self._judge(self._chat, prompt, dependent_id)
        else:
            # soundness 方向级联：便宜档先判。判 stale 即采信、短路（省贵档）；
            # 判 fresh 才升贵档复核（复核"便宜档说没事"的边，补回其漏判）。
            cheap_stale, cheap_ans = await self._judge(self._cheap_chat, prompt, dependent_id)
            cheap_verdict = "stale" if cheap_stale else "fresh"
            if cheap_stale:
                is_stale, answer = True, cheap_ans
            else:
                escalated = True
                is_stale, answer = await self._judge(self._chat, prompt, dependent_id)

        if self._event_sink is not None:
            self._event_sink.append({
                "dependent_id": str(dependent_id),
                "change_type": change_type,
                "cheap": cheap_verdict,          # None = 单档（无便宜档）
                "escalated": escalated,          # True = 便宜档判 fresh、升了贵档
                "final": "stale" if is_stale else "fresh",
            })

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

    async def _judge(self, chat, prompt: str, dependent_id) -> tuple[bool, str]:
        """调一档判定器，返回 (is_stale, answer)。

        判定器失败时保守：不吞异常、交回引擎重试（partial failure），绝不
        把调用失败当成 fresh——否则会引入 false-fresh。
        """
        try:
            answer = await chat.complete(prompt, max_tokens=100)
        except Exception:
            logger.exception("Oracle LLM call failed for dependent_id=%s", dependent_id)
            raise
        is_stale = (answer or "").strip().upper().startswith("YES")
        return is_stale, (answer or "")

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
