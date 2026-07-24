"""Stage E: answer a question from ContextHub retrieval + LLM generation.

Held constant across ON/OFF arms: same retrieval config (include_stale=False),
same prompt template, same model/max_tokens/top_k. The only thing that differs
between arms is whether the poisoned materialized node is stale — a product of
the propagation layer, not of this step.
"""

from __future__ import annotations

from dataclasses import dataclass

from contexthub.db.repository import ScopedRepo
from contexthub.models.context import ContextLevel, ContextType, Scope
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest

from integrations.memebench.ingest import EVAL_AGENT

_ANSWER_PROMPT = """Answer the question using only the notes below.
Answer with the shortest possible span (a name or value), no explanation.

Notes:
{notes}

Question: {question}
Answer:"""


@dataclass
class AnswerResult:
    answer: str
    retrieved_uris: list[str]
    retrieved_l2: list[str]


async def answer_question(
    system,
    db: ScopedRepo,
    account_id: str,
    question: str,
    *,
    top_k: int = 8,
) -> AnswerResult:
    """Retrieve (stale excluded) → build prompt → generate answer string."""
    ctx = RequestContext(account_id=account_id, agent_id=EVAL_AGENT)
    req = SearchRequest(
        query=question,
        top_k=top_k,
        level=ContextLevel.L2,
        include_stale=False,           # constant across arms
        context_type=[ContextType.MEMORY],
        scope=[Scope.AGENT],
    )
    resp = await system.retrieval.search(db, req, ctx)

    notes = []
    uris = []
    l2s = []
    for r in resp.results:
        text = r.l2_content or r.l1_content or r.l0_content or ""
        if text:
            notes.append(f"- {text}")
            uris.append(r.uri)
            l2s.append(text)

    prompt = _ANSWER_PROMPT.format(notes="\n".join(notes) or "(no notes found)", question=question)
    answer = await system.answer_chat.complete(prompt, max_tokens=50)
    return AnswerResult(answer=(answer or "").strip(), retrieved_uris=uris, retrieved_l2=l2s)
