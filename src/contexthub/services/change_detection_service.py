"""ChangeDetectionService: zero-oracle change detection at write time.

When a new fact arrives, decide which already-stored facts it makes OUTDATED —
i.e. the new fact reports a changed state of the SAME thing, so an existing
fact's asserted value is no longer current. Callers fire a ``modified``
change_event on each superseded node; propagation then marks its
``derived_from`` dependents stale (mechanism unchanged).

This is the counterpart to ``DependencyDiscoveryService``: discovery decides
which edges a new fact is *derived from* (build-time graph), detection decides
which stored facts a new fact *supersedes* (change trigger). The service only
*decides* the superseded ids; callers persist the change events. This keeps it
usable both from the production write path (``MemoryService.add_memory``) and
from evaluation harnesses that manage their own inserts.
"""

from __future__ import annotations

import uuid

from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)

_SUPERSEDE_PROMPT = """A new fact was just stated in conversation. Decide which \
of the existing stored facts it makes OUTDATED — i.e. the new fact reports a \
changed state of the SAME thing, so the existing fact's asserted value is no \
longer current.

Report a fact only if the new fact directly replaces its value (same subject / \
attribute, new value). Do NOT report facts about a different thing, conditional \
rules about what WOULD happen, or facts that merely mention a related topic.

New fact:
{new_fact}

Existing facts (numbered):
{candidates}

On the first line, list the numbers of the existing facts the new fact makes \
outdated, comma-separated (e.g. "1" or "1,3"). If it makes none of them \
outdated, write exactly "NONE". Then optionally one short line of reasoning."""


class ChangeDetectionService:
    """Decide which stored facts a new fact supersedes, via one LLM call.

    One LLM call over the full candidate set (no entity labels used). Returns the
    candidate ids the new fact supersedes (0, 1, or many). Reuses
    ``DependencyDiscoveryService._parse`` for tolerant first-line parsing.
    """

    def __init__(self, chat_client: BaseChatClient):
        self._chat = chat_client

    async def detect_superseded(
        self, new_fact: str, candidates: list[CandidateFact]
    ) -> list[uuid.UUID]:
        """Return the candidate ids ``new_fact`` makes outdated (possibly none)."""
        if not new_fact or not candidates:
            return []
        numbered = "\n".join(f"{i + 1}. {c.text}" for i, c in enumerate(candidates))
        prompt = _SUPERSEDE_PROMPT.format(new_fact=new_fact, candidates=numbered)
        answer = await self._chat.complete(prompt, max_tokens=100)
        return DependencyDiscoveryService._parse(answer, candidates)
