from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from contexthub.api.routers.documents import router as documents_router
from contexthub.models.document import DocumentIngestResponse
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService


class _RepoSession:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeRepo:
    def __init__(self, db):
        self._db = db

    def session(self, account_id):
        return _RepoSession(self._db)


class StubIngester:
    def __init__(self, *, result: DocumentIngestResponse | None = None, exc: Exception | None = None):
        self.result = result or DocumentIngestResponse(
            context_id=uuid.uuid4(),
            uri="ctx://resources/manuals/test",
            section_count=2,
            file_path="/tmp/doc",
        )
        self.exc = exc
        self.calls = []

    async def ingest(self, db, uri, source_path, ctx, tags=None):
        self.calls.append(
            {
                "db": db,
                "uri": uri,
                "source_path": source_path,
                "ctx": ctx,
                "tags": tags,
                "source_exists_during_call": Path(source_path).exists(),
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.result


async def _insert_document_context(
    db,
    uri: str,
    file_path: str,
    *,
    status: str = "active",
    stale_at=None,
):
    row = await db.fetchrow(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            status, l0_content, file_path, stale_at
        )
        VALUES (
            $1, $2, 'resource', 'team', 'engineering/backend',
            current_setting('app.account_id'), $4, 'doc fixture', $3, $5
        )
        RETURNING id
        """,
        uuid.uuid4(),
        uri,
        file_path,
        status,
        stale_at,
    )
    return row["id"]


async def _insert_section(
    db,
    context_id,
    *,
    section_id: int,
    parent_id: int | None,
    title: str,
    depth: int,
    start_offset: int,
    end_offset: int,
    summary: str,
):
    await db.execute(
        """
        INSERT INTO document_sections (
            section_id, context_id, parent_id, node_id, title, depth,
            start_offset, end_offset, summary, token_count, account_id
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, 10, current_setting('app.account_id')
        )
        """,
        section_id,
        context_id,
        parent_id,
        f"node-{section_id}",
        title,
        depth,
        start_offset,
        end_offset,
        summary,
    )


async def _insert_policy(db, pattern: str, principal: str, effect: str, field_masks: list[str] | None = None):
    await db.execute(
        """
        INSERT INTO access_policies (
            resource_uri_pattern, principal, effect, actions,
            conditions, field_masks, priority, account_id, created_by
        )
        VALUES (
            $1, $2, $3, ARRAY['read']::text[],
            NULL, $4, 10, current_setting('app.account_id'), 'test'
        )
        """,
        pattern,
        principal,
        effect,
        field_masks,
    )


@pytest_asyncio.fixture
async def documents_http_client():
    async def factory(
        *,
        repo,
        ingester,
        acl_service=None,
        masking_service=None,
        lifecycle_service=None,
        audit_service=None,
    ):
        app = FastAPI()
        app.include_router(documents_router)
        app.state.repo = repo
        app.state.document_ingester = ingester
        app.state.acl_service = acl_service or ACLService()
        app.state.masking_service = masking_service or MaskingService()
        app.state.audit_service = audit_service or AuditService()
        app.state.lifecycle_service = lifecycle_service or LifecycleService(
            audit=app.state.audit_service
        )
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        return client

    yield factory


@pytest.mark.asyncio
async def test_document_ingest_uses_multipart_repeated_tags_and_cleans_temp_file(documents_http_client):
    ingester = StubIngester()
    sentinel_db = object()
    client = await documents_http_client(repo=FakeRepo(sentinel_db), ingester=ingester)
    try:
        response = await client.post(
            "/api/v1/documents/ingest",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            files=[
                ("uri", (None, "ctx://resources/manuals/test")),
                ("tags", (None, "alpha")),
                ("tags", (None, "beta")),
                ("file", ("manual.txt", b"hello world", "text/plain")),
            ],
        )
    finally:
        await client.aclose()

    assert response.status_code == 201
    assert ingester.calls
    call = ingester.calls[0]
    assert call["db"] is sentinel_db
    assert call["uri"] == "ctx://resources/manuals/test"
    assert call["tags"] == ["alpha", "beta"]
    assert isinstance(call["ctx"], RequestContext)
    assert call["source_exists_during_call"] is True
    assert not Path(call["source_path"]).exists()


@pytest.mark.asyncio
async def test_document_ingest_returns_service_unavailable(documents_http_client):
    from contexthub.errors import ServiceUnavailableError

    client = await documents_http_client(
        repo=FakeRepo(object()),
        ingester=StubIngester(exc=ServiceUnavailableError("Long document ingestion requires a configured LLM API key")),
    )
    try:
        response = await client.post(
            "/api/v1/documents/ingest",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
            files=[
                ("uri", (None, "ctx://resources/manuals/test")),
                ("file", ("manual.txt", b"hello world", "text/plain")),
            ],
        )
    finally:
        await client.aclose()

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_document_sections_returns_only_requested_document_and_public_shape(
    documents_http_client,
    repo,
    clean_db,
    tmp_path: Path,
):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    (first_dir / "extracted.txt").write_text("first document text", encoding="utf-8")
    (second_dir / "extracted.txt").write_text("second document text", encoding="utf-8")

    async with repo.session("acme") as db:
        first_id = await _insert_document_context(db, "ctx://resources/manuals/first", str(first_dir))
        second_id = await _insert_document_context(db, "ctx://resources/manuals/second", str(second_dir))
        await _insert_section(
            db,
            first_id,
            section_id=1,
            parent_id=None,
            title="Doc",
            depth=0,
            start_offset=0,
            end_offset=5,
            summary="overview",
        )
        await _insert_section(
            db,
            second_id,
            section_id=2,
            parent_id=None,
            title="Other",
            depth=0,
            start_offset=0,
            end_offset=6,
            summary="other",
        )

    client = await documents_http_client(
        repo=repo,
        ingester=StubIngester(),
        acl_service=ACLService(),
        masking_service=MaskingService(),
    )
    try:
        response = await client.get(
            f"/api/v1/documents/{first_id}/sections",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["section_id"] == 1
    assert set(body[0].keys()) == {
        "section_id",
        "parent_id",
        "title",
        "depth",
        "summary",
        "start_offset",
        "end_offset",
        "token_count",
    }


@pytest.mark.asyncio
async def test_document_sections_enforce_acl(
    documents_http_client,
    repo,
    clean_db,
    tmp_path: Path,
):
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text("masked document text", encoding="utf-8")

    async with repo.session("acme") as db:
        context_id = await _insert_document_context(db, "ctx://resources/manuals/locked", str(doc_dir))
        await _insert_section(
            db,
            context_id,
            section_id=1,
            parent_id=None,
            title="Doc",
            depth=0,
            start_offset=0,
            end_offset=6,
            summary="overview",
        )
        await _insert_policy(db, "ctx://resources/manuals/locked", "query-agent", "deny")

    client = await documents_http_client(
        repo=repo,
        ingester=StubIngester(),
        acl_service=ACLService(),
        masking_service=MaskingService(),
    )
    try:
        response = await client.get(
            f"/api/v1/documents/{context_id}/sections",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_document_read_section_applies_field_masks(
    documents_http_client,
    repo,
    clean_db,
    tmp_path: Path,
):
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    content = "public secret details"
    (doc_dir / "extracted.txt").write_text(content, encoding="utf-8")

    async with repo.session("acme") as db:
        context_id = await _insert_document_context(db, "ctx://resources/manuals/masked", str(doc_dir))
        await _insert_section(
            db,
            context_id,
            section_id=7,
            parent_id=None,
            title="Sensitive",
            depth=0,
            start_offset=0,
            end_offset=len(content),
            summary="summary",
        )
        await _insert_policy(db, "ctx://resources/manuals/masked", "query-agent", "allow", ["secret"])

    client = await documents_http_client(
        repo=repo,
        ingester=StubIngester(),
        acl_service=ACLService(),
        masking_service=MaskingService(),
    )
    try:
        response = await client.get(
            f"/api/v1/documents/{context_id}/section/7",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["section_id"] == 7
    assert body["content"] == "public [MASKED] details"


@pytest.mark.asyncio
async def test_document_read_section_reuses_store_side_effects(
    documents_http_client,
    repo,
    clean_db,
    services,
    tmp_path: Path,
):
    doc_dir = tmp_path / "stale-doc"
    doc_dir.mkdir()
    content = "stale document body"
    (doc_dir / "extracted.txt").write_text(content, encoding="utf-8")

    async with repo.session("acme") as db:
        context_id = await _insert_document_context(
            db,
            "ctx://resources/manuals/stale-doc",
            str(doc_dir),
            status="stale",
            stale_at=await db.fetchval("SELECT NOW() - INTERVAL '3 days'"),
        )
        await _insert_section(
            db,
            context_id,
            section_id=9,
            parent_id=None,
            title="Body",
            depth=0,
            start_offset=0,
            end_offset=len(content),
            summary="summary",
        )

    client = await documents_http_client(
        repo=repo,
        ingester=StubIngester(),
        acl_service=services.acl,
        masking_service=services.masking,
        lifecycle_service=services.lifecycle,
        audit_service=services.audit,
    )
    try:
        response = await client.get(
            f"/api/v1/documents/{context_id}/section/9",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    async with repo.session("acme") as db:
        row = await db.fetchrow(
            "SELECT status, stale_at, last_accessed_at FROM contexts WHERE id = $1",
            context_id,
        )
        audit_count = await db.fetchval(
            """
            SELECT COUNT(*)
            FROM audit_log
            WHERE resource_uri = $1 AND action = 'read'
            """,
            "ctx://resources/manuals/stale-doc",
        )
        audit_meta = await db.fetchval(
            """
            SELECT metadata
            FROM audit_log
            WHERE resource_uri = $1 AND action = 'read'
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            "ctx://resources/manuals/stale-doc",
        )
    assert row["status"] == "active"
    assert row["stale_at"] is None
    assert row["last_accessed_at"] is not None
    assert audit_count == 1
    assert audit_meta is not None
    assert audit_meta.get("section_id") == 9


@pytest.mark.asyncio
async def test_document_read_section_returns_only_requested_slice(
    documents_http_client,
    repo,
    clean_db,
    tmp_path: Path,
):
    doc_dir = tmp_path / "slice-doc"
    doc_dir.mkdir()
    content = "intro_part|section_body|outro_part"
    (doc_dir / "extracted.txt").write_text(content, encoding="utf-8")

    async with repo.session("acme") as db:
        context_id = await _insert_document_context(
            db, "ctx://resources/manuals/slice", str(doc_dir)
        )
        await _insert_section(
            db,
            context_id,
            section_id=3,
            parent_id=None,
            title="Body",
            depth=0,
            start_offset=11,
            end_offset=24,
            summary="body",
        )

    client = await documents_http_client(
        repo=repo,
        ingester=StubIngester(),
        acl_service=ACLService(),
        masking_service=MaskingService(),
    )
    try:
        response = await client.get(
            f"/api/v1/documents/{context_id}/section/3",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "section_body|"
    assert body["start_offset"] == 11
    assert body["end_offset"] == 24
    assert "intro_part" not in body["content"]
    assert "outro_part" not in body["content"]


@pytest.mark.asyncio
async def test_document_read_section_rejects_out_of_range_offset(
    documents_http_client,
    repo,
    clean_db,
    tmp_path: Path,
):
    doc_dir = tmp_path / "short-doc"
    doc_dir.mkdir()
    (doc_dir / "extracted.txt").write_text("tiny", encoding="utf-8")

    async with repo.session("acme") as db:
        context_id = await _insert_document_context(
            db, "ctx://resources/manuals/short", str(doc_dir)
        )
        await _insert_section(
            db,
            context_id,
            section_id=1,
            parent_id=None,
            title="Body",
            depth=0,
            start_offset=0,
            end_offset=999,
            summary="body",
        )

    client = await documents_http_client(
        repo=repo,
        ingester=StubIngester(),
        acl_service=ACLService(),
        masking_service=MaskingService(),
    )
    try:
        response = await client.get(
            f"/api/v1/documents/{context_id}/section/1",
            headers={"X-Account-Id": "acme", "X-Agent-Id": "query-agent"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
