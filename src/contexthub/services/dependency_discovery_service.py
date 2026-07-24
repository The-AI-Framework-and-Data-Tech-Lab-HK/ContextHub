"""DependencyDiscoveryService: discover semantic ``derived_from`` edges at write time.

ContextHub records ``derived_from`` dependencies from two kinds of source:

- deterministic provenance — e.g. ``MemoryService.promote`` knows the promoted
  memory was copied from a specific source, so it writes the edge directly.
- semantic derivation — a newly written fact is *derived from* an existing fact
  even though no code computed it (e.g. "the report recipient is Hyunwoo Nam,
  assigned by team lead ..." is derived from "the team lead is ..."). No signature
  or hash reveals this; only reading the two facts does.

This service covers the second case. Given a new fact and a set of candidate
existing facts, it asks an LLM which candidate(s) the new fact is derived from
(possibly none). It builds the graph incrementally at write time — each new fact
is judged against the facts already stored — so the dependency edges needed for
failure propagation exist without anyone hand-supplying a dependency graph.

The service only *decides* the edges; callers persist them. This keeps it usable
both from the production write path (``MemoryService.add_memory``) and from
evaluation harnesses that manage their own inserts.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import uuid

from contexthub.llm.chat_client import BaseChatClient

logger = logging.getLogger(__name__)


@dataclass
class CandidateFact:
    """An already-stored fact the new fact might be derived from."""

    id: uuid.UUID
    text: str


_DISCOVERY_PROMPT = """A new fact is being stored. Decide which of the existing \
facts it is DERIVED FROM — i.e. its value was computed or determined from that \
fact, so that if the existing fact changed, the new fact could become outdated.

Only report a dependency when the new fact's value genuinely depends on the \
existing fact. Do NOT link facts that are merely about a related topic, restate \
the same thing, or happen to share a name. Most facts are derived from nothing.

New fact:
{new_fact}

Existing facts (numbered):
{candidates}

On the first line, list the numbers of the facts the new fact is derived from, \
comma-separated (e.g. "1" or "1,3"). If it is derived from none of them, write \
exactly "NONE". Then optionally one short line of reasoning."""


# Prompt used when a syntactic check flags the NEW fact as looking like a
# conditional rule ("if X changes, Y will be ..."). The syntactic check only
# *routes* to this prompt; the LLM still decides. This guards recall: a fact that
# happens to contain "if" but whose present-tense value really is derived (e.g.
# "rent is $2000, which would rise if I move") can still be linked.
_DISCOVERY_PROMPT_CONDITIONAL = """A new fact is being stored. Decide which of \
the existing facts its CURRENT asserted value is DERIVED FROM — i.e. that value \
was computed from the existing fact, so if the existing fact changed, the new \
fact's stated value would become outdated.

This new fact looks like it may state a RULE, PLAN, or CONDITIONAL about the \
upstream (e.g. "if X changes, Y will be ..."). If it only describes what WOULD \
happen when the upstream changes — rather than asserting a present value that \
was computed from the upstream — then it is NOT derived from anything: it stays \
valid (indeed becomes the correct answer) after the change, so answer NONE. \
Only link it if it also asserts a current value genuinely computed from an \
existing fact.

New fact:
{new_fact}

Existing facts (numbered):
{candidates}

On the first line, list the numbers of the facts the new fact is derived from, \
comma-separated (e.g. "1" or "1,3"). If it is derived from none of them, write \
exactly "NONE". Then optionally one short line of reasoning."""


# Conditional-rule / predeclaration syntactic signature: an "if ... will/would/
# becomes/changes" construction. Deliberately narrow — it only picks the prompt
# variant, never suppresses an edge on its own.
_COND_MODAL = r"(will|would|becomes?|changes?|switch(?:es)?)"
# A predeclaration/rule fact has its conditional in the MAIN clause: it *starts*
# with "if ..." ("If the team lead changes, the recipient will be James Lee").
# This must NOT match a current-value fact that merely appends a change-hint tail
# ("My appointment is dermatologist — ...; if my health changes, this would
# change"): that fact asserts a present derived value and DOES need an edge.
# Matching only a leading "if ... <modal>" cleanly separates the two (verified
# on MEME: 213/213 pre matched, 0/430 cur+root matched).
_CONDITIONAL_RE = re.compile(
    rf"^\s*if\b.{{0,80}}?\b{_COND_MODAL}\b",
    re.IGNORECASE | re.DOTALL,
)


class DependencyDiscoveryService:
    """Discover semantic ``derived_from`` sources for a new fact via an LLM.

    Naive one-shot judgement: one LLM call per new fact, over all candidates at
    once. Returns the candidate ids the new fact is derived from (0, 1, or many).
    Callers decide how the candidate set is chosen (e.g. retrieval top-k) and
    persist the resulting edges.

    Tier-0 syntactic pre-check on the NEW fact (zero LLM cost):

    - ``conditional_aware`` (conservative): if the new fact reads like a
      conditional rule ("if X changes, Y will be ..."), route it to a prompt
      variant that tells the LLM such rules are NOT derived unless they also
      assert a present computed value. The LLM still decides, so recall is
      protected — a fact like "rent is $2000, would rise if I move" can still
      link.
    - ``conditional_hard`` (aggressive): if the new fact looks conditional,
      give it NO derived_from edge at all — skip the LLM entirely. This bets
      that any conditional-rule fact should be edge-free (true for MEME's
      predeclaration nodes, which are designed to have no in-edge). The risk is
      recall: a conditional fact that *also* asserts a present derived value
      (e.g. "rent is $2000, would rise if I move") is wrongly excluded. Such
      dual facts are essentially absent in MEME, so this is safe *for that
      benchmark* — do not treat hard mode as a general-purpose capability.

    ``conditional_hard`` implies conditional routing and takes precedence.
    Both default False, keeping the naive baseline byte-for-byte unchanged.
    """

    def __init__(
        self,
        chat_client: BaseChatClient,
        *,
        conditional_aware: bool = False,
        conditional_hard: bool = False,
    ):
        self._chat = chat_client
        self._conditional_aware = conditional_aware
        self._conditional_hard = conditional_hard

    @staticmethod
    def _looks_conditional(text: str) -> bool:
        return bool(_CONDITIONAL_RE.search(text or ""))

    async def discover_sources(
        self, new_fact: str, candidates: list[CandidateFact]
    ) -> list[uuid.UUID]:
        """Return the subset of ``candidates`` that ``new_fact`` is derived from."""
        if not new_fact or not candidates:
            return []

        looks_conditional = (
            (self._conditional_hard or self._conditional_aware)
            and self._looks_conditional(new_fact)
        )
        # Aggressive: a conditional-looking fact gets no in-edge; skip the LLM.
        if self._conditional_hard and looks_conditional:
            return []

        numbered = "\n".join(f"{i + 1}. {c.text}" for i, c in enumerate(candidates))
        if looks_conditional:
            template = _DISCOVERY_PROMPT_CONDITIONAL
        else:
            template = _DISCOVERY_PROMPT
        prompt = template.format(new_fact=new_fact, candidates=numbered)

        answer = await self._chat.complete(prompt, max_tokens=100)
        return self._parse(answer, candidates)

    @staticmethod
    def _parse(answer: str, candidates: list[CandidateFact]) -> list[uuid.UUID]:
        """Parse the first line into candidate ids; tolerant of formatting noise."""
        first_line = (answer or "").strip().splitlines()[0] if (answer or "").strip() else ""
        if not first_line or "NONE" in first_line.upper():
            return []
        picked: list[uuid.UUID] = []
        seen: set[int] = set()
        for tok in re.findall(r"\d+", first_line):
            idx = int(tok) - 1
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                picked.append(candidates[idx].id)
        return picked
