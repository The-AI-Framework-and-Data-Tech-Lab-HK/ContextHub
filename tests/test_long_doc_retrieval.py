from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from types import SimpleNamespace
from contexthub.retrieval.router import RetrievalRouter

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

    async def fetchval(self, sql: str, *args):
        self.fetches.append((sql, args))
        return None

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
        if "SELECT id, version, status, validity_status" in sql:
            allowed = set(args[0])
            return [
                FakeRecord(
                    id=row["id"],
                    version=row.get("version"),
                    status=row.get("status"),
                    validity_status=row.get("validity_status")
                    or ("fresh" if row.get("status") == "active" else row.get("status")),
                )
                for row in self.candidates
                if row["id"] in allowed
            ]
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
    async def search_originals(self, db, query, documents, *, query_embedding=None, top_k=30, original_fusion="weighted"):
        assert not documents
        return []

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
async def test_tree_retriever_can_stop_at_parent_when_body_matches_better_than_children(tmp_path: Path):
    context_id = uuid.uuid4()
    parent_text = (
        "The benchmark covers 57 subjects across STEM, the humanities, the social sciences, and more.\n"
        "It ranges from elementary topics to advanced professional material.\n"
    )
    stem_text = "STEM subjects include mathematics, physics, and computer science.\n"
    humanities_text = "Humanities subjects include philosophy, law, and history.\n"
    text = "Document overview\n" + parent_text + stem_text + humanities_text
    parent_start = text.index(parent_text)
    stem_start = text.index(stem_text)
    humanities_start = text.index(humanities_text)
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")
    rows = [
        _section_row(1, context_id, parent_id=None, title="Document", depth=0, start_offset=0, end_offset=len(text), summary="whole doc", token_count=5000),
        _section_row(2, context_id, parent_id=1, title="A Multitask Test", depth=1, start_offset=parent_start, end_offset=len(text), summary="benchmark overview", token_count=2600),
        _section_row(3, context_id, parent_id=2, title="STEM", depth=2, start_offset=stem_start, end_offset=humanities_start, summary="science technology engineering mathematics", token_count=300),
        _section_row(4, context_id, parent_id=2, title="Humanities", depth=2, start_offset=humanities_start, end_offset=len(text), summary="law philosophy history", token_count=300),
    ]

    result = (
        await TreeRetriever(ScriptedChatClient(exc=RuntimeError("llm down"))).retrieve(
            SectionDB(rows),
            "57 subjects across STEM humanities social sciences",
            context_id,
            "ctx://resources/manuals/mmlu",
            str(doc_dir),
        )
    )[0]

    assert result.section_id == 2
    assert "57 subjects across STEM" in result.snippet


@pytest.mark.asyncio
async def test_tree_retriever_focuses_snippet_inside_large_selected_section(tmp_path: Path):
    context_id = uuid.uuid4()
    prefix = "Intro material.\n" * 80
    target = (
        "The benchmark covers 57 subjects across STEM, the humanities, the social sciences, and more.\n"
    )
    suffix = "Tail material.\n" * 80
    text = prefix + target + suffix
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text, encoding="utf-8")
    rows = [
        _section_row(1, context_id, parent_id=None, title="Document", depth=0, start_offset=0, end_offset=len(text), summary="whole doc", token_count=6000),
        _section_row(2, context_id, parent_id=1, title="Overview", depth=1, start_offset=0, end_offset=len(text), summary="benchmark overview", token_count=3000),
    ]

    result = (
        await TreeRetriever(ScriptedChatClient(exc=RuntimeError("llm down")), max_snippet_chars=220).retrieve(
            SectionDB(rows),
            "57 subjects across STEM humanities social sciences",
            context_id,
            "ctx://resources/manuals/mmlu-focus",
            str(doc_dir),
        )
    )[0]

    assert result.section_id == 2
    assert target.strip() in result.snippet
    assert result.snippet_offset[0] > 0
    assert result.snippet_offset[0] < text.index(target)


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
@pytest.mark.parametrize("query", ["once", "can", "make", "1986", "8800"])
async def test_keyword_retriever_matches_whole_words_and_numbers(tmp_path: Path, query):
    false_dir = tmp_path / "substring"
    false_dir.mkdir()
    (false_dir / "extracted.txt").write_text("concepts significantly makes 19860 88000")
    exact_dir = tmp_path / "exact"
    exact_dir.mkdir()
    original = "Once, CAN; make! (1986) VAX-8800."
    (exact_dir / "extracted.txt").write_text(original)
    docs = [
        _doc_candidate(uuid.uuid4(), uri="ctx://resources/substring", file_path=str(false_dir)),
        _doc_candidate(uuid.uuid4(), uri="ctx://resources/exact", file_path=str(exact_dir)),
    ]
    results = await KeywordRetriever().retrieve(SectionDB(), query, docs)
    assert len(results) == 1
    assert results[0].uri == "ctx://resources/exact"
    assert results[0].snippet == original
    assert results[0].snippet_offset == (0, len(original))


@pytest.mark.asyncio
async def test_keyword_bm25_statistics_include_all_ranges_and_nonmatching_documents(tmp_path: Path):
    # Four equally sized (two-token) fragments across two documents. Only one
    # matches "rare"; the three nonmatching fragments must still count in IDF.
    parts = ["rare thing\n\n", "common words\n\n", "common words\n\n"]
    original = "".join(parts)
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir(); second_dir.mkdir()
    (first_dir / "extracted.txt").write_text(original)
    (second_dir / "extracted.txt").write_text("common words")
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    sections = [{"section_id": 1, "parent_id": None, "depth": 0,
                 "start_offset": 0, "end_offset": len(original)}]
    offset = 0
    for i, part in enumerate(parts, 2):
        sections.append({"section_id": i, "parent_id": 1, "depth": 1,
                         "start_offset": offset, "end_offset": offset + len(part)})
        offset += len(part)

    class DB:
        async def fetch(self, sql, *args):
            assert "FROM document_sections" in sql
            return sections if args[0] == first_id else []

    docs = [
        _doc_candidate(first_id, uri="ctx://resources/first", file_path=str(first_dir)),
        _doc_candidate(second_id, uri="ctx://resources/second", file_path=str(second_dir)),
    ]
    retriever = KeywordRetriever()
    hits = await retriever.retrieve(DB(), "rare", docs)
    assert len(hits) == 1
    assert hits[0].section_id == 2 and hits[0].snippet == parts[0]
    assert hits[0].relevance_score == pytest.approx(math.log(1 + 3.5 / 1.5))
    # Query changes must not change corpus statistics. Rarer tokens outweigh
    # common tokens; query deduplication and the existing stopwords remain.
    hits = await retriever.retrieve(DB(), "How rare rare common?", docs)
    assert len(hits) == 4
    assert hits[0].snippet == parts[0]
    assert hits[0].relevance_score == pytest.approx(math.log(1 + 3.5 / 1.5))
    assert all(h.relevance_score == pytest.approx(math.log(1 + 1.5 / 3.5)) for h in hits[1:])


@pytest.mark.asyncio
async def test_keyword_retriever_keeps_unicode_character_offsets(tmp_path: Path):
    text = "背景。\n\n批准码 23 Ada。\n\n后文。"
    folder = tmp_path / "unicode"
    folder.mkdir()
    (folder / "extracted.txt").write_text(text)
    doc = _doc_candidate(uuid.uuid4(), uri="ctx://resources/unicode", file_path=str(folder))
    results = await KeywordRetriever(max_snippet_chars=14).retrieve(SectionDB(), "批准码 23", [doc])
    assert len(results) == 1
    hit = results[0]
    assert "批准码 23" in hit.snippet
    assert text[slice(*hit.snippet_offset)] == hit.snippet
    assert len(hit.snippet) <= 14


@pytest.mark.asyncio
async def test_keyword_retriever_skips_unreadable_docs_and_handles_empty_queries(tmp_path: Path):
    good_dir, missing_dir = tmp_path / "good", tmp_path / "missing"
    good_dir.mkdir(); missing_dir.mkdir()
    (good_dir / "extracted.txt").write_text("wal lag shows up here")
    docs = [
        _doc_candidate(uuid.uuid4(), uri="ctx://resources/good", file_path=str(good_dir)),
        _doc_candidate(uuid.uuid4(), uri="ctx://resources/missing", file_path=str(missing_dir)),
    ]
    retriever = KeywordRetriever()
    hits = await retriever.retrieve(SectionDB(), "How to reduce WAL lag?", docs)
    assert len(hits) == 1 and hits[0].uri == "ctx://resources/good"
    assert await retriever.retrieve(SectionDB(), "", docs) == []
    assert await retriever.retrieve(SectionDB(), "how to", docs) == []
    assert await retriever.retrieve(SectionDB(), "wal", []) == []


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
async def test_independent_original_search_replaces_old_conditional_keyword_fallback():
    doc_id = uuid.uuid4()
    candidate = _doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=1.0)
    tree_strategy = DummyTreeStrategy(
        results_by_uri={
            "ctx://resources/manuals/one": [
                LongDocRetrievalResult(
                    context_id=doc_id,
                    uri="ctx://resources/manuals/one",
                    strategy="tree",
                    section_id=4,
                    snippet="broad benchmark introduction without the target phrase",
                    snippet_offset=(0, 52),
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
                snippet="The benchmark covers 57 subjects across STEM, the humanities, and the social sciences.",
                snippet_offset=(10, 96),
                relevance_score=1.4,
            )
        ]
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", tree_strategy)
    coordinator.register_strategy("keyword", keyword_strategy)

    merged = await coordinator.retrieve(
        SimpleNamespace(),
        "57 subjects across STEM humanities social sciences",
        [candidate],
    )

    assert tree_strategy.calls == [("ctx://resources/manuals/one", 1.0)]
    assert keyword_strategy.calls == []
    assert merged[0]["retrieval_strategy"] == "tree"
    merged = await coordinator.search_originals(SimpleNamespace(), "57 subjects", [candidate])
    assert len(keyword_strategy.calls) == 1
    assert [doc["uri"] for doc in keyword_strategy.calls[0]] == ["ctx://resources/manuals/one"]
    assert merged[0]["retrieval_strategy"] == "keyword"
    assert "57 subjects across STEM" in merged[0]["snippet"]


@pytest.mark.asyncio
async def test_coordinator_skips_keyword_fallback_when_tree_snippet_is_confident():
    doc_id = uuid.uuid4()
    candidate = _doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=1.0)
    tree_strategy = DummyTreeStrategy(
        results_by_uri={
            "ctx://resources/manuals/one": [
                LongDocRetrievalResult(
                    context_id=doc_id,
                    uri="ctx://resources/manuals/one",
                    strategy="tree",
                    section_id=4,
                    snippet="The benchmark covers 57 subjects across STEM, the humanities, and the social sciences.",
                    snippet_offset=(10, 96),
                    relevance_score=1.1,
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
                snippet="keyword fallback snippet",
                snippet_offset=(0, 24),
                relevance_score=3.0,
            )
        ]
    )
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", tree_strategy)
    coordinator.register_strategy("keyword", keyword_strategy)

    merged = await coordinator.retrieve(
        SimpleNamespace(),
        "The benchmark covers 57 subjects across STEM, the humanities, and the social sciences",
        [candidate],
    )

    assert keyword_strategy.calls == []
    assert merged[0]["retrieval_strategy"] == "tree"
    assert "57 subjects across STEM" in merged[0]["snippet"]


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
        RetrievalRouter(StubRerank()),
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
    assert response.trace["final_materialized"][0]["service_content"] == "[MASKED] snippet"
    assert response.results[0].section_id == 7
    assert response.results[0].retrieval_strategy == "tree"
    assert response.results[0].l2_content is None


@pytest.mark.asyncio
async def test_retrieval_service_filters_acl_before_precision(monkeypatch):
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
        RetrievalRouter(StubRerank()),
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
                    snippet="postgres replication tree snippet",
                    snippet_offset=(0, 33),
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
        RetrievalRouter(StubRerank()),
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
    assert response.results[0].snippet == "postgres replication tree snippet"


@pytest.mark.asyncio
async def test_retrieval_service_keeps_search_working_when_long_doc_precision_fails(monkeypatch):
    doc_id = uuid.uuid4()
    candidate = _doc_candidate(doc_id, uri="ctx://resources/manuals/one", file_path="/tmp/doc1", score=1.0)
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("tree", DummyTreeStrategy(failures={"ctx://resources/manuals/one"}))
    service = RetrievalService(
        RetrievalRouter(StubRerank()),
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
        RetrievalRouter(
            rerank_strategy=StubRerankWithScores(
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
        SearchRequest(query="postgres", top_k=2, include_stale=True),
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
                {"title":"Postgres Handbook","start_line":1,"start_text":"# Postgres Handbook","level":1,"summary":"whole"},
                {"title":"WAL Lag","start_line":3,"start_text":"## WAL Lag","level":2,"summary":"wal lag incidents"}
              ]
            }
            """
        ),
        embedding_client=NoOpEmbeddingClient(),
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


@pytest.mark.asyncio
async def test_keyword_search_scores_late_evidence_without_l0_score(tmp_path):
    text = ("noise " + "x" * 500 + "\n") * 12 + "批准码 23 Ada late-evidence"
    doc_dir = tmp_path / "original"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text(text)
    retriever = KeywordRetriever(max_snippet_chars=160)
    doc = _doc_candidate(uuid.uuid4(), uri="ctx://resources/evidence", file_path=str(doc_dir), score=0)
    results = await retriever.retrieve(SectionDB(), "noise first second third fourth 批准码 23 Ada late-evidence", [doc])
    assert len(results) > 1  # Keep other lexical candidates for range-level fusion.
    hit = results[0]
    assert "批准码 23 Ada" in hit.snippet
    assert hit.relevance_score > 0
    assert text[slice(*hit.snippet_offset)] == hit.snippet


@pytest.mark.asyncio
@pytest.mark.parametrize("version_changed", [False, True])
async def test_original_route_discovers_vector_miss_with_acl_and_version_barriers(tmp_path, monkeypatch, version_changed):
    from contexthub.services import retrieval_service as module

    doc_dir = tmp_path / "visible"
    doc_dir.mkdir()
    original = "Ordinary prose. " * 500 + "Ada approved 23 units in ZQX-804."
    (doc_dir / "extracted.txt").write_text(original)
    visible = _doc_candidate(uuid.uuid4(), uri="ctx://resources/visible", file_path=str(doc_dir), score=0)
    visible.update(l0_content="Document overview", l1_content=None, validity_status="fresh")
    hidden = {**visible, "id": uuid.uuid4(), "uri": "ctx://resources/hidden", "file_path": "/must-not-read"}

    class DB(RetrievalFlowDB):
        async def fetch(self, sql, *args):
            if "file_path IS NOT NULL" in sql:
                assert "account_id = current_setting" in sql and "LIMIT" not in sql
                assert args == (False, ["team"])
                return [visible, hidden]
            if "FROM document_sections" in sql:
                assert args == (visible["id"],)
                return [{"section_id": 42, "parent_id": None, "depth": 0,
                         "start_offset": 0, "end_offset": len(original)}]
            rows = await super().fetch(sql, *args)
            if version_changed and "SELECT id, version, status, validity_status" in sql:
                return [{**row, "version": 2} for row in rows]
            return rows

    class QueryEmbedding:
        async def embed(self, text):
            return [1.0]

    async def empty_vector(*args, **kwargs):
        return []

    keyword = KeywordRetriever()
    real_read = keyword._document_fragments
    async def guarded_read(db, doc):
        assert doc["id"] == visible["id"], "Denied documents must never be read"
        return await real_read(db, doc)
    monkeypatch.setattr(keyword, "_document_fragments", guarded_read)
    monkeypatch.setattr(module, "vector_search", empty_vector)
    coordinator = LongDocRetrievalCoordinator()
    coordinator.register_strategy("keyword", keyword)
    tree = DummyTreeStrategy()
    coordinator.register_strategy("tree", tree)
    service = RetrievalService(RetrievalRouter.default(), QueryEmbedding(), StubACL(visible_uris={visible["uri"]}),
                               masking_service=MaskingService(), long_doc_coordinator=coordinator)
    response = await service.search(DB([visible, hidden]), SearchRequest(query="ZQX 804", scope=["team"], top_k=1),
                                    RequestContext(account_id="acme", agent_id="query-agent"))
    assert tree.calls == []  # Original-only discoveries add no model navigation.
    if version_changed:
        assert response.results == []
    else:
        assert len(response.results) == 1
        result = response.results[0]
        assert result.uri == visible["uri"] and result.section_id == 42
        assert "ZQX-804" in result.snippet
        assert result.retrieval_strategy == "keyword"


def test_original_fusion_keeps_one_versioned_hit_and_original_evidence():
    from contexthub.services.retrieval_service import _fuse_original_hits
    first = _doc_candidate(uuid.uuid4(), uri="ctx://resources/one", file_path="/tmp/one")
    second = _doc_candidate(uuid.uuid4(), uri="ctx://resources/two", file_path="/tmp/two")
    original = {**first, "snippet": "raw evidence", "_rerank_score": 9999}
    fused = _fuse_original_hits([first, second], [original])
    assert len(fused) == 2
    evidence = next(hit for hit in fused if hit["id"] == first["id"])
    assert evidence["snippet"] == "raw evidence"
    assert evidence["_rerank_score"] == pytest.approx(1 / 61)
    assert all(hit["_rerank_score"] == pytest.approx(1 / 61) for hit in fused)


@pytest.mark.asyncio
async def test_table_only_search_does_not_scan_document_files():
    service = RetrievalService(RetrievalRouter.default(), NoOpEmbeddingClient(), StubACL(), masking_service=MaskingService())
    result = await service._search_document_originals(
        SimpleNamespace(), SearchRequest(query="constructors", context_type=["table_schema"]),
        RequestContext(account_id="acme", agent_id="query-agent"), ["table_schema"], None,
    )
    assert result == []


async def _section_keyword_result(tmp_path, text, sections, query, limit):
    folder = tmp_path / "document"
    folder.mkdir()
    (folder / "extracted.txt").write_text(text, encoding="utf-8", newline="")
    db = SectionDB(sections)
    candidate = _doc_candidate(uuid.uuid4(), uri="ctx://resources/section-boundaries", file_path=str(folder))
    retriever = KeywordRetriever(max_snippet_chars=limit)
    results = await retriever.retrieve(db, query, [candidate])
    assert len(results) == 1
    result = results[0]
    assert text[slice(*result.snippet_offset)] == result.snippet
    assert len(result.snippet) <= limit
    return result


def _stored_section(section_id, parent_id, depth, start, end):
    return {"section_id": section_id, "parent_id": parent_id, "depth": depth,
            "start_offset": start, "end_offset": end}


@pytest.mark.asyncio
async def test_fitting_chapter_is_complete_without_neighbor_or_title_wrapper(tmp_path):
    intro = "# Manual\n\nIntro.\n\n"
    chapter = "## Hardware\n\n1986 VAX 8800.\n\n### Details\nRelated background.\n\n"
    neighbor = "## Cache\nUnrelated material.\n"
    text = intro + chapter + neighbor
    start, end = len(intro), len(intro + chapter)
    sections = [
        _stored_section(1, None, 0, 0, len(text)),
        _stored_section(2, 1, 1, 0, len(text)),
        _stored_section(3, 2, 2, start, end),
        _stored_section(4, 3, 3, text.index("### Details"), end),
        _stored_section(5, 2, 2, end, len(text)),
    ]
    hit = await _section_keyword_result(tmp_path, text, sections, "1986 VAX 8800", 200)
    assert hit.snippet == chapter
    assert hit.snippet_offset == (start, end)
    assert hit.section_id == 3


@pytest.mark.asyncio
async def test_oversized_chapter_uses_complete_child_section(tmp_path):
    intro = "# Manual\n\n## Concepts\nParent introduction.\n\n"
    first = "### Pipelines\n\n1986 VAX 8800 used pipelines.\n\n"
    second = "### Cache\n" + "Other material. " * 20
    text = intro + first + second
    parent_start, start, end = text.index("## Concepts"), len(intro), len(intro + first)
    sections = [
        _stored_section(1, None, 0, 0, len(text)),
        _stored_section(2, 1, 1, 0, len(text)),
        _stored_section(3, 2, 2, parent_start, len(text)),
        _stored_section(4, 3, 3, start, end),
        _stored_section(5, 3, 3, end, len(text)),
    ]
    hit = await _section_keyword_result(tmp_path, text, sections, "1986 VAX 8800", 100)
    assert hit.snippet == first
    assert hit.section_id == 4


@pytest.mark.asyncio
async def test_oversized_leaf_prefers_complete_paragraphs(tmp_path):
    before = "# Notes\n\n" + "Opening context. " * 5 + "\n\n"
    evidence = "The VAX 8800 was pipelined by 1986.\n\n"
    text = before + evidence + "Closing context. " * 5
    sections = [_stored_section(1, None, 0, 0, len(text))]
    hit = await _section_keyword_result(tmp_path, text, sections, "1986 VAX 8800", 95)
    assert hit.snippet == evidence
    assert hit.snippet_offset == (len(before), len(before + evidence))
    assert hit.section_id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["english", "chinese"])
async def test_oversized_paragraph_uses_complete_sentences(tmp_path, language):
    if language == "english":
        before = "A background sentence contains no evidence. " * 4
        evidence = "The VAX 8800 was pipelined by 1986. "
        after = "Another background sentence follows this one. " * 4
        query = "1986 VAX 8800"
    else:
        before = "这是一段没有目标事实的背景介绍。" * 4
        evidence = "批准码为甲乙23，暂停11分钟。"
        after = "后面继续介绍另一项无关的操作流程。" * 4
        query = "甲乙23 11分钟"
    text = before + evidence + after
    hit = await _section_keyword_result(tmp_path, text, [], query, 65)
    assert evidence in hit.snippet
    start, end = hit.snippet_offset
    sentences = [before[:len(before)//4], evidence, after[:len(after)//4]]
    assert any(hit.snippet.startswith(sentence) for sentence in sentences)
    assert any(hit.snippet.endswith(sentence) for sentence in sentences)
    assert start == 0 or text[start - 1] in " 。"
    assert end == len(text) or text[end - 1] in " 。"


@pytest.mark.asyncio
async def test_oversized_sentence_splits_at_words(tmp_path):
    text = "background " * 12 + "VAX8800 " + "background " * 12
    hit = await _section_keyword_result(tmp_path, text, [], "VAX8800", 45)
    start, end = hit.snippet_offset
    assert "VAX8800" in hit.snippet
    assert start == 0 or text[start - 1].isspace()
    assert end == len(text) or text[end - 1].isspace()


@pytest.mark.asyncio
async def test_parent_intro_before_first_child_remains_retrievable(tmp_path):
    intro = "# Guide\n\n1986 VAX 8800.\n\n"
    child = "## Details\n" + "Other information. " * 20
    text = intro + child
    sections = [_stored_section(1, None, 0, 0, len(text)),
                _stored_section(2, 1, 1, len(intro), len(text))]
    hit = await _section_keyword_result(tmp_path, text, sections, "1986 VAX 8800", 80)
    assert hit.snippet == intro
    assert hit.section_id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('version_changed', [False, True])
async def test_chunk_vector_discovery_bypasses_l0_and_keywords_with_acl_and_version_checks(tmp_path, monkeypatch, version_changed):
    from hashlib import sha256
    from contexthub.retrieval.long_doc.vector_retriever import ChunkVectorRetriever
    from contexthub.services import retrieval_service as module

    text = 'secret original evidence with no literal query terms.'
    (tmp_path / 'extracted.txt').write_text(text)
    visible = _doc_candidate(uuid.uuid4(), uri='ctx://resources/visible-vector', file_path=str(tmp_path))
    visible.update(l0_content='Unrelated overview', l1_content=None, validity_status='fresh')
    hidden = {**visible, 'id':uuid.uuid4(), 'uri':'ctx://resources/hidden-vector', 'file_path':'/must-not-read'}
    class DB(RetrievalFlowDB):
        async def fetch(self, sql, *args):
            if 'file_path IS NOT NULL' in sql:
                return [visible,hidden]
            if 'FROM document_chunk_embeddings' in sql:
                assert args[1] == [visible['id']], 'ACL must filter before vector/file reads'
                return [dict(context_id=visible['id'], context_version=visible['version'],section_id=42,
                             start_offset=0,end_offset=len(text),source_sha256=sha256(text.encode()).hexdigest(),similarity=.8)]
            rows=await super().fetch(sql,*args)
            if version_changed and 'SELECT id, version, status, validity_status' in sql:
                return [{**r,'version':r['version']+1} for r in rows]
            return rows
    class Embedding:
        calls=0
        async def embed(self, query):
            self.calls+=1
            return [1.]
    async def no_l0_candidates(*args, **kwargs): return []
    monkeypatch.setattr(module,'vector_search',no_l0_candidates)
    coordinator=LongDocRetrievalCoordinator()
    coordinator.register_strategy('vector',ChunkVectorRetriever())
    embedding=Embedding()
    service=RetrievalService(RetrievalRouter.default(),embedding,
        StubACL(masks=['secret'],visible_uris={visible['uri']}),masking_service=MaskingService(),long_doc_coordinator=coordinator)
    result=await service.search(DB([visible,hidden]),SearchRequest(query='paraphrased request',top_k=1),
        RequestContext(account_id='acme',agent_id='query-agent'))
    assert embedding.calls==1
    if version_changed:
        assert result.results==[]
    else:
        assert len(result.results)==1
        hit=result.results[0]
        assert hit.retrieval_strategy=='vector' and hit.section_id==42
        assert hit.snippet=='[MASKED] original evidence with no literal query terms.'



def test_outer_fusion_preserves_multiple_original_ranges_and_drops_tree_representative():
    from contexthub.services.retrieval_service import _fuse_original_hits
    doc = _doc_candidate(uuid.uuid4(), uri="ctx://resources/ranges", file_path="/tmp/ranges")
    tree = {**doc, "snippet": "overlapping tree", "snippet_offset": (2, 10)}
    originals = [
        {**doc, "snippet": "first", "snippet_offset": (0, 5), "_rerank_score": .03},
        {**doc, "snippet": "second", "snippet_offset": (6, 12), "_rerank_score": .02},
    ]
    # A single remaining route keeps its existing range scores.
    assert _fuse_original_hits([], originals) == originals
    assert _fuse_original_hits([tree], originals) == originals
    other = _doc_candidate(uuid.uuid4(), uri="ctx://resources/other", file_path=None)
    fused = _fuse_original_hits([tree, other], originals)
    assert len(fused) == 3
    hits = [hit for hit in fused if hit["id"] == doc["id"]]
    assert [hit["snippet_offset"] for hit in hits] == [(0, 5), (6, 12)]
    assert [hit["_rerank_score"] for hit in hits] == pytest.approx([1/61, 1/62])
    assert all(hit.get("snippet") != "overlapping tree" for hit in fused)


@pytest.mark.asyncio
@pytest.mark.parametrize("level,top_k,changed", [
    ("L1", 1, False), ("L1", 2, False), ("L1", 3, False),
    ("L1", 3, True), ("L2", 3, False), ("L0", 3, False),
])
async def test_global_passage_topk_with_shared_sections_masks_and_version_barrier(monkeypatch, level, top_k, changed):
    from dataclasses import replace
    from contexthub.services import retrieval_service as module
    a = _doc_candidate(uuid.uuid4(), uri="ctx://resources/a", file_path="/source/a")
    b = _doc_candidate(uuid.uuid4(), uri="ctx://resources/b", file_path="/source/b")
    hidden = _doc_candidate(uuid.uuid4(), uri="ctx://resources/hidden", file_path="/must-not-read")
    for doc in (a, b, hidden): doc["validity_status"] = "fresh"
    ranges = [
        LongDocRetrievalResult(a['id'], a['uri'], 'keyword', 7, 'secret A', (0, 8), 3),
        LongDocRetrievalResult(b['id'], b['uri'], 'keyword', 8, 'secret B', (0, 8), 2),
        LongDocRetrievalResult(a['id'], a['uri'], 'keyword', 7, 'secret C', (9, 17), 1),
    ]
    class DB(RetrievalFlowDB):
        async def fetch(self, sql, *args):
            if 'file_path IS NOT NULL' in sql: return [a, b, hidden]
            if 'SELECT id, version, l2_content' in sql:
                return [dict(id=d['id'],version=d['version'],l2_content=None) for d in [a,b]]
            rows = await super().fetch(sql,*args)
            if changed and 'SELECT id, version, status, validity_status' in sql:
                return [{**r,'version':r['version']+1} if r['id']==a['id'] else r for r in rows]
            return rows
    class Keyword:
        async def retrieve(self, db, query, documents):
            assert [d['id'] for d in documents] == [a['id'],b['id']]
            return ranges
    class Vector:
        async def retrieve(self, db, vector, documents, budget):
            assert budget == top_k * 3
            return [replace(r,strategy='vector',relevance_score=1-i*.1) for i,r in enumerate(ranges)]
    class Embedding:
        calls=0
        async def embed(self, query): self.calls+=1; return [1.]
    async def empty(*args, **kwargs): return []
    monkeypatch.setattr(module,'vector_search',empty)
    coordinator=LongDocRetrievalCoordinator()
    coordinator.register_strategy('keyword',Keyword())
    coordinator.register_strategy('vector',Vector())
    embedding=Embedding()
    service=RetrievalService(RetrievalRouter.default(),embedding,
        StubACL(masks=['secret'],visible_uris={a['uri'],b['uri']}),
        masking_service=MaskingService(),long_doc_coordinator=coordinator)
    result=await service.search(DB([a,b,hidden]),SearchRequest(query='evidence',level=level,top_k=top_k),
        RequestContext(account_id='acme',agent_id='query-agent'))
    assert embedding.calls==1
    expected = [ranges[1]] if changed else ranges[:2] if level=='L0' else ranges[:top_k]
    assert [(h.uri,h.snippet_offset) for h in result.results] == [(r.uri,r.snippet_offset) for r in expected]
    assert len({(h.uri,h.version,h.snippet_offset) for h in result.results}) == len(result.results)
    for hit in result.results:
        assert hit.retrieval_strategy=='hybrid' and 'secret' not in hit.snippet
        if level=='L0': assert hit.l0_content == a['l0_content']
        else: assert hit.l0_content is hit.l1_content is hit.l2_content is None
    assert len(result.trace['final_materialized']) == len(expected)
    assert [v['snippet_offset'] for v in result.trace['final_versions']] == [r.snippet_offset for r in expected]
    if changed:
        removed=[c for c in result.trace['candidates'] if c['node_id']==str(a['id'])]
        assert len(removed)==2
        assert all(c['filtered'] and c['filter_reasons']==['materialization_version_changed'] for c in removed)
