from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from fastapi import FastAPI

from contexthub.config import Settings
from contexthub.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    ServiceUnavailableError,
)
from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.llm.chat_client import NoOpChatClient, OpenAIChatClient
from contexthub.llm.factory import create_chat_client
from contexthub.models.context import ContextLevel, Scope
from contexthub.models.request import RequestContext
from contexthub.services.access_decision import AccessDecision
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.document_ingester import (
    TREE_PROMPT_CHAR_LIMIT,
    LongDocumentIngester,
    _pdf_to_markdownish_text,
    build_bounded_tree_prompt,
    doc_dir_key,
)
from contexthub.store.context_store import ContextStore
import contexthub.main as main_module


ROOT_TEAM_ID = "00000000-0000-0000-0000-000000000001"


class FakeAudit:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    async def log_strict(
        self,
        db,
        actor: str,
        action: str,
        resource_uri: str | None,
        result: str,
        context_used: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self.fail:
            raise RuntimeError("audit failed")
        self.calls.append(
            {
                "actor": actor,
                "action": action,
                "resource_uri": resource_uri,
                "result": result,
                "metadata": metadata,
            }
        )


class FakeACL:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls: list[tuple] = []

    async def check_write_target(self, db, scope, owner_space, ctx):
        self.calls.append((scope, owner_space, ctx.agent_id))
        return self.allowed


class FakeEmbedding:
    def __init__(self, result=None, *, exc: Exception | None = None):
        self.result = result
        self.exc = exc
        self.calls: list[str] = []

    async def embed(self, text: str):
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.result


class ScriptedChatClient:
    def __init__(self, response: str | None = None, *, exc: Exception | None = None):
        self.response = response or ""
        self.exc = exc
        self.prompts: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.response


class IngestDB:
    def __init__(self, *, duplicate_exists: bool = False, fail_stage: str | None = None):
        self.duplicate_exists = duplicate_exists
        self.fail_stage = fail_stage
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.fetchval_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.context_insert_args: tuple | None = None
        self.section_insert_args: list[tuple] = []
        self.context_id = uuid.uuid4()
        self.section_counter = 0

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        if "INSERT INTO contexts" in sql:
            self.context_insert_args = args
            if self.fail_stage == "context_insert":
                raise RuntimeError("context insert failed")
            if self.fail_stage == "context_conflict":
                raise RuntimeError("duplicate key value violates unique constraint")
            return {"id": self.context_id}
        return None

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        if "SELECT 1 FROM contexts WHERE uri = $1" in sql:
            return 1 if self.duplicate_exists else None
        if "RETURNING section_id" in sql:
            if self.fail_stage == "section_insert":
                raise RuntimeError("section insert failed")
            self.section_counter += 1
            self.section_insert_args.append(args)
            return self.section_counter
        return None

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        if "INSERT INTO change_events" in sql and self.fail_stage == "change_event":
            raise RuntimeError("change event failed")
        return "INSERT 0 1"


def _make_ingester(
    tmp_path: Path,
    *,
    chat_client=None,
    embedding=None,
    acl=None,
    audit=None,
    max_document_size_mb: int = 50,
    max_token_per_node: int = 2000,
) -> LongDocumentIngester:
    return LongDocumentIngester(
        chat_client=chat_client or ScriptedChatClient(
            response="""
            {
              "sections": [
                {
                  "node_id": "root",
                  "parent_node_id": null,
                  "title": "Document",
                  "start_offset": 0,
                  "end_offset": 40,
                  "summary": "Overview"
                }
              ]
            }
            """
        ),
        embedding_client=embedding or FakeEmbedding([0.1, 0.2]),
        content_generator=ContentGenerator(),
        acl=acl or FakeACL(True),
        audit=audit or FakeAudit(),
        doc_store_root=str(tmp_path / "docs"),
        max_document_size_mb=max_document_size_mb,
        max_token_per_node=max_token_per_node,
    )


def _create_text_file(tmp_path: Path, name: str = "source.txt", content: str = "hello world") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _flatten(node) -> list:
    nodes = [node]
    for child in node.children:
        nodes.extend(_flatten(child))
    return nodes


@pytest.mark.asyncio
async def test_create_chat_client_returns_openai_when_api_key_present():
    client = create_chat_client(Settings(openai_api_key="sk-test"))
    assert isinstance(client, OpenAIChatClient)
    assert client._model == "gpt-4o-mini"
    await client.close()


def test_create_chat_client_returns_noop_when_api_key_missing():
    client = create_chat_client(Settings(openai_api_key=""))
    assert isinstance(client, NoOpChatClient)


@pytest.mark.asyncio
async def test_create_chat_client_uses_configured_model_and_base_url():
    client = create_chat_client(
        Settings(
            openai_api_key="sk-test",
            openai_base_url="https://example.com/v1/",
            chat_model="custom-chat-model",
        )
    )
    assert isinstance(client, OpenAIChatClient)
    assert client._model == "custom-chat-model"
    assert str(client._client.base_url) == "https://example.com/v1/"
    await client.close()


@pytest.mark.asyncio
async def test_ingest_requires_configured_llm_api_key(tmp_path: Path):
    ingester = LongDocumentIngester(
        chat_client=NoOpChatClient(),
        embedding_client=NoOpEmbeddingClient(),
        content_generator=ContentGenerator(),
        acl=FakeACL(True),
        audit=FakeAudit(),
        doc_store_root=str(tmp_path / "docs"),
    )

    with pytest.raises(ServiceUnavailableError, match="Long document ingestion requires a configured LLM API key"):
        await ingester.ingest(
            IngestDB(),
            "ctx://resources/manuals/test",
            str(_create_text_file(tmp_path)),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "prepare"),
    [
        ("missing.txt", None),
        ("directory", "dir"),
    ],
)
async def test_ingest_rejects_missing_or_non_file_source(tmp_path: Path, name: str, prepare: str | None):
    ingester = _make_ingester(tmp_path)
    source = tmp_path / name
    if prepare == "dir":
        source.mkdir()

    with pytest.raises(BadRequestError):
        await ingester.ingest(
            IngestDB(),
            "ctx://resources/manuals/test",
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
async def test_ingest_rejects_source_when_stat_fails(tmp_path: Path, monkeypatch):
    ingester = _make_ingester(tmp_path)
    source = _create_text_file(tmp_path, "stat-fail.txt", "hello")
    original_stat = Path.stat

    def broken_stat(self: Path):
        if self == source:
            raise OSError("boom")
        return original_stat(self)

    monkeypatch.setattr(Path, "stat", broken_stat)

    with pytest.raises(BadRequestError):
        await ingester.ingest(
            IngestDB(),
            "ctx://resources/manuals/test",
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
async def test_ingest_rejects_non_resource_uri(tmp_path: Path):
    ingester = _make_ingester(tmp_path)
    source = _create_text_file(tmp_path)

    with pytest.raises(BadRequestError, match="ctx://resources/"):
        await ingester.ingest(
            IngestDB(),
            "ctx://team/engineering/doc",
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
async def test_ingest_checks_root_team_write_permission(tmp_path: Path):
    ingester = _make_ingester(tmp_path, acl=FakeACL(False))
    source = _create_text_file(tmp_path)

    with pytest.raises(ForbiddenError):
        await ingester.ingest(
            IngestDB(),
            "ctx://resources/manuals/test",
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
async def test_duplicate_uri_is_blocked_before_filesystem_write(tmp_path: Path):
    ingester = _make_ingester(tmp_path)
    source = _create_text_file(tmp_path)
    uri = "ctx://resources/manuals/test"
    final_dir = Path(ingester._doc_store_root) / doc_dir_key("acme", uri)

    with pytest.raises(ConflictError):
        await ingester.ingest(
            IngestDB(duplicate_exists=True),
            uri,
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )

    assert not final_dir.exists()


@pytest.mark.asyncio
async def test_successful_ingest_writes_files_and_database_side_effects(tmp_path: Path):
    source = _create_text_file(
        tmp_path,
        content="# Intro\nhello world\n\n## Details\nsecond section\n",
    )
    audit = FakeAudit()
    db = IngestDB()
    ingester = _make_ingester(tmp_path, audit=audit)
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await ingester.ingest(db, "ctx://resources/manuals/test", str(source), ctx, tags=["doc"])

    final_dir = Path(result.file_path)
    assert final_dir.exists()
    assert (final_dir / "source.txt").exists()
    assert (final_dir / "extracted.txt").exists()
    assert (final_dir / "extracted.md").exists()
    assert result.context_id == db.context_id
    assert result.section_count >= 1
    assert db.context_insert_args is not None
    assert db.context_insert_args[3] == result.file_path
    assert db.context_insert_args[4] == ["doc"]
    assert db.context_insert_args[5] == "[0.1,0.2]"
    assert any("INSERT INTO change_events" in sql for sql, _args in db.execute_calls)
    assert len(db.section_insert_args) >= 1
    assert audit.calls and audit.calls[0]["action"] == "create"


@pytest.mark.asyncio
async def test_ingest_persists_absolute_file_path_and_l2_read_survives_cwd_change(
    tmp_path: Path,
    monkeypatch,
):
    run_dir = tmp_path / "run"
    later_dir = tmp_path / "later"
    run_dir.mkdir()
    later_dir.mkdir()
    source = _create_text_file(tmp_path, content="portable content")

    monkeypatch.chdir(run_dir)
    ingester = LongDocumentIngester(
        chat_client=ScriptedChatClient(
            response="""
            {
              "sections": [
                {
                  "node_id": "root",
                  "parent_node_id": null,
                  "title": "Document",
                  "start_offset": 0,
                  "end_offset": 16,
                  "summary": "Overview"
                }
              ]
            }
            """
        ),
        embedding_client=FakeEmbedding([0.1, 0.2]),
        content_generator=ContentGenerator(),
        acl=FakeACL(True),
        audit=FakeAudit(),
        doc_store_root="relative-docs",
    )
    db = IngestDB()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    result = await ingester.ingest(db, "ctx://resources/manuals/portable", str(source), ctx)

    assert Path(result.file_path).is_absolute()
    assert db.context_insert_args is not None
    assert db.context_insert_args[3] == result.file_path

    monkeypatch.chdir(later_dir)

    class FakeReadDB:
        async def fetchrow(self, sql, *args):
            return {
                "id": uuid.uuid4(),
                "status": "active",
                "l0_content": "db l0",
                "l1_content": "db l1",
                "l2_content": None,
                "file_path": result.file_path,
            }

        async def fetchval(self, sql, *args):
            return 1

        async def execute(self, sql, *args):
            return "UPDATE 1"

    store = ContextStore(
        SimpleNamespace(
            check_read_access=AsyncMock(
                return_value=AccessDecision(allowed=True, field_masks=None, reason="ok")
            )
        ),
        SimpleNamespace(apply_masks=lambda content, masks: content),
        audit=SimpleNamespace(log_best_effort=AsyncMock()),
    )
    assert await store.read(
        FakeReadDB(),
        result.uri,
        ContextLevel.L2,
        ctx,
    ) == "portable content"


def test_doc_dir_key_is_tenant_aware():
    uri = "ctx://resources/manuals/shared"
    assert doc_dir_key("acme", uri) != doc_dir_key("globex", uri)


@pytest.mark.asyncio
async def test_preexisting_final_dir_conflicts_without_deleting_existing_files(tmp_path: Path):
    ingester = _make_ingester(tmp_path)
    source = _create_text_file(tmp_path)
    uri = "ctx://resources/manuals/test"
    final_dir = Path(ingester._doc_store_root) / doc_dir_key("acme", uri)
    final_dir.mkdir(parents=True)
    sentinel = final_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ConflictError):
        await ingester.ingest(
            IngestDB(),
            uri,
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )

    assert sentinel.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", ["context_insert", "section_insert", "change_event", "audit"])
async def test_failure_cleanup_removes_only_new_final_dir(tmp_path: Path, fail_stage: str):
    uri = "ctx://resources/manuals/test"
    source = _create_text_file(tmp_path, content="# Intro\nhello")
    audit = FakeAudit(fail=fail_stage == "audit")
    db = IngestDB(fail_stage=fail_stage if fail_stage != "audit" else None)
    ingester = _make_ingester(tmp_path, audit=audit)
    final_dir = Path(ingester._doc_store_root) / doc_dir_key("acme", uri)

    with pytest.raises(RuntimeError):
        await ingester.ingest(
            db,
            uri,
            str(source),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )

    assert not final_dir.exists()


@pytest.mark.asyncio
async def test_embedding_failures_do_not_block_ingest(tmp_path: Path):
    source = _create_text_file(tmp_path)
    db_none = IngestDB()
    ingester_none = _make_ingester(tmp_path / "none", embedding=FakeEmbedding(None))
    await ingester_none.ingest(
        db_none,
        "ctx://resources/manuals/none",
        str(source),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert db_none.context_insert_args[5] is None

    db_exc = IngestDB()
    ingester_exc = _make_ingester(tmp_path / "exc", embedding=FakeEmbedding(exc=RuntimeError("embed fail")))
    await ingester_exc.ingest(
        db_exc,
        "ctx://resources/manuals/exc",
        str(source),
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert db_exc.context_insert_args[5] is None


@pytest.mark.asyncio
async def test_extract_text_supports_txt_and_md(tmp_path: Path):
    ingester = _make_ingester(tmp_path)
    final_dir = tmp_path / "out"
    final_dir.mkdir()

    txt_source = final_dir / "source.txt"
    txt_source.write_text("plain text", encoding="utf-8")
    plain_txt, markdown_txt = ingester._extract_text(txt_source, final_dir)
    assert plain_txt == "plain text"
    assert markdown_txt == "plain text"

    md_source = final_dir / "source.md"
    md_source.write_text("# Title\nbody", encoding="utf-8")
    plain_md, markdown_md = ingester._extract_text(md_source, final_dir)
    assert plain_md == "# Title\nbody"
    assert markdown_md == "# Title\nbody"


@pytest.mark.asyncio
async def test_extract_text_supports_pdf(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "source.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "PDF Hello")
    document.save(pdf_path)
    document.close()

    ingester = _make_ingester(tmp_path)
    final_dir = tmp_path / "pdf-out"
    final_dir.mkdir()
    plain_text, markdown_text = ingester._extract_text(pdf_path, final_dir)

    assert "PDF Hello" in plain_text
    assert "PDF Hello" in markdown_text
    assert (final_dir / "extracted.txt").exists()
    assert (final_dir / "extracted.md").exists()


def test_pdf_to_markdownish_text_recovers_headings_and_drops_repeated_headers():
    plain_text = "\n".join(
        [
            "Published as a conference paper at ICLR 2021",
            "MEASURING MASSIVE MULTITASK",
            "LANGUAGE UNDERSTANDING",
            "UIUC",
            "",
            "Published as a conference paper at ICLR 2021",
            "ABSTRACT",
            "We propose a new benchmark.",
            "",
            "Published as a conference paper at ICLR 2021",
            "3.1",
            "HUMANITIES",
            "Law and philosophy.",
            "57 subjects across STEM, the humanities, the social sciences, and more.",
            "7",
        ]
    )

    markdownish = _pdf_to_markdownish_text(plain_text)

    assert "# MEASURING MASSIVE MULTITASK LANGUAGE UNDERSTANDING" in markdownish
    assert "## ABSTRACT" in markdownish
    assert "### 3.1 HUMANITIES" in markdownish
    assert "Published as a conference paper at ICLR 2021" not in markdownish
    assert "## UIUC" not in markdownish
    assert "## 57 subjects across STEM" not in markdownish


@pytest.mark.asyncio
async def test_ingest_rejects_unsupported_extension_and_oversized_file(tmp_path: Path):
    source_docx = _create_text_file(tmp_path, "source.docx", "not supported")
    ingester = _make_ingester(tmp_path / "docx")

    with pytest.raises(BadRequestError, match="Unsupported document type"):
        await ingester.ingest(
            IngestDB(),
            "ctx://resources/manuals/docx",
            str(source_docx),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )

    oversized = _create_text_file(tmp_path, "big.txt", "0123456789")
    tiny_ingester = _make_ingester(tmp_path / "tiny", max_document_size_mb=0)
    with pytest.raises(BadRequestError, match="max_document_size_mb"):
        await tiny_ingester.ingest(
            IngestDB(),
            "ctx://resources/manuals/big",
            str(oversized),
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
async def test_build_document_tree_parses_valid_json_and_code_fences(tmp_path: Path):
    raw_json = """
    ```json
    {
      "sections": [
        {
          "node_id": "root",
          "parent_node_id": null,
          "title": "Document",
          "start_offset": 0,
          "end_offset": 11,
          "summary": "Overview"
        },
        {
          "node_id": "child",
          "parent_node_id": "root",
          "title": "Child",
          "start_offset": 0,
          "end_offset": 11,
          "summary": "Child summary"
        }
      ]
    }
    ```
    """
    ingester = _make_ingester(tmp_path, chat_client=ScriptedChatClient(response=raw_json))
    tree = await ingester.build_document_tree("# Child\nhello world", "hello world")

    nodes = _flatten(tree)
    assert tree.node_id == "root"
    assert any(node.node_id == "child" for node in nodes)


@pytest.mark.asyncio
async def test_invalid_json_uses_heading_fallback(tmp_path: Path):
    ingester = _make_ingester(tmp_path, chat_client=ScriptedChatClient(response="not-json"))
    tree = await ingester.build_document_tree("# Intro\n## Details", "Intro\nDetails\nbody")

    titles = [node.title for node in _flatten(tree)]
    assert "Intro" in titles or "Details" in titles


@pytest.mark.asyncio
async def test_build_document_tree_can_skip_llm_and_use_deterministic_heading_fallback(tmp_path: Path):
    chat = ScriptedChatClient(
        response="""
        {
          "sections": [
            {
              "node_id": "root",
              "parent_node_id": null,
              "title": "Wrong",
              "start_offset": 0,
              "end_offset": 5,
              "summary": "Wrong"
            }
          ]
        }
        """
    )
    ingester = _make_ingester(tmp_path, chat_client=chat)

    tree = await ingester.build_document_tree(
        "# Intro\n\n## Details\nbody",
        "# Intro\n\n## Details\nbody",
        allow_llm=False,
    )

    titles = [node.title for node in _flatten(tree)]
    assert chat.prompts == []
    assert "Intro" in titles
    assert "Details" in titles


@pytest.mark.asyncio
async def test_build_document_tree_normalizes_root_to_full_document_span(tmp_path: Path):
    text = "# Intro\nHello world\n"
    ingester = _make_ingester(
        tmp_path,
        chat_client=ScriptedChatClient(
            response="""
            {
              "sections": [
                {
                  "node_id": "root",
                  "parent_node_id": null,
                  "title": "Document",
                  "start_offset": 0,
                  "end_offset": 4,
                  "summary": "Overview"
                },
                {
                  "node_id": "intro",
                  "parent_node_id": "root",
                  "title": "Intro",
                  "start_offset": 0,
                  "end_offset": 19,
                  "summary": "Intro section"
                }
              ]
            }
            """
        ),
    )

    tree = await ingester.build_document_tree(text, text)

    assert tree.start_offset == 0
    assert tree.end_offset == len(text)
    assert any(node.title == "Intro" for node in _flatten(tree))


@pytest.mark.asyncio
async def test_invalid_schema_and_chat_errors_use_sequential_fallback(tmp_path: Path):
    invalid_schema = """
    {
      "sections": [
        {
          "node_id": "dup",
          "parent_node_id": null,
          "title": "A",
          "start_offset": 0,
          "end_offset": 10,
          "summary": "A"
        },
        {
          "node_id": "dup",
          "parent_node_id": null,
          "title": "B",
          "start_offset": 0,
          "end_offset": 10,
          "summary": "B"
        }
      ]
    }
    """
    plain_text = ("paragraph one.\n\n" * 800).strip()
    ingester_invalid = _make_ingester(tmp_path / "invalid", chat_client=ScriptedChatClient(response=invalid_schema))
    tree_invalid = await ingester_invalid.build_document_tree("", plain_text)
    assert any(node.node_id.startswith("seq-") for node in _flatten(tree_invalid))

    ingester_error = _make_ingester(
        tmp_path / "error",
        chat_client=ScriptedChatClient(exc=RuntimeError("provider down")),
    )
    tree_error = await ingester_error.build_document_tree("", plain_text)
    assert any(node.node_id.startswith("seq-") for node in _flatten(tree_error))


@pytest.mark.asyncio
async def test_split_oversized_leaf_into_bounded_siblings(tmp_path: Path):
    plain_text = ("0123456789 " * 40).strip()
    end_offset = len(plain_text)
    ingester = _make_ingester(
        tmp_path,
        chat_client=ScriptedChatClient(
            response=f"""
            {{
              "sections": [
                {{
                  "node_id": "root",
                  "parent_node_id": null,
                  "title": "Document",
                  "start_offset": 0,
                  "end_offset": {end_offset},
                  "summary": "Overview"
                }},
                {{
                  "node_id": "child",
                  "parent_node_id": "root",
                  "title": "Section",
                  "start_offset": 0,
                  "end_offset": {end_offset},
                  "summary": "Section summary"
                }}
              ]
            }}
            """
        ),
        max_token_per_node=10,
    )

    tree = await ingester.build_document_tree("# Section\n" + plain_text, plain_text)

    non_root_nodes = [node for node in _flatten(tree) if node.depth > 0]
    assert non_root_nodes
    assert tree.start_offset == 0
    assert tree.end_offset == end_offset
    assert all((node.token_count or 0) <= 10 for node in non_root_nodes)
    assert all(node.node_id != "child" for node in non_root_nodes)
    assert (tree.token_count or 0) > 10


@pytest.mark.asyncio
async def test_split_oversized_internal_nodes_into_bounded_groups(tmp_path: Path):
    plain_text = ("0123456789 " * 60).strip()
    end_offset = len(plain_text)
    ingester = _make_ingester(
        tmp_path,
        chat_client=ScriptedChatClient(
            response=f"""
            {{
              "sections": [
                {{
                  "node_id": "root",
                  "parent_node_id": null,
                  "title": "Document",
                  "start_offset": 0,
                  "end_offset": {end_offset},
                  "summary": "Overview"
                }},
                {{
                  "node_id": "chapter",
                  "parent_node_id": "root",
                  "title": "Chapter",
                  "start_offset": 0,
                  "end_offset": {end_offset},
                  "summary": "Chapter summary"
                }},
                {{
                  "node_id": "section",
                  "parent_node_id": "chapter",
                  "title": "Section",
                  "start_offset": 0,
                  "end_offset": {end_offset},
                  "summary": "Section summary"
                }}
              ]
            }}
            """
        ),
        max_token_per_node=10,
    )

    tree = await ingester.build_document_tree("# Chapter\n## Section\n" + plain_text, plain_text)

    all_nodes = _flatten(tree)
    non_root_nodes = [node for node in all_nodes if node.depth > 0]
    assert non_root_nodes
    assert all((node.token_count or 0) <= 10 for node in non_root_nodes)
    assert all(node.node_id not in {"chapter", "section"} for node in non_root_nodes)
    assert tree.children
    assert all(child.node_id.startswith("chapter-part-") for child in tree.children)
    assert tree.start_offset == 0
    assert tree.end_offset == end_offset


@pytest.mark.asyncio
async def test_root_keeps_full_document_offsets_after_internal_splitting(tmp_path: Path):
    plain_text = ("0123456789 " * 80).strip()
    end_offset = len(plain_text)
    ingester = _make_ingester(
        tmp_path,
        chat_client=ScriptedChatClient(
            response=f"""
            {{
              "sections": [
                {{
                  "node_id": "root",
                  "parent_node_id": null,
                  "title": "Document",
                  "start_offset": 0,
                  "end_offset": {end_offset},
                  "summary": "Overview"
                }},
                {{
                  "node_id": "chapter-1",
                  "parent_node_id": "root",
                  "title": "Chapter 1",
                  "start_offset": 0,
                  "end_offset": {end_offset // 2},
                  "summary": "Chapter 1 summary"
                }},
                {{
                  "node_id": "chapter-2",
                  "parent_node_id": "root",
                  "title": "Chapter 2",
                  "start_offset": {end_offset // 2},
                  "end_offset": {end_offset},
                  "summary": "Chapter 2 summary"
                }}
              ]
            }}
            """
        ),
        max_token_per_node=10,
    )

    tree = await ingester.build_document_tree("# Chapter 1\n# Chapter 2\n" + plain_text, plain_text)

    assert tree.start_offset == 0
    assert tree.end_offset == end_offset
    assert (tree.token_count or 0) > 10
    assert tree.children


@pytest.mark.asyncio
async def test_prompt_is_bounded_for_large_inputs(tmp_path: Path):
    huge_markdown = "\n".join(f"# Heading {i}" for i in range(5000))
    huge_text = "abc " * 100000
    prompt = build_bounded_tree_prompt(huge_markdown, huge_text)
    assert len(prompt) <= TREE_PROMPT_CHAR_LIMIT

    chat = ScriptedChatClient(
        response="""
        {
          "sections": [
            {
              "node_id": "root",
              "parent_node_id": null,
              "title": "Document",
              "start_offset": 0,
              "end_offset": 100,
              "summary": "Overview"
            }
          ]
        }
        """
    )
    ingester = _make_ingester(tmp_path, chat_client=chat)
    await ingester.build_document_tree(huge_markdown, huge_text)
    assert chat.prompts
    assert len(chat.prompts[0]) <= TREE_PROMPT_CHAR_LIMIT


@pytest.mark.asyncio
async def test_context_store_reads_l2_from_filesystem_and_preserves_masking_and_audit(tmp_path: Path):
    file_dir = tmp_path / "doc"
    file_dir.mkdir()
    (file_dir / "extracted.txt").write_text("secret value", encoding="utf-8")

    class FakeDB:
        def __init__(self):
            self.executed: list[tuple[str, tuple]] = []

        async def fetchrow(self, sql, *args):
            return {
                "id": uuid.uuid4(),
                "status": "active",
                "l0_content": "db l0",
                "l1_content": "db l1",
                "l2_content": None,
                "file_path": str(file_dir),
            }

        async def fetchval(self, sql, *args):
            return 1

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "UPDATE 1"

    acl = SimpleNamespace(
        check_read_access=AsyncMock(
            return_value=AccessDecision(allowed=True, field_masks=["secret"], reason="ok")
        )
    )
    masking = SimpleNamespace(apply_masks=lambda content, masks: content.replace("secret", "[MASKED]"))
    audit = SimpleNamespace(log_best_effort=AsyncMock())
    lifecycle = SimpleNamespace(recover_from_stale=AsyncMock())
    store = ContextStore(acl, masking, audit=audit, lifecycle=lifecycle)
    db = FakeDB()
    ctx = RequestContext(account_id="acme", agent_id="query-agent")

    content = await store.read(db, "ctx://resources/manuals/test", ContextLevel.L2, ctx)
    assert content == "[MASKED] value"
    assert db.executed and "last_accessed_at" in db.executed[0][0]
    audit.log_best_effort.assert_awaited_once()

    l0 = await store.read(db, "ctx://resources/manuals/test", ContextLevel.L0, ctx)
    assert l0 == "db l0"


@pytest.mark.asyncio
async def test_context_store_handles_missing_extracted_file_and_stale_recovery(tmp_path: Path):
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()

    class FakeDB:
        def __init__(self):
            self.executed = []

        async def fetchrow(self, sql, *args):
            return {
                "id": uuid.uuid4(),
                "status": "stale",
                "l0_content": "l0",
                "l1_content": "l1",
                "l2_content": None,
                "file_path": str(missing_dir),
            }

        async def fetchval(self, sql, *args):
            return 1

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "UPDATE 1"

    lifecycle = SimpleNamespace(recover_from_stale=AsyncMock())
    store = ContextStore(
        SimpleNamespace(
            check_read_access=AsyncMock(
                return_value=AccessDecision(allowed=True, field_masks=None, reason="ok")
            )
        ),
        SimpleNamespace(apply_masks=lambda content, masks: content),
        audit=SimpleNamespace(log_best_effort=AsyncMock()),
        lifecycle=lifecycle,
    )

    content = await store.read(
        FakeDB(),
        "ctx://resources/manuals/test",
        ContextLevel.L2,
        RequestContext(account_id="acme", agent_id="query-agent"),
    )
    assert content == ""
    lifecycle.recover_from_stale.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_exposes_chat_client_and_document_ingester_and_closes_chat_client():
    fake_pool = MagicMock()
    fake_pool.close = AsyncMock()
    fake_embedding = MagicMock()
    fake_embedding.close = AsyncMock()
    fake_chat = MagicMock()
    fake_chat.close = AsyncMock()
    app = FastAPI()

    with patch.object(main_module.asyncpg, "create_pool", AsyncMock(return_value=fake_pool)), patch.object(
        main_module,
        "create_embedding_client",
        return_value=fake_embedding,
    ), patch.object(
        main_module,
        "create_chat_client",
        return_value=fake_chat,
    ), patch.object(
        main_module.LifecycleScheduler,
        "start",
        AsyncMock(),
    ), patch.object(
        main_module.LifecycleScheduler,
        "stop",
        AsyncMock(),
    ), patch.object(
        main_module.PropagationEngine,
        "start",
        AsyncMock(),
    ), patch.object(
        main_module.PropagationEngine,
        "stop",
        AsyncMock(),
    ):
        async with main_module.lifespan(app):
            assert app.state.chat_client is fake_chat
            assert app.state.document_ingester is not None
            assert app.state.long_doc_retrieval_coordinator is not None
            assert app.state.retrieval_service._long_doc_coordinator is app.state.long_doc_retrieval_coordinator
            assert set(app.state.long_doc_retrieval_coordinator._strategies) == {"tree", "keyword"}
            assert isinstance(
                app.state.long_doc_retrieval_coordinator._strategies["tree"],
                main_module.TreeRetriever,
            )
            assert isinstance(
                app.state.long_doc_retrieval_coordinator._strategies["keyword"],
                main_module.KeywordRetriever,
            )

    fake_chat.close.assert_awaited_once()
    fake_embedding.close.assert_awaited_once()
    fake_pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_integration_document_ingest_smoke(acme_session, services, query_agent_ctx, tmp_path: Path):
    source = tmp_path / "integration.txt"
    source.write_text("# Intro\nIntegration body\n", encoding="utf-8")
    await acme_session.execute(
        """
        INSERT INTO team_memberships (agent_id, team_id, role, access)
        VALUES ($1, $2::uuid, 'member', 'read_write')
        ON CONFLICT (agent_id, team_id)
        DO UPDATE SET access = 'read_write'
        """,
        query_agent_ctx.agent_id,
        ROOT_TEAM_ID,
    )

    chat = ScriptedChatClient(
        response="""
        {
          "sections": [
            {
              "node_id": "root",
              "parent_node_id": null,
              "title": "Document",
              "start_offset": 0,
              "end_offset": 22,
              "summary": "Overview"
            },
            {
              "node_id": "intro",
              "parent_node_id": "root",
              "title": "Intro",
              "start_offset": 0,
              "end_offset": 22,
              "summary": "Intro section"
            }
          ]
        }
        """
    )
    ingester = LongDocumentIngester(
        chat_client=chat,
        embedding_client=NoOpEmbeddingClient(),
        content_generator=ContentGenerator(),
        acl=services.acl,
        audit=services.audit,
        doc_store_root=str(tmp_path / "docs"),
    )

    response = await ingester.ingest(
        acme_session,
        "ctx://resources/manuals/integration",
        str(source),
        query_agent_ctx,
        tags=["integration"],
    )

    context_row = await acme_session.fetchrow(
        """
        SELECT context_type, scope, owner_space, status, file_path, l2_content
        FROM contexts
        WHERE uri = $1
        """,
        response.uri,
    )
    assert context_row is not None
    assert context_row["context_type"] == "resource"
    assert context_row["scope"] == Scope.TEAM.value
    assert context_row["owner_space"] == ""
    assert context_row["status"] == "active"
    assert context_row["file_path"] == response.file_path
    assert context_row["l2_content"] is None

    section_count = await acme_session.fetchval(
        "SELECT COUNT(*) FROM document_sections WHERE context_id = $1",
        response.context_id,
    )
    assert section_count >= 2

    change_count = await acme_session.fetchval(
        """
        SELECT COUNT(*) FROM change_events
        WHERE context_id = $1 AND change_type = 'created' AND actor = $2
        """,
        response.context_id,
        query_agent_ctx.agent_id,
    )
    assert change_count == 1

    l2_content = await services.context_store.read(
        acme_session,
        response.uri,
        ContextLevel.L2,
        query_agent_ctx,
    )
    assert "Integration body" in l2_content
