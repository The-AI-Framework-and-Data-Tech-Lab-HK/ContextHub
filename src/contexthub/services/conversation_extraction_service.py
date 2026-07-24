"""Write-time fact extraction: turn a raw conversation session into memory facts.

ContextHub's ``add_memory`` stores whatever text it is given verbatim; it does
not distil a dialogue into atomic facts. This service adds that missing step so
a live system (or a benchmark) can feed raw multi-turn conversations instead of
pre-extracted gold facts.

Design (mode B — no schema hint, faithful to MEME's baseline protocol §3):
the extractor is given ONLY the conversation transcript. It is NOT told which
entities to look for and never sees gold facts. It freely decides which facts to
record, one self-contained natural-language statement per fact, preserving any
conditional ("if X changes, Y will be Z") phrasing verbatim so downstream
dependency discovery can still see it.
"""

from __future__ import annotations

from dataclasses import dataclass

from contexthub.llm.chat_client import BaseChatClient

_EXTRACT_PROMPT = (
    "You are the memory module of a personal assistant. Read the following "
    "conversation session and write down every fact the user shared that could "
    "be useful in future sessions.\n"
    "Rules:\n"
    "- One fact per line, as a short self-contained statement.\n"
    "- When the user states a value AND what it depends on (or why it has that "
    "value) in the same breath (e.g. \"my monitoring dashboard is at <url>, which "
    "is determined by the deploy target\"), keep the value and its stated "
    "dependency together in ONE line. Do not split the value into one fact and "
    "its reason/dependency into another.\n"
    "- Preserve conditional statements exactly as said (e.g. \"If the team lead "
    "changes, the report recipient will be James Lee\"). Do not resolve or drop "
    "them. A standalone conditional about a FUTURE value still gets its own line, "
    "separate from the current-value fact above.\n"
    "- Record only what the user actually stated. Do not invent or infer values "
    "that were not said.\n"
    "- No numbering, no bullets, no commentary. Just one fact per line.\n\n"
    "Conversation:\n{conversation}\n\n"
    "Facts:"
)


@dataclass
class ExtractedFact:
    text: str


class ConversationExtractionService:
    """LLM extraction of memory facts from a raw conversation transcript."""

    def __init__(self, chat_client: BaseChatClient, *, max_tokens: int = 1200):
        self._chat = chat_client
        self._max_tokens = max_tokens

    async def extract(self, conversation: str) -> list[ExtractedFact]:
        """Extract facts from one conversation. Returns [] on empty input."""
        if not (conversation or "").strip():
            return []
        out = await self._chat.complete(
            _EXTRACT_PROMPT.format(conversation=conversation),
            max_tokens=self._max_tokens,
        )
        return self._parse(out)

    @staticmethod
    def _parse(raw: str) -> list[ExtractedFact]:
        facts: list[ExtractedFact] = []
        seen: set[str] = set()
        for line in (raw or "").splitlines():
            line = line.strip().lstrip("-*0123456789. ").strip()
            if not line:
                continue
            key = line.casefold()
            if key in seen:
                continue
            seen.add(key)
            facts.append(ExtractedFact(text=line))
        return facts
