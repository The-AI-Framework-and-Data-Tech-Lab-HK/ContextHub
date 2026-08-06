"""Unit tests for the build-side confidence cascade router.

No DB, no network: a fake chat client returns canned verdicts. Tests assert the
per-variable tier ladder (cheapest first, escalate when confidence < tau) and the
gold-free confidence signals, plus that spaCy staying optional does not break
variable-2 when the model is absent.
"""

import uuid

import pytest

from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.cascade_router import (
    route_candidate_selection,
    route_disambiguation,
    route_edge_discovery,
    route_extraction,
    _regex_extract_sentence,
)
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)


class FakeChat(BaseChatClient):
    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else "NONE"


# --------------------------------------------------------------------------- #
# Variable 4 — candidate selection ladder (structural cosine confidence).
# --------------------------------------------------------------------------- #

def test_var4_empty_pool_returns_block():
    r = route_candidate_selection("x", [1.0, 0.0], [], tau=0.5)
    assert r.tier == "block" and r.candidates == []


def test_var4_block_when_confident_and_enough():
    # 3 lexical matches all cosine-1 to the query => stay at cheap 'block' tier.
    pool = [
        CandidateFact(id=uuid.uuid4(), text="team lead report owner", embedding=[1.0, 0.0]),
        CandidateFact(id=uuid.uuid4(), text="team lead deputy report", embedding=[1.0, 0.0]),
        CandidateFact(id=uuid.uuid4(), text="report team lead backup", embedding=[1.0, 0.0]),
    ]
    r = route_candidate_selection("team lead report", [1.0, 0.0], pool, tau=0.5)
    assert r.tier == "block"
    assert len(r.candidates) == 3


def test_var4_escalates_to_full_when_nothing_similar():
    # No lexical overlap AND low cosine => escalate past embed_topk to full.
    pool = [
        CandidateFact(id=uuid.uuid4(), text="lunch noon cafeteria", embedding=[0.0, 1.0]),
        CandidateFact(id=uuid.uuid4(), text="weather sunny today", embedding=[0.0, 1.0]),
    ]
    r = route_candidate_selection("team lead report", [1.0, 0.0], pool, tau=0.9)
    assert r.tier == "full"
    assert len(r.candidates) == 2


# --------------------------------------------------------------------------- #
# Variable 3 — edge discovery ladder (regex hard-block, cheap NONE, escalate).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_var3_regex_hardblock_no_llm():
    cheap = DependencyDiscoveryService(FakeChat(["1"]))
    strong = DependencyDiscoveryService(FakeChat(["1"]))
    cands = [CandidateFact(id=uuid.uuid4(), text="I live in Pyresta Meadow")]
    r = await route_edge_discovery(
        "If the team lead changes, the recipient will be James Lee",
        cands, tau=0.5, cheap=cheap, strong=strong,
    )
    assert r.tier == "regex" and r.sources == []


@pytest.mark.asyncio
async def test_var3_cheap_none_trusted():
    cheap = DependencyDiscoveryService(FakeChat(["NONE"]))
    strong = DependencyDiscoveryService(FakeChat(["1"]))  # must not be consulted
    cands = [CandidateFact(id=uuid.uuid4(), text="Lunch is at noon")]
    r = await route_edge_discovery("The sky is blue", cands, tau=0.5, cheap=cheap, strong=strong)
    assert r.tier == "cheap_none" and r.sources == []
    assert strong._chat.prompts == []


@pytest.mark.asyncio
async def test_var3_escalates_to_strong_on_weak_support():
    a = uuid.uuid4()
    # Cheap proposes an edge, but the two texts share no content words => overlap
    # coefficient 0 < tau, so the strong tier re-judges.
    cheap = DependencyDiscoveryService(FakeChat(["1"]))
    strong = DependencyDiscoveryService(FakeChat(["NONE"]))
    cands = [CandidateFact(id=a, text="pottery class every friday")]
    r = await route_edge_discovery("the report recipient is Nam", cands, tau=0.5,
                                   cheap=cheap, strong=strong)
    assert r.tier == "strong"
    assert strong._chat.prompts  # strong tier was consulted


# --------------------------------------------------------------------------- #
# Variable 1 — extraction ladder (regex "X is Y" high-confidence lift).
# --------------------------------------------------------------------------- #

def test_var1_regex_lifts_clean_declarative():
    assert _regex_extract_sentence("The team lead is Seokjin.") is not None


def test_var1_regex_skips_pronoun_and_greeting():
    assert _regex_extract_sentence("He is the manager.") is None
    assert _regex_extract_sentence("Hi there, how are you?") is None
    assert _regex_extract_sentence("The lead is Seokjin and he assigned it.") is None


@pytest.mark.asyncio
async def test_var1_all_clean_stays_regex():
    cheap = ConversationExtractionService(FakeChat([]))  # must not be consulted
    strong = ConversationExtractionService(FakeChat([]))
    text = "The team lead is Seokjin. The report owner is Nam."
    r = await route_extraction(text, tau=0.5, cheap=cheap, strong=strong)
    assert r.tier == "regex"
    assert len(r.facts) == 2


# --------------------------------------------------------------------------- #
# Variable 2 — disambiguation ladder (literal hit; LLM fallback).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_var2_literal_hit_no_llm():
    llm = DependencyDiscoveryService(FakeChat(["1"]))  # must not be consulted
    r = await route_disambiguation("Seokjin", "Seokjin leads the team",
                                   ["Seokjin", "Nam"], tau=0.5, llm=llm)
    assert r.tier == "literal" and r.resolution == "Seokjin"
    assert llm._chat.prompts == []


@pytest.mark.asyncio
async def test_var2_tau_zero_skips_spacy_on_pronoun():
    # tau=0 gates off the spaCy tier; a bare pronoun has no literal match, so the
    # LLM tier runs as last resort (or 'none' if the pool were empty).
    llm = DependencyDiscoveryService(FakeChat(["1"]))
    r = await route_disambiguation("they", "the team met", ["Seokjin", "Nam"],
                                   tau=0.0, llm=llm)
    assert r.tier in ("llm", "none")
