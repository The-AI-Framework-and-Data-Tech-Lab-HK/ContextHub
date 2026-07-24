"""Unit tests for write-time change detection (ChangeDetectionService) and its
optional integration into MemoryService.add_memory / add_conversation.

No DB, no network: a fake chat client returns canned verdicts and a fake scoped
repo records the SQL. The key regression guard is that add_memory WITHOUT a
detection service fires no supersede change_event (behaviour unchanged).
"""

import uuid

import pytest

from contexthub.errors import BadRequestError
from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.change_detection_service import ChangeDetectionService
from contexthub.services.conversation_extraction_service import ExtractedFact
from contexthub.services.dependency_discovery_service import CandidateFact


class FakeChat(BaseChatClient):
    """Returns a queued reply per call; records prompts."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else "NONE"


# --------------------------------------------------------------------------- #
# ChangeDetectionService
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_detect_picks_superseded():
    a, b = uuid.uuid4(), uuid.uuid4()
    cands = [CandidateFact(id=a, text="The team lead is Seokjin Kang."),
             CandidateFact(id=b, text="Lunch is at noon.")]
    svc = ChangeDetectionService(FakeChat(["1\nnew value for the same thing"]))
    out = await svc.detect_superseded("The team lead is now Hyunwoo Nam.", cands)
    assert out == [a]


@pytest.mark.asyncio
async def test_detect_none_returns_empty():
    cands = [CandidateFact(id=uuid.uuid4(), text="Lunch is at noon.")]
    svc = ChangeDetectionService(FakeChat(["NONE"]))
    assert await svc.detect_superseded("The sky is blue.", cands) == []


@pytest.mark.asyncio
async def test_detect_multiple_and_dedup():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cands = [CandidateFact(id=a, text="f1"), CandidateFact(id=b, text="f2"),
             CandidateFact(id=c, text="f3")]
    svc = ChangeDetectionService(FakeChat(["1,3,1"]))  # dup 1 collapses
    assert await svc.detect_superseded("new", cands) == [a, c]


@pytest.mark.asyncio
async def test_detect_out_of_range_ignored():
    a = uuid.uuid4()
    cands = [CandidateFact(id=a, text="f1")]
    svc = ChangeDetectionService(FakeChat(["1,5,9"]))  # only 1 valid
    assert await svc.detect_superseded("new", cands) == [a]


@pytest.mark.asyncio
async def test_detect_no_candidates_skips_llm():
    chat = FakeChat(["1"])
    svc = ChangeDetectionService(chat)
    assert await svc.detect_superseded("new", []) == []
    assert chat.prompts == []  # no LLM call when nothing to compare against


# --------------------------------------------------------------------------- #
# Fake repo + memory-service factory (mirrors test_dependency_discovery.py)
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


class _CandRow(dict):
    def __init__(self, cid, text):
        super().__init__(id=cid, text=text)

    def __getattr__(self, k):
        return self[k]


def _make_memory_service(*, detection=None, extractor=None):
    from contexthub.generation.base import ContentGenerator
    from contexthub.llm.base import NoOpEmbeddingClient
    from contexthub.services.acl_service import ACLService
    from contexthub.services.indexer_service import IndexerService
    from contexthub.services.masking_service import MaskingService
    from contexthub.services.memory_service import MemoryService

    indexer = IndexerService(ContentGenerator(), NoOpEmbeddingClient())
    return MemoryService(
        indexer, ACLService(), MaskingService(),
        detection=detection, extractor=extractor,
    )


def _ctx():
    from contexthub.models.request import RequestContext
    return RequestContext(account_id="acct", agent_id="a")


def _is_modified_event(sql: str) -> bool:
    return "change_events" in sql and "'modified'" in sql


# --------------------------------------------------------------------------- #
# add_memory: no detection injected => no supersede event (regression guard)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_add_memory_without_detection_fires_no_supersede_event():
    from contexthub.models.memory import AddMemoryRequest

    repo = RecordingRepo(uuid.uuid4())
    svc = _make_memory_service(detection=None)
    await svc.add_memory(repo, AddMemoryRequest(content="a fact"), _ctx())

    # The only change_events insert is the 'created' one; no 'modified'.
    assert not any(_is_modified_event(s) for s in repo.sql), \
        "no supersede event should fire without detection"


@pytest.mark.asyncio
async def test_add_memory_with_detection_fires_modified_event():
    from contexthub.models.memory import AddMemoryRequest

    new_id = uuid.uuid4()
    old_id = uuid.uuid4()

    class RepoWithCandidate(RecordingRepo):
        async def fetch(self, sql, *args):
            self.sql.append(sql)
            return [_CandRow(old_id, "The team lead is Kang.")]

    repo = RepoWithCandidate(new_id)
    svc = _make_memory_service(detection=ChangeDetectionService(FakeChat(["1"])))
    await svc.add_memory(
        repo, AddMemoryRequest(content="The team lead is now Nam."), _ctx()
    )

    modified = [s for s in repo.sql if _is_modified_event(s)]
    assert len(modified) == 1


# --------------------------------------------------------------------------- #
# add_conversation
# --------------------------------------------------------------------------- #

class FakeExtractor:
    def __init__(self, facts):
        self._facts = facts

    async def extract(self, conversation: str):
        return [ExtractedFact(text=t) for t in self._facts]


@pytest.mark.asyncio
async def test_add_conversation_without_extractor_raises():
    svc = _make_memory_service(extractor=None)
    with pytest.raises(BadRequestError):
        await svc.add_conversation(RecordingRepo(uuid.uuid4()), "hello", _ctx())


@pytest.mark.asyncio
async def test_add_conversation_stores_each_fact():
    repo = RecordingRepo(uuid.uuid4())
    extractor = FakeExtractor(["fact one", "fact two", "fact three"])
    svc = _make_memory_service(extractor=extractor)
    out = await svc.add_conversation(repo, "raw dialogue", _ctx())

    assert len(out) == 3  # one Context per extracted fact
    inserts = [s for s in repo.sql if "INSERT INTO contexts" in s]
    assert len(inserts) == 3
