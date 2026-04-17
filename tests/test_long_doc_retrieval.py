from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest
from contexthub.retrieval.long_doc import (
    KeywordRetriever,
    LongDocRetrievalCoordinator,
    LongDocRetrievalResult,
    MAX_SNIPPET_CHARS,
    TreeRetriever,
)
from contexthub.retrieval.long_doc import keyword_retriever as keyword_module
from contexthub.retrieval.long_doc import tree_retriever as tree_module
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.document_ingester import LongDocumentIngester
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService


class FakeRecord(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


class ScriptedChatClient:
    def __init__(self, response: str = "", *, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.prompts: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.response


class SectionDB:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = [FakeRecord(**row) for row in (rows or [])]
        self.fetches: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetches.append((sql, args))
        if "FROM document_sections" in sql:
            return self.rows
        return []


class RetrievalFlowDB:
    def __init__(self, candidates: list[dict], quality_rows: list[dict] | None = None):
        self.candidates = [FakeRecord(**row) for row in candidates]
        self.quality_rows = [FakeRecord(**row) for row in (quality_rows or [])]
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        if "SELECT id, adopted_count, ignored_count" in sql:
            return self.quality_rows
        return []

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class StubRerank:
    async def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        del query
        ranked = [dict(candidate) for candidate in candidates]
        for idx, candidate in enumerate(ranked, start=1):
            candidate["_rerank_score"] = float(idx)
        return list(reversed(ranked))


class StubRerankWithScores:
    def __init__(self, scores_by_uri: dict[str, float]):
        self.scores_by_uri = scores_by_uri

    async def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        del query
        ranked = []
        for candidate in candidates:
            enriched = dict(candidate)
            enriched["_rerank_score"] = self.scores_by_uri[enriched["uri"]]
            ranked.append(enriched)
        return sorted(ranked, key=lambda item: item["_rerank_score"], reverse=True)


class StubACL:
    def __init__(self, masks: list[str] | None = None, visible_uris: set[str] | None = None):
        self.calls: list[list[dict]] = []
        self.masks = masks
        self.visible_uris = visible_uris

    async def filter_visible_with_acl(self, db, candidates, ctx):
        del db, ctx
        self.calls.append(candidates)
        visible = []
        for candidate in candidates:
            if self.visible_uris is not None and candidate["uri"] not in self.visible_uris:
                continue
            visible.append((candidate, self.masks))
        return visible


class CaptureCoordinator:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def retrieve(self, db, query, candidates, *, strategy="tree"):
        del db, query
        self.calls.append(candidates)
        enriched = [dict(candidate) for candidate in candidates]
        enriched[0]["snippet"] = "secret snippet"
        enriched[0]["section_id"] = 7
        enriched[0]["retrieval_strategy"] = strategy
        return enriched


class DummyTreeStrategy:
    def __init__(self, results_by_uri: dict[str, list[LongDocRetrievalResult]] | None = None, failures: set[str] | None = None):
        self.results_by_uri = results_by_uri or {}
        self.failures = failures or set()
        self.calls: list[tuple[str, float]] = []

    async def retrieve(self, db, query, context_id, uri, file_path, *, base_score=0.0):
        del db, query, context_id, file_path
        self.calls.append((uri, base_score))
        if uri in self.failures:
            raise RuntimeError("boom")
        return self.results_by_uri.get(uri, [])


class DummyKeywordStrategy:
    def __init__(self, results: list[LongDocRetrievalResult] | None = None):
        self.results = results or []
        self.calls: list[list[dict]] = []

    async def retrieve(self, db, query, doc_contexts):
        del db, query
        self.calls.append(doc_contexts)
        return self.results


class FakeProcess:
    def __init__(self, stdout: str, returncode: int):
        self._stdout = stdout.encode("utf-8")
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def _section_row(
    section_id: int,
    context_id,
    *,
    parent_id: int | None,
    title: str,
    depth: int,
    start_offset: int,
    end_offset: int,
    summary: str,
    token_count: int,
) -> dict:
    return {
        "section_id": section_id,
        "context_id": context_id,
        "parent_id": parent_id,
        "node_id": f"node-{section_id}",
        "title": title,
        "depth": depth,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "summary": summary,
        "token_count": token_count,
        "account_id": "acme",
        "created_at": None,
    }


def _doc_candidate(
    doc_id,
    *,
    uri: str,
    file_path: str | None,
    score: float = 1.0,
    status: str = "active",
    context_type: str = "resource",
) -> dict:
    return {
        "id": doc_id,
        "uri": uri,
        "context_type": context_type,
        "scope": "team",
        "owner_space": "engineering",
        "status": status,
        "version": 1,
        "l0_content": "postgres replication handbook",
        "l1_content": "wal lag troubleshooting guide",
        "tags": ["doc"],
        "file_path": file_path,
        "_rerank_score": score,
    }


def test_long_doc_retrieval_result_contract():
    result = LongDocRetrievalResult(
        context_id=uuid.uuid4(),
        uri="ctx://resources/manuals/postgres",
        strategy="tree",
        section_id=12,
        snippet="hello",
        snippet_offset=(0, 5),
        relevance_score=0.9,
    )
    assert result.strategy == "tree"
    assert result.section_id == 12
    assert result.snippet_offset == (0, 5)


@pytest.mark.asyncio
async def test_tree_retriever_returns_section_snippet_and_offset(tmp_path: Path):
    context_id = uuid.uuid4()
    text = "Root\nIntro alpha details\nDeep replication lag answer here\nTail"
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")
    rows = [
        _section_row(1, context_id, parent_id=None, title="Document", depth=0, start_offset=0, end_offset=len(text), summary="whole doc", token_count=5000),
        _section_row(2, context_id, parent_id=1, title="Intro", depth=1, start_offset=5, end_offset=24, summary="alpha overview", token_count=3000),
        _section_row(3, context_id, parent_id=2, title="Replication", depth=2, start_offset=24, end_offset=57, summary="replication lag answer", token_count=500),
    ]
    retriever = TreeRetriever(ScriptedChatClient(response="3"))

    results = await retriever.retrieve(SectionDB(rows), "replication lag", context_id, "ctx://resources/manuals/postgres", str(doc_dir), base_score=1.0)

    assert len(results) == 1
    result = results[0]
    assert result.strategy == "tree"
    assert result.section_id == 3
    assert result.snippet
    assert len(result.snippet) <= MAX_SNIPPET_CHARS
    assert result.snippet == text[result.snippet_offset[0]:result.snippet_offset[1]]


@pytest.mark.asyncio
async def test_tree_retriever_stops_at_small_node_even_if_children_exist(tmp_path: Path):
    context_id = uuid.uuid4()
    text = "Root summary\nFocused answer\nNested detail"
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")
    rows = [
        _section_row(1, context_id, parent_id=None, title="Root", depth=0, start_offset=0, end_offset=len(text), summary="whole doc", token_count=300),
        _section_row(2, context_id, parent_id=1, title="Child", depth=1, start_offset=13, end_offset=len(text), summary="nested detail", token_count=100),
    ]

    result = (await TreeRetriever(ScriptedChatClient(response="1")).retrieve(SectionDB(rows), "root summary", context_id, "ctx://resources/manuals/postgres", str(doc_dir)))[0]

    assert result.section_id == 1
    assert result.snippet == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_client",
    [
        ScriptedChatClient(exc=RuntimeError("llm down")),
        ScriptedChatClient(response=""),
        ScriptedChatClient(response="999"),
    ],
)
async def test_tree_retriever_falls_back_deterministically(tmp_path: Path, chat_client: ScriptedChatClient):
    context_id = uuid.uuid4()
    text = "Overview\nWAL replication lag section\nOther appendix"
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")
    rows = [
        _section_row(1, context_id, parent_id=None, title="Document", depth=0, start_offset=0, end_offset=len(text), summary="overview", token_count=5000),
        _section_row(2, context_id, parent_id=1, title="WAL replication", depth=1, start_offset=9, end_offset=37, summary="lag troubleshooting", token_count=400),
        _section_row(3, context_id, parent_id=1, title="Appendix", depth=1, start_offset=38, end_offset=len(text), summary="misc", token_count=300),
    ]

    result = (await TreeRetriever(chat_client).retrieve(SectionDB(rows), "replication lag", context_id, "ctx://resources/manuals/postgres", str(doc_dir)))[0]

    assert result.section_id == 2
    assert result.strategy == "tree"


@pytest.mark.asyncio
async def test_tree_retriever_uses_character_offsets_and_clamps_bounds(tmp_path: Path):
    context_id = uuid.uuid4()
    text = "你好abcDEF"
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")
    rows = [
        _section_row(1, context_id, parent_id=None, title="Document", depth=0, start_offset=0, end_offset=len(text), summary="whole", token_count=3000),
        _section_row(2, context_id, parent_id=1, title="Body", depth=1, start_offset=2, end_offset=100, summary="body", token_count=200),
    ]

    result = (await TreeRetriever(ScriptedChatClient(response="2")).retrieve(SectionDB(rows), "body", context_id, "ctx://resources/manuals/unicode", str(doc_dir)))[0]

    assert result.snippet == "abcDEF"
    assert result.snippet_offset == (2, len(text))


@pytest.mark.asyncio
async def test_tree_retriever_handles_invalid_ranges_and_missing_files(tmp_path: Path):
    context_id = uuid.uuid4()
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    rows = [
        _section_row(1, context_id, parent_id=None, title="Document", depth=0, start_offset=0, end_offset=10, summary="whole", token_count=3000),
        _section_row(2, context_id, parent_id=1, title="Broken", depth=1, start_offset=8, end_offset=3, summary="broken", token_count=100),
    ]
    retriever = TreeRetriever(ScriptedChatClient(response="2"))

    assert await retriever.retrieve(SectionDB(rows), "broken", context_id, "ctx://resources/manuals/missing", str(missing_dir)) == []

    valid_dir = tmp_path / "valid"
    valid_dir.mkdir()
    (valid_dir / "extracted.txt").write_text("1234567890", encoding="utf-8")
    assert await retriever.retrieve(SectionDB(rows), "broken", context_id, "ctx://resources/manuals/invalid", str(valid_dir)) == []


@pytest.mark.asyncio
async def test_keyword_retriever_returns_best_window_from_rg_hits(tmp_path: Path, monkeypatch):
    doc_id = uuid.uuid4()
    text = "prefix postgres replication details\nmiddle text\nWAL lag appears here\nsuffix"
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")

    match_event = json.dumps(
        {
            "type": "match",
            "data": {
                "line_number": 1,
                "lines": {"text": "prefix postgres replication details\n"},
                "submatches": [{"match": {"text": "postgres"}, "start": 7, "end": 15}],
            },
        }
    )

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        return FakeProcess(match_event, 0)

    monkeypatch.setattr(keyword_module.shutil, "which", lambda _: "/usr/bin/rg")
    monkeypatch.setattr(keyword_module.asyncio, "create_subprocess_exec", fake_exec)
    retriever = KeywordRetriever(ScriptedChatClient(response='[["postgres","replication"]]'))

    results = await retriever.retrieve(
        SimpleNamespace(),
        "postgres replication",
        [_doc_candidate(doc_id, uri="ctx://resources/manuals/postgres", file_path=str(doc_dir), score=2.0)],
    )

    assert len(results) == 1
    result = results[0]
    assert result.strategy == "keyword"
    assert result.section_id is None
    assert result.snippet
    assert len(result.snippet) <= MAX_SNIPPET_CHARS
    assert result.snippet_offset[0] <= text.index("postgres") < result.snippet_offset[1]


@pytest.mark.asyncio
async def test_keyword_retriever_translates_rg_byte_offsets_to_char_offsets(tmp_path: Path, monkeypatch):
    doc_id = uuid.uuid4()
    text = ("你" * 1500) + "世界" + "postgres"
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")

    match_event = json.dumps(
        {
            "type": "match",
            "data": {
                "line_number": 1,
                "absolute_offset": 0,
                "lines": {"text": text},
                "submatches": [{"match": {"text": "世界"}, "start": 4500, "end": 4506}],
            },
        }
    )

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        return FakeProcess(match_event, 0)

    monkeypatch.setattr(keyword_module.shutil, "which", lambda _: "/usr/bin/rg")
    monkeypatch.setattr(keyword_module.asyncio, "create_subprocess_exec", fake_exec)
    retriever = KeywordRetriever(
        ScriptedChatClient(response='[["世界"]]'),
        max_snippet_chars=64,
    )

    results = await retriever.retrieve(
        SimpleNamespace(),
        "世界",
        [_doc_candidate(doc_id, uri="ctx://resources/manuals/unicode", file_path=str(doc_dir), score=2.0)],
    )

    assert len(results) == 1
    result = results[0]
    world_index = text.index("世界")
    assert "世界" in result.snippet
    assert result.snippet_offset[0] <= world_index < result.snippet_offset[1]


@pytest.mark.asyncio
async def test_keyword_retriever_degrades_when_rg_missing_or_no_hits(tmp_path: Path, monkeypatch):
    doc_id = uuid.uuid4()
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text("postgres replication", encoding="utf-8")
    retriever = KeywordRetriever(ScriptedChatClient())

    monkeypatch.setattr(keyword_module.shutil, "which", lambda _: None)
    assert await retriever.retrieve(SimpleNamespace(), "postgres", [_doc_candidate(doc_id, uri="ctx://resources/manuals/postgres", file_path=str(doc_dir))]) == []

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        return FakeProcess("", 1)

    monkeypatch.setattr(keyword_module.shutil, "which", lambda _: "/usr/bin/rg")
    monkeypatch.setattr(keyword_module.asyncio, "create_subprocess_exec", fake_exec)
    assert await retriever.retrieve(SimpleNamespace(), "postgres", [_doc_candidate(doc_id, uri="ctx://resources/manuals/postgres", file_path=str(doc_dir))]) == []


@pytest.mark.asyncio
async def test_keyword_retriever_degrades_when_rg_cannot_start(tmp_path: Path, monkeypatch):
    doc_id = uuid.uuid4()
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text("postgres replication", encoding="utf-8")
    retriever = KeywordRetriever(ScriptedChatClient())

    async def broken_exec(*args, **kwargs):
        del args, kwargs
        raise PermissionError("not executable")

    monkeypatch.setattr(keyword_module.shutil, "which", lambda _: "/tmp/fake-rg")
    monkeypatch.setattr(keyword_module.asyncio, "create_subprocess_exec", broken_exec)

    assert await retriever.retrieve(
        SimpleNamespace(),
        "postgres",
        [_doc_candidate(doc_id, uri="ctx://resources/manuals/postgres", file_path=str(doc_dir))],
    ) == []


@pytest.mark.asyncio
async def test_keyword_retriever_skips_unreadable_docs_and_falls_back_keyword_extraction(tmp_path: Path, monkeypatch):
    doc_id = uuid.uuid4()
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    (good_dir / "extracted.txt").write_text("wal lag shows up here", encoding="utf-8")
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()

    event = json.dumps(
        {
            "type": "match",
            "data": {
                "line_number": 1,
                "lines": {"text": "wal lag shows up here"},
                "submatches": [{"match": {"text": "wal"}, "start": 0, "end": 3}],
            },
        }
    )

    async def fake_exec(*args, **kwargs):
        del args, kwargs
        return FakeProcess(event, 0)

    monkeypatch.setattr(keyword_module.shutil, "which", lambda _: "/usr/bin/rg")
    monkeypatch.setattr(keyword_module.asyncio, "create_subprocess_exec", fake_exec)
    retriever = KeywordRetriever(ScriptedChatClient(exc=RuntimeError("llm fail")))

    keyword_groups = await retriever._extract_keywords("How to reduce WAL lag?")
    assert ["wal"] in keyword_groups

    results = await retriever.retrieve(
        SimpleNamespace(),
        "How to reduce WAL lag?",
        [
            _doc_candidate(doc_id, uri="ctx://resources/manuals/good", file_path=str(good_dir)),
            _doc_candidate(uuid.uuid4(), uri="ctx://resources/manuals/missing", file_path=str(missing_dir)),
        ],
    )

    assert len(results) == 1
    assert results[0].uri == "ctx://resources/manuals/good"


@pytest.mark.asyncio
async def test_monte_carlo_sample_covers_hits_and_deduplicates(tmp_path: Path):
    text = "a" * 300 + "postgres" + "b" * 300
    retriever = KeywordRetriever(ScriptedChatClient())

    windows = await retriever._monte_carlo_sample(text, [320, 321, 322], window_size=200, n_samples=5)

    assert windows
    assert len(windows) == 1
    snippet, (start, end) = windows[0]
    assert snippet
    assert start <= 320 < end


@pytest.mark.asyncio
async def test_monte_carlo_sample_keeps_each_window_hit_covered_after_merge(tmp_path: Path):
    text = ("a" * 1200) + "postgres" + ("b" * 1200) + "replication" + ("c" * 1200)
    retriever = KeywordRetriever(ScriptedChatClient(), max_snippet_chars=400)
    first_hit = text.index("postgres")
    second_hit = text.index("replication")

    windows = await retriever._monte_carlo_sample(
        text,
        [first_hit, second_hit],
        window_size=400,
        n_samples=5,
    )

    assert len(windows) >= 2
    assert any(start <= first_hit < end for _snippet, (start, end) in windows)
    assert any(start <= second_hit < end for _snippet, (start, end) in windows)


@pytest.mark.asyncio
async def test_coordinator_replaces_only_long_docs_and_keeps_failures():
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    short_id = uuid.uuid4()
    candidates = [
        _doc_candidate(first_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=1.3),
        _doc_candidate(second_id, uri="ctx://resources/manuals/two", file_path="/tmp/doc2", score=0.7),
        _doc_candidate(short_id, uri="ctx://team/engineering/resource", file_path=None, score=0.4),
    ]
    strategy = DummyTreeStrategy(
        results_by_uri={
            "ctx://resources/manuals/one": [
                LongDocRetrievalResult(
                    context_id=first_id,
                    uri="ctx://resources/manuals/one",
                    strategy="tree",
                    section_id=9,
                    snippet="snippet one",
                    snippet_offset=(0, 11),
                    relevance_score=1.1,
                )
            ]
        },
        failures={"ctx://resources/manuals/two"},
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", strategy)

    merged = await coordinator.retrieve(SimpleNamespace(), "query", candidates)

    assert merged[0]["snippet"] == "snippet one"
    assert merged[0]["section_id"] == 9
    assert merged[0]["retrieval_strategy"] == "tree"
    assert merged[0]["_rerank_score"] == pytest.approx(1.1)
    assert "snippet" not in merged[1]
    assert merged[2] == candidates[2]
    assert strategy.calls == [
        ("ctx://resources/manuals/one", 1.3),
        ("ctx://resources/manuals/two", 0.7),
    ]


@pytest.mark.asyncio
async def test_coordinator_keyword_strategy_batches_long_docs():
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    candidates = [
        _doc_candidate(first_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1"),
        _doc_candidate(second_id, uri="ctx://resources/manuals/two", file_path="/tmp/doc2"),
    ]
    keyword_strategy = DummyKeywordStrategy(
        [
            LongDocRetrievalResult(
                context_id=second_id,
                uri="ctx://resources/manuals/two",
                strategy="keyword",
                section_id=None,
                snippet="wal lag",
                snippet_offset=(10, 17),
                relevance_score=2.2,
            )
        ]
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("keyword", keyword_strategy)

    merged = await coordinator.retrieve(SimpleNamespace(), "wal lag", candidates, strategy="keyword")

    assert len(keyword_strategy.calls) == 1
    assert len(keyword_strategy.calls[0]) == 2
    assert merged[1]["retrieval_strategy"] == "keyword"
    assert merged[1]["_rerank_score"] == pytest.approx(2.2)


@pytest.mark.asyncio
async def test_coordinator_returns_original_candidates_for_unregistered_strategy():
    candidates = [
        _doc_candidate(uuid.uuid4(), uri="ctx://resources/manuals/one", file_path="/tmp/doc1"),
        _doc_candidate(uuid.uuid4(), uri="ctx://resources/manuals/two", file_path=None),
    ]
    coordinator = LongDocRetrievalCoordinator()

    merged = await coordinator.retrieve(SimpleNamespace(), "query", candidates, strategy="keyword")

    assert merged == candidates


@pytest.mark.asyncio
async def test_coordinator_triggers_on_any_candidate_with_file_path(tmp_path: Path):
    first_dir = tmp_path / "doc1"
    first_dir.mkdir()
    second_dir = tmp_path / "doc2"
    second_dir.mkdir()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    strategy = DummyTreeStrategy(
        results_by_uri={
            "ctx://resources/manuals/one": [
                LongDocRetrievalResult(
                    context_id=first_id,
                    uri="ctx://resources/manuals/one",
                    strategy="tree",
                    section_id=1,
                    snippet="first snippet",
                    snippet_offset=(0, 13),
                    relevance_score=1.5,
                )
            ]
        }
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", strategy)
    candidates = [
        _doc_candidate(first_id, uri="ctx://resources/manuals/one", file_path=str(first_dir)),
        _doc_candidate(second_id, uri="ctx://resources/manuals/two", file_path=str(second_dir)),
    ]

    merged = await coordinator.retrieve(SimpleNamespace(), "query", candidates)

    assert strategy.calls == [
        ("ctx://resources/manuals/one", 1.0),
        ("ctx://resources/manuals/two", 1.0),
    ]
    assert merged[0]["snippet"] == "first snippet"
    assert "snippet" not in merged[1]


@pytest.mark.asyncio
async def test_coordinator_keeps_candidate_when_tree_has_no_sections(tmp_path: Path):
    doc_id = uuid.uuid4()
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text("postgres wal lag", encoding="utf-8")
    candidate = _doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path=str(doc_dir), score=1.0)
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", TreeRetriever(ScriptedChatClient(response="1")))

    merged = await coordinator.retrieve(SectionDB([]), "wal lag", [candidate])

    assert merged == [candidate]
    assert "snippet" not in merged[0]


@pytest.mark.asyncio
async def test_retrieval_service_runs_coordinator_after_rerank_and_masks_snippet(monkeypatch):
    doc_id = uuid.uuid4()
    candidates = [_doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=0.3)]
    coordinator = CaptureCoordinator()
    acl = StubACL(masks=["secret"])
    service = RetrievalService(
        SimpleNamespace(rerank=StubRerank()),
        NoOpEmbeddingClient(),
        acl,
        masking_service=MaskingService(),
        long_doc_coordinator=coordinator,
    )

    async def fake_keyword_search(db, query, top_k, context_types=None, scopes=None, include_stale=True):
        del db, query, top_k, context_types, scopes, include_stale
        return candidates

    monkeypatch.setattr("contexthub.services.retrieval_service.keyword_search", fake_keyword_search)
    response = await service.search(
        RetrievalFlowDB(candidates, quality_rows=[{"id": doc_id, "adopted_count": 0, "ignored_count": 0}]),
        SearchRequest(query="postgres", top_k=1),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert coordinator.calls
    assert coordinator.calls[0][0]["_rerank_score"] == 1.0
    assert response.results[0].snippet == "[MASKED] snippet"
    assert response.results[0].section_id == 7
    assert response.results[0].retrieval_strategy == "tree"
    assert response.results[0].l2_content is None


@pytest.mark.asyncio
async def test_retrieval_service_runs_precision_before_acl_filter(monkeypatch):
    visible_id = uuid.uuid4()
    hidden_id = uuid.uuid4()
    visible_candidate = _doc_candidate(
        visible_id,
        uri="ctx://resources/manuals/visible",
        file_path="/tmp/visible-doc",
        score=0.3,
    )
    hidden_candidate = _doc_candidate(
        hidden_id,
        uri="ctx://resources/manuals/hidden",
        file_path="/tmp/hidden-doc",
        score=0.2,
    )
    coordinator = CaptureCoordinator()
    acl = StubACL(visible_uris={"ctx://resources/manuals/visible"})
    service = RetrievalService(
        SimpleNamespace(rerank=StubRerank()),
        NoOpEmbeddingClient(),
        acl,
        masking_service=MaskingService(),
        long_doc_coordinator=coordinator,
    )

    async def fake_keyword_search(db, query, top_k, context_types=None, scopes=None, include_stale=True):
        del db, query, top_k, context_types, scopes, include_stale
        return [visible_candidate, hidden_candidate]

    monkeypatch.setattr("contexthub.services.retrieval_service.keyword_search", fake_keyword_search)
    response = await service.search(
        RetrievalFlowDB(
            [visible_candidate, hidden_candidate],
            quality_rows=[
                {"id": visible_id, "adopted_count": 0, "ignored_count": 0},
                {"id": hidden_id, "adopted_count": 0, "ignored_count": 0},
            ],
        ),
        SearchRequest(query="postgres", top_k=2),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert len(coordinator.calls) == 1
    assert [candidate["uri"] for candidate in coordinator.calls[0]] == [
        "ctx://resources/manuals/hidden",
        "ctx://resources/manuals/visible",
    ]
    assert [result.uri for result in response.results] == ["ctx://resources/manuals/visible"]


@pytest.mark.asyncio
async def test_retrieval_service_default_path_uses_tree_only(monkeypatch):
    doc_id = uuid.uuid4()
    candidate = _doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=0.2)
    tree_strategy = DummyTreeStrategy(
        results_by_uri={
            "ctx://resources/manuals/one": [
                LongDocRetrievalResult(
                    context_id=doc_id,
                    uri="ctx://resources/manuals/one",
                    strategy="tree",
                    section_id=4,
                    snippet="tree snippet",
                    snippet_offset=(0, 12),
                    relevance_score=1.0,
                )
            ]
        }
    )
    keyword_strategy = DummyKeywordStrategy(
        [
            LongDocRetrievalResult(
                context_id=doc_id,
                uri="ctx://resources/manuals/one",
                strategy="keyword",
                section_id=None,
                snippet="keyword snippet",
                snippet_offset=(0, 15),
                relevance_score=9.9,
            )
        ]
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", tree_strategy)
    coordinator.register_strategy("keyword", keyword_strategy)
    service = RetrievalService(
        SimpleNamespace(rerank=StubRerank()),
        NoOpEmbeddingClient(),
        StubACL(),
        masking_service=MaskingService(),
        long_doc_coordinator=coordinator,
    )

    async def fake_keyword_search(db, query, top_k, context_types=None, scopes=None, include_stale=True):
        del db, query, top_k, context_types, scopes, include_stale
        return [candidate]

    monkeypatch.setattr("contexthub.services.retrieval_service.keyword_search", fake_keyword_search)
    response = await service.search(
        RetrievalFlowDB([candidate], quality_rows=[{"id": doc_id, "adopted_count": 0, "ignored_count": 0}]),
        SearchRequest(query="postgres", top_k=1),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert tree_strategy.calls == [("ctx://resources/manuals/one", 1.0)]
    assert keyword_strategy.calls == []
    assert response.results[0].retrieval_strategy == "tree"
    assert response.results[0].snippet == "tree snippet"


@pytest.mark.asyncio
async def test_retrieval_service_keeps_search_working_when_long_doc_precision_fails(monkeypatch):
    doc_id = uuid.uuid4()
    candidate = _doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=1.0)
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", DummyTreeStrategy(failures={"ctx://resources/manuals/one"}))
    service = RetrievalService(
        SimpleNamespace(rerank=StubRerank()),
        NoOpEmbeddingClient(),
        StubACL(),
        masking_service=MaskingService(),
        long_doc_coordinator=coordinator,
    )

    async def fake_keyword_search(db, query, top_k, context_types=None, scopes=None, include_stale=True):
        del db, query, top_k, context_types, scopes, include_stale
        return [candidate]

    monkeypatch.setattr("contexthub.services.retrieval_service.keyword_search", fake_keyword_search)

    response = await service.search(
        RetrievalFlowDB([candidate], quality_rows=[{"id": doc_id, "adopted_count": 0, "ignored_count": 0}]),
        SearchRequest(query="postgres", top_k=1),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert response.total == 1
    assert response.results[0].snippet is None


@pytest.mark.asyncio
async def test_retrieval_service_applies_quality_and_stale_to_long_doc_results(monkeypatch):
    stale_id = uuid.uuid4()
    active_id = uuid.uuid4()
    stale_candidate = _doc_candidate(
        stale_id,
        uri="ctx://resources/manuals/stale",
        file_path="/tmp/doc-stale",
        score=0.1,
        status="stale",
    )
    active_candidate = _doc_candidate(
        active_id,
        uri="ctx://resources/manuals/active",
        file_path="/tmp/doc-active",
        score=0.1,
        status="active",
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy(
        "tree",
        DummyTreeStrategy(
            results_by_uri={
                "ctx://resources/manuals/stale": [
                    LongDocRetrievalResult(
                        context_id=stale_id,
                        uri="ctx://resources/manuals/stale",
                        strategy="tree",
                        section_id=1,
                        snippet="stale snippet",
                        snippet_offset=(0, 13),
                        relevance_score=1.0,
                    )
                ],
                "ctx://resources/manuals/active": [
                    LongDocRetrievalResult(
                        context_id=active_id,
                        uri="ctx://resources/manuals/active",
                        strategy="tree",
                        section_id=2,
                        snippet="active snippet",
                        snippet_offset=(0, 14),
                        relevance_score=0.9,
                    )
                ],
            }
        ),
    )
    service = RetrievalService(
        SimpleNamespace(
            rerank=StubRerankWithScores(
                {
                    "ctx://resources/manuals/stale": 1.0,
                    "ctx://resources/manuals/active": 0.9,
                }
            )
        ),
        NoOpEmbeddingClient(),
        StubACL(),
        masking_service=MaskingService(),
        long_doc_coordinator=coordinator,
    )

    async def fake_keyword_search(db, query, top_k, context_types=None, scopes=None, include_stale=True):
        del db, query, top_k, context_types, scopes, include_stale
        return [stale_candidate, active_candidate]

    monkeypatch.setattr("contexthub.services.retrieval_service.keyword_search", fake_keyword_search)
    response = await service.search(
        RetrievalFlowDB(
            [stale_candidate, active_candidate],
            quality_rows=[
                {"id": stale_id, "adopted_count": 0, "ignored_count": 8},
                {"id": active_id, "adopted_count": 8, "ignored_count": 0},
            ],
        ),
        SearchRequest(query="postgres", top_k=2),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert [result.uri for result in response.results] == [
        "ctx://resources/manuals/active",
        "ctx://resources/manuals/stale",
    ]
    assert response.results[0].retrieval_strategy == "tree"
    assert response.results[1].retrieval_strategy == "tree"
    assert response.results[0].snippet == "active snippet"
    assert response.results[1].snippet == "stale snippet"
    assert response.results[0].score > response.results[1].score


@pytest.mark.asyncio
async def test_integration_long_doc_search_smoke(acme_session, services, query_agent_ctx, tmp_path: Path):
    source = tmp_path / "search-smoke.md"
    source.write_text(
        "# Postgres Handbook\n\n## WAL Lag\nTree path returns a focused snippet for WAL lag incidents.\n",
        encoding="utf-8",
    )
    await acme_session.execute(
        """
        INSERT INTO team_memberships (agent_id, team_id, role, access)
        VALUES ($1, $2::uuid, 'member', 'read_write')
        ON CONFLICT (agent_id, team_id)
        DO UPDATE SET access = 'read_write'
        """,
        query_agent_ctx.agent_id,
        "00000000-0000-0000-0000-000000000001",
    )

    ingester = LongDocumentIngester(
        chat_client=ScriptedChatClient(
            response="""
            {
              "sections": [
                {"node_id":"root","parent_node_id":null,"title":"Document","start_offset":0,"end_offset":84,"summary":"whole"},
                {"node_id":"wal","parent_node_id":"root","title":"WAL Lag","start_offset":20,"end_offset":84,"summary":"wal lag incidents"}
              ]
            }
            """
        ),
        embedding_client=NoOpEmbeddingClient(),
        content_generator=ContentGenerator(),
        acl=services.acl,
        audit=services.audit,
        doc_store_root=str(tmp_path / "docs"),
    )
    ingest_response = await ingester.ingest(
        acme_session,
        "ctx://resources/manuals/search-smoke",
        str(source),
        query_agent_ctx,
        tags=["integration"],
    )
    row = await acme_session.fetchrow(
        "SELECT l0_content FROM contexts WHERE uri = $1",
        ingest_response.uri,
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", TreeRetriever(ScriptedChatClient(exc=RuntimeError("fallback"))))
    retrieval_service = RetrievalService(
        RetrievalRouter.default(),
        NoOpEmbeddingClient(),
        services.acl,
        masking_service=services.masking,
        audit_service=services.audit,
        long_doc_coordinator=coordinator,
    )

    response = await retrieval_service.search(
        acme_session,
        SearchRequest(query=" ".join((row["l0_content"] or "WAL lag").split()[:4]), top_k=5),
        query_agent_ctx,
    )

    assert any(result.snippet for result in response.results)
    assert any(result.retrieval_strategy == "tree" for result in response.results)
