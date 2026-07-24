"""Stage F: judge answers against MEME gold, with trivial-pass filtering.

MEME's trivial-pass rule: a Cascade case counts correct iff the model answers
BOTH the before-question (pre-change value) AND the after/cascade question
(post-change value) correctly. This guards against models that always emit the
new value regardless of timing.

Matching is layered: normalized containment first (robust for entity answers
like "James Lee"); an optional LLM judge is available for ambiguous phrase
answers but is not required for the entity-style Cascade golds.

MEME parity: MEME §4.1 scores answers with a GPT-4o judge, not string match.
`judge_case_async(..., chat=...)` uses an LLM judge to match that protocol; when
`chat is None` it falls back to the containment `matches()` above, so existing
callers and tests that pass no chat client behave exactly as before.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

from contexthub.llm.chat_client import BaseChatClient

_PUNCT = str.maketrans("", "", string.punctuation)
_PAREN = re.compile(r"\([^)]*\)")
_LEAD = re.compile(r"^(with|a|an|the)\s+")


def normalize(text: str) -> str:
    text = (text or "").strip().casefold()
    text = _PAREN.sub("", text)          # drop qualifiers like "(weekly)", "(1 week)"
    text = text.translate(_PUNCT)
    text = re.sub(r"\s+", " ", text).strip()
    text = _LEAD.sub("", text)           # drop leading "with/a/an/the" (e.g. "with partner")
    return text.strip()


def matches(answer: str, gold: str) -> bool:
    """Normalized containment either direction (gold in answer or vice versa)."""
    a, g = normalize(answer), normalize(gold)
    if not g:
        return False
    return g in a or a == g


@dataclass
class CaseVerdict:
    before_ok: bool
    after_ok: bool
    trivial_pass: bool          # before_ok AND after_ok
    raw_after_ok: bool          # after answer correct regardless of before
    before_answer: str
    after_answer: str


def judge_case(
    before_answer: str,
    before_gold: str | None,
    after_answer: str,
    after_gold: str,
) -> CaseVerdict:
    before_ok = matches(before_answer, before_gold) if before_gold else True
    after_ok = matches(after_answer, after_gold)
    return CaseVerdict(
        before_ok=before_ok,
        after_ok=after_ok,
        trivial_pass=before_ok and after_ok,
        raw_after_ok=after_ok,
        before_answer=before_answer,
        after_answer=after_answer,
    )


# --- LLM judge (MEME §4.1 parity) ---------------------------------------------

_JUDGE_PROMPT = (
    "You are grading whether an AI assistant's answer to a question is correct, "
    "given the single reference answer.\n"
    "The answer is CORRECT if it conveys the reference value, even with extra "
    "words, rephrasing, or surrounding explanation. It is INCORRECT if it gives a "
    "different value, omits the reference value, or also asserts a conflicting "
    "value.\n\n"
    "Question: {question}\n"
    "Reference answer: {gold}\n"
    "Assistant answer: {answer}\n\n"
    "Reply with exactly one word: CORRECT or INCORRECT."
)


async def _llm_match(
    chat: BaseChatClient, question: str, answer: str, gold: str
) -> bool:
    """One LLM call grading answer vs gold. Falls back to containment on any
    unexpected reply so a flaky judge never silently zeros a case."""
    if not (gold or "").strip():
        return False
    out = await chat.complete(
        _JUDGE_PROMPT.format(question=question or "", gold=gold, answer=answer or ""),
        max_tokens=4,
    )
    verdict = (out or "").strip().casefold()
    if verdict.startswith("correct"):
        return True
    if verdict.startswith("incorrect"):
        return False
    return matches(answer, gold)  # unparseable → conservative containment


async def judge_case_async(
    before_answer: str,
    before_gold: str | None,
    after_answer: str,
    after_gold: str,
    *,
    before_question: str = "",
    after_question: str = "",
    chat: BaseChatClient | None = None,
) -> CaseVerdict:
    """LLM-judged variant (MEME parity). With chat=None this is identical to the
    synchronous judge_case (containment), so it is a safe drop-in."""
    if chat is None:
        return judge_case(before_answer, before_gold, after_answer, after_gold)
    before_ok = (
        await _llm_match(chat, before_question, before_answer, before_gold)
        if before_gold else True
    )
    after_ok = await _llm_match(chat, after_question, after_answer, after_gold)
    return CaseVerdict(
        before_ok=before_ok,
        after_ok=after_ok,
        trivial_pass=before_ok and after_ok,
        raw_after_ok=after_ok,
        before_answer=before_answer,
        after_answer=after_answer,
    )
