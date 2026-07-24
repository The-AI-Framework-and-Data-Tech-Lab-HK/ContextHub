"""Unit tests for write-time dependency discovery (DependencyDiscoveryService)
and its optional integration into MemoryService.add_memory.

No DB, no network: a fake chat client returns canned verdicts and a fake scoped
repo records the SQL. The key regression guard is that add_memory WITHOUT a
discovery service behaves exactly as before (no candidate query, no edges).
"""

import uuid
from contextlib import asynccontextmanager

import pytest

from contexthub.llm.chat_client import BaseChatClient
from contexthub.propagation.derived_memory_rule import (
    DerivedMemoryOracleRule,
    DerivedMemoryRule,
)
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)


class FakeChat(BaseChatClient):
    """Returns a queued reply per call; records prompts."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else "NONE"


# --------------------------------------------------------------------------- #
# DependencyDiscoveryService
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_discover_picks_named_source():
    a, b = uuid.uuid4(), uuid.uuid4()
    cands = [CandidateFact(id=a, text="The team lead is Seokjin Kang."),
             CandidateFact(id=b, text="Lunch is at noon.")]
    svc = DependencyDiscoveryService(FakeChat(["1\nderived from the team lead"]))
    out = await svc.discover_sources("Report recipient is Hyunwoo Nam, set by the team lead.", cands)
    assert out == [a]


@pytest.mark.asyncio
async def test_discover_none_returns_empty():
    cands = [CandidateFact(id=uuid.uuid4(), text="Lunch is at noon.")]
    svc = DependencyDiscoveryService(FakeChat(["NONE"]))
    assert await svc.discover_sources("The sky is blue.", cands) == []


@pytest.mark.asyncio
async def test_discover_multiple_sources_and_dedup():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cands = [CandidateFact(id=a, text="f1"), CandidateFact(id=b, text="f2"),
             CandidateFact(id=c, text="f3")]
    svc = DependencyDiscoveryService(FakeChat(["1,3,1"]))  # dup 1 collapses
    assert await svc.discover_sources("new", cands) == [a, c]


@pytest.mark.asyncio
async def test_discover_out_of_range_ignored():
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="f1")]
    svc = DependencyDiscoveryService(FakeChat(["1,5,9"]))  # only 1 valid
    assert await svc.discover_sources("new", cands) == [a]


@pytest.mark.asyncio
async def test_discover_no_candidates_skips_llm():
    chat = FakeChat(["1"])
    svc = DependencyDiscoveryService(chat)
    assert await svc.discover_sources("new", []) == []
    assert chat.prompts == []  # no LLM call when nothing to compare against


# --------------------------------------------------------------------------- #
# Tier-0 conditional-aware routing (conservative: routes prompt, LLM still decides)
# --------------------------------------------------------------------------- #

def test_looks_conditional_flags_predeclaration():
    f = DependencyDiscoveryService._looks_conditional
    # predeclaration/rule facts: the conditional is the MAIN clause (leading "if")
    assert f("If I change my residence, my appointment will be therapy")
    assert f("if the team lead changes, the recipient will be James Lee")
    # CRITICAL: a current-value fact with a trailing change-hint must NOT match.
    # This is the exact false positive that made hard mode build zero edges —
    # MEME cur nodes read "X is <value> — ...; if X changes, this would change".
    # It asserts a present derived value and DOES need an edge, so must be False.
    assert not f("My appointment is dermatologist (monthly) — this is determined "
                 "by my health; if my health changes, this would change")
    assert not f("My rent is $2000, which would rise if I move downtown")
    # plain value facts do NOT look conditional
    assert not f("The team lead is Seokjin Kang")
    assert not f("My appointment is dermatologist, set by where I live")


@pytest.mark.asyncio
async def test_conditional_aware_uses_variant_prompt_but_llm_decides():
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="I live in Pyresta Meadow")]
    # A conditional new-fact: routed to the conditional prompt; LLM answers NONE.
    chat = FakeChat(["NONE"])
    svc = DependencyDiscoveryService(chat, conditional_aware=True)
    out = await svc.discover_sources(
        "If I change my residence, my appointment will be therapy", cands)
    assert out == []
    assert "RULE, PLAN, or CONDITIONAL" in chat.prompts[0]  # variant prompt used


@pytest.mark.asyncio
async def test_conditional_aware_recall_guard_can_still_link():
    # A fact that contains 'if' but asserts a present derived value: the LLM may
    # still link it. Conservative design => syntactic check must NOT suppress it.
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="I live downtown")]
    chat = FakeChat(["1"])
    svc = DependencyDiscoveryService(chat, conditional_aware=True)
    out = await svc.discover_sources(
        "My rent is $2000, which would rise if I move", cands)
    assert out == [a]  # edge preserved despite 'if'


@pytest.mark.asyncio
async def test_naive_mode_ignores_conditional_routing():
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="I live in Pyresta Meadow")]
    chat = FakeChat(["1"])
    svc = DependencyDiscoveryService(chat)  # conditional_aware defaults False
    await svc.discover_sources("If I move, appointment will be therapy", cands)
    assert "RULE, PLAN, or CONDITIONAL" not in chat.prompts[0]  # naive prompt


@pytest.mark.asyncio
async def test_conditional_hard_excludes_edge_without_llm():
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="I live in Pyresta Meadow")]
    chat = FakeChat(["1"])  # would link if asked — but hard mode must not ask
    svc = DependencyDiscoveryService(chat, conditional_hard=True)
    out = await svc.discover_sources(
        "If I change my residence, my appointment will be therapy", cands)
    assert out == []           # no edge
    assert chat.prompts == []  # LLM never called for a conditional fact


@pytest.mark.asyncio
async def test_conditional_hard_still_uses_llm_for_plain_facts():
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="The team lead is Kang")]
    chat = FakeChat(["1"])
    svc = DependencyDiscoveryService(chat, conditional_hard=True)
    out = await svc.discover_sources("The report recipient is set by the team lead", cands)
    assert out == [a]           # non-conditional => normal LLM discovery
    assert len(chat.prompts) == 1


# --------------------------------------------------------------------------- #
# Registry: oracle rule only when chat+repo given; else no-op (no regression)
# --------------------------------------------------------------------------- #

def test_registry_default_noop_without_injection():
    reg = PropagationRuleRegistry.default()
    assert isinstance(reg.get_dep_rule("derived_from"), DerivedMemoryRule)


def test_registry_default_oracle_with_injection():
    reg = PropagationRuleRegistry.default(chat_client=FakeChat([]), repo=object())
    assert isinstance(reg.get_dep_rule("derived_from"), DerivedMemoryOracleRule)


# --------------------------------------------------------------------------- #
# MemoryService.add_memory: no discovery injected => no candidate query, no edges
# --------------------------------------------------------------------------- #

class RecordingRepo:
    """Scoped repo that records every SQL statement it runs."""

    def __init__(self, new_id):
        self._new_id = new_id
        self.sql = []

    async def fetchrow(self, sql, *args):
        self.sql.append(sql)
        return _FakeRow(self._new_id)

    async def fetch(self, sql, *args):
        self.sql.append(sql)
        return []

    async def execute(self, sql, *args):
        self.sql.append(sql)
        return "INSERT 1"


class _FakeRow(dict):
    def __init__(self, ctx_id):
        super().__init__(
            id=ctx_id, uri="ctx://agent/a/memories/m", context_type="memory",
            scope="agent", owner_space="a", account_id="acct",
            l0_content="x", l1_content="x", l2_content="x", file_path=None,
            status="active", version=1, tags=[], created_at=None, updated_at=None,
            last_accessed_at=None, stale_at=None, archived_at=None, deleted_at=None,
            active_count=0, adopted_count=0, ignored_count=0,
        )

    def __getattr__(self, k):
        return self[k]


def _make_memory_service(discovery=None):
    from contexthub.generation.base import ContentGenerator
    from contexthub.llm.base import NoOpEmbeddingClient
    from contexthub.services.acl_service import ACLService
    from contexthub.services.indexer_service import IndexerService
    from contexthub.services.masking_service import MaskingService
    from contexthub.services.memory_service import MemoryService

    indexer = IndexerService(ContentGenerator(), NoOpEmbeddingClient())
    return MemoryService(indexer, ACLService(), MaskingService(), discovery=discovery)


@pytest.mark.asyncio
async def test_add_memory_without_discovery_runs_no_candidate_query():
    from contexthub.models.memory import AddMemoryRequest
    from contexthub.models.request import RequestContext

    new_id = uuid.uuid4()
    repo = RecordingRepo(new_id)
    svc = _make_memory_service(discovery=None)
    await svc.add_memory(repo, AddMemoryRequest(content="a fact"),
                         RequestContext(account_id="acct", agent_id="a"))

    # No discovery => no candidate SELECT, no derived_from INSERT.
    assert not any("status = 'active'" in s and "ORDER BY updated_at DESC" in s and "LIMIT" in s
                   for s in repo.sql), "candidate query should not run without discovery"
    assert not any("dependencies" in s for s in repo.sql), "no edges without discovery"


@pytest.mark.asyncio
async def test_add_memory_with_discovery_writes_edge():
    from contexthub.models.memory import AddMemoryRequest
    from contexthub.models.request import RequestContext

    new_id = uuid.uuid4()
    src_id = uuid.uuid4()

    class RepoWithCandidate(RecordingRepo):
        async def fetch(self, sql, *args):
            self.sql.append(sql)
            return [_CandRow(src_id)]

    class _CandRow(dict):
        def __init__(self, cid):
            super().__init__(id=cid, text="The team lead is Kang.")

        def __getattr__(self, k):
            return self[k]

    repo = RepoWithCandidate(new_id)
    svc = _make_memory_service(DependencyDiscoveryService(FakeChat(["1"])))
    await svc.add_memory(repo, AddMemoryRequest(content="recipient set by team lead"),
                         RequestContext(account_id="acct", agent_id="a"))

    edge_inserts = [s for s in repo.sql if "dependencies" in s and "derived_from" in s]
    assert len(edge_inserts) == 1
