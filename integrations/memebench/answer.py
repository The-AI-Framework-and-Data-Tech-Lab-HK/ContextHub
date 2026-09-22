"""Stage E: answer a question from ContextHub retrieval + LLM generation.

Held constant across ON/OFF arms: same retrieval config (include_stale=False),
same prompt template, same model/max_tokens/top_k. The only thing that differs
between arms is whether the poisoned materialized node is stale — a product of
the propagation layer, not of this step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

# Separate prompt, byte-frozen independently of _ANSWER_PROMPT: that one's hash
# is part of run identity for every arm already on disk, so it must not move.
#
# The only added information is what the system already recorded about the
# withheld notes.  The model is not asked to guess a new value — the whole point
# is that no current value exists, and the honest answer names the prior value
# and the upstream that changed.
_ANSWER_PROMPT_WITH_NOTICES = """Answer the question using only the notes below.

Notes:
{notes}

Some notes were withheld because a fact they were derived from has since
changed, so their content is no longer reliable:
{notices}

If the question can be answered from the notes above, answer it with the
shortest possible span (a name or value), no explanation.

If the answer would have come from a withheld note, do not guess a new value
and do not reuse the withheld value as if it were current. Instead answer in
exactly this form, on one line:
Uncertain — previously '<withheld value>', but <upstream that changed> changed
where <withheld value> is just the value itself, not the whole sentence the
withheld note used. For <upstream that changed>, use the upstream's name when
one is given above; otherwise name in two or three words what the upstream is
about, based on what it now says.
Example: Uncertain — previously 'Sunrise Gym', but work_location changed

Question: {question}
Answer:"""


# Slug segments that name a node's *role*, never an entity. The frozen-v3 import
# path writes `<episode_id>-node-<sha256>` (run_full100_v3_p2.import_frozen_graph),
# whose middle segment is the literal word "node"; the gold-facts ingest path
# writes "filler"/"fact"/"change" for nodes that carry no entity. Without this
# guard the frozen path yields the entity name "node" for every single notice.
_ROLE_SLUG_SEGMENTS = frozenset({"node", "fact", "filler", "change"})


def _entity_from_uri(uri: str) -> str | None:
    """The entity name carried by an ingested node's URI slug, when it has one.

    The gold-facts path inserts `.../memories/<role>-<entity>-<hex6>` (see
    ``ingest._insert_memory`` call sites), so there the entity is recoverable
    from the URI. The frozen-v3 path has no entity labels at all -- its nodes are
    raw dialogue text keyed by content hash -- so this returns ``None`` there,
    and the notice names the changed upstream by its content instead. This reads
    a name the system wrote; it never infers one.
    """
    slug = uri.rsplit("/", 1)[-1]
    parts = slug.split("-")
    if len(parts) < 3:
        return None
    entity = "-".join(parts[1:-1])
    if not entity or entity in _ROLE_SLUG_SEGMENTS:
        return None
    return entity


def format_stale_notices(notices) -> str:
    """Render withheld-note explanations for the prompt.

    Each line carries only recorded facts: the withheld note's own prior text,
    and the nearest upstream that changed (named by entity where the URI
    supplies one, else by URI).
    """
    lines = []
    for notice in notices:
        # Name the upstream only when a real name exists. Falling back to the URI
        # printed a 64-hex content hash, which tells the model nothing and reads
        # as if it were the upstream's name; the "now says" clause below carries
        # the same information in a form the model can actually use.
        upstream = _entity_from_uri(notice.source_uri) if notice.source_uri else None
        parts = [f'- withheld note previously said: "{notice.stale_content or ""}"']
        if upstream:
            parts.append(f"the upstream that changed is: {upstream}")
        if notice.source_content:
            # "that upstream" only reads correctly after the clause above named
            # one; with no name it has no antecedent.
            label = "that upstream" if upstream else "the upstream that changed"
            parts.append(f'{label} now says: "{notice.source_content}"')
        elif notice.reason:
            parts.append(f"reason recorded: {notice.reason}")
        lines.append("; ".join(parts))
    return "\n".join(lines)


def build_answer_prompt(notes_block: str, question: str, notices=()) -> str:
    """The single place either answer prompt is assembled.

    ``run_full100_v3_p2`` builds prompts inline in two code paths rather than
    calling ``answer_question``; routing all three through here keeps the ON and
    OFF arms on the same template selection rule. With no notices this returns
    the byte-frozen ``_ANSWER_PROMPT`` exactly as before.
    """
    if notices:
        return _ANSWER_PROMPT_WITH_NOTICES.format(
            notes=notes_block,
            notices=format_stale_notices(notices),
            question=question,
        )
    return _ANSWER_PROMPT.format(notes=notes_block, question=question)


@dataclass
class AnswerResult:
    answer: str
    retrieved_uris: list[str]
    retrieved_l2: list[str]
    stale_notices: list = field(default_factory=list)


async def answer_question(
    system,
    db: ScopedRepo,
    account_id: str,
    question: str,
    *,
    top_k: int = 8,
    with_stale_notices: bool = True,
) -> AnswerResult:
    """Retrieve (stale excluded) → build prompt → generate answer string.

    ``with_stale_notices`` adds *why* each withheld note was withheld to the
    prompt.  Retrieval itself is unchanged: stale nodes stay out of the served
    notes either way, so this only ever adds an explanation, never a value.

    On by default, and not conditioned on task type: a memory system that
    withholds a stale note should say so whatever it is being asked.  Pass False
    to reproduce an arm frozen before this existed, or to measure what the
    explanation itself is worth.
    """
    ctx = RequestContext(account_id=account_id, agent_id=EVAL_AGENT)
    req = SearchRequest(
        query=question,
        top_k=top_k,
        level=ContextLevel.L2,
        include_stale=False,           # constant across arms
        include_stale_notices=with_stale_notices,
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

    notes_block = "\n".join(notes) or "(no notes found)"
    prompt = build_answer_prompt(
        notes_block,
        question,
        resp.stale_notices if with_stale_notices else (),
    )
    answer = await system.answer_chat.complete(prompt, max_tokens=50)
    return AnswerResult(
        answer=(answer or "").strip(),
        retrieved_uris=uris,
        retrieved_l2=l2s,
        stale_notices=list(resp.stale_notices),
    )
