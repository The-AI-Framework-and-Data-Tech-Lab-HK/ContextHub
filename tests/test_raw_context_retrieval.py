"""Raw evidence must remain discoverable without generated summaries."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from contexthub.generation.base import ContentGenerator
from contexthub.models.context import CreateContextRequest, UpdateContextRequest
from contexthub.models.request import RequestContext
from contexthub.retrieval.rerank import KeywordRerankStrategy
from contexthub.services.context_service import ContextService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_service import LifecycleService


@pytest.mark.asyncio
async def test_raw_create_embeds_original_and_update_clears_old_vector_on_failure():
    row = dict(id=uuid4(), uri="ctx://team/engineering/raw", context_type="resource",
               scope="team", owner_space="engineering", account_id="acme",
               l0_content=None, l1_content=None, l2_content="Ada: 37 engines.",
               file_path=None, status="active", version=1, tags=[])
    row.update({key: None for key in ("created_at", "updated_at", "last_accessed_at", "stale_at", "archived_at", "deleted_at")})
    row.update(active_count=0, adopted_count=0, ignored_count=0)
    db = AsyncMock()
    db.fetchrow.return_value = row
    embedding = AsyncMock()
    embedding.embed.return_value = [1., 0.]
    indexer = IndexerService(ContentGenerator(), embedding, embedding_dimensions=2)
    acl = AsyncMock()
    svc = ContextService(None, acl, indexer)
    ctx = RequestContext(account_id="acme", agent_id="query-agent", expected_version=1)
    result = await svc.create(db, CreateContextRequest(**row), ctx)
    assert result.l0_content is None and result.l1_content is None
    embedding.embed.assert_awaited_once_with("Ada: 37 engines.")

    db.execute.reset_mock()
    row.update(l2_content="Ada: 38 engines.", version=2)
    embedding.embed.return_value = None
    await svc.update(db, row["uri"], UpdateContextRequest(l2_content=row["l2_content"]), ctx)
    embedding.embed.assert_awaited_with("Ada: 38 engines.")
    statements = [call.args[0] for call in db.execute.await_args_list]
    assert any("l0_embedding = NULL" in sql for sql in statements)
    assert not any("l0_embedding = $1" in sql for sql in statements)


@pytest.mark.asyncio
async def test_rerank_uses_original_evidence_when_summaries_absent():
    result = await KeywordRerankStrategy().rerank("Ada 37", [
        {"uri": "irrelevant", "snippet": "Bob has 14 engines."},
        {"uri": "evidence", "snippet": "Ada has 37 engines."},
    ])
    assert result[0]["uri"] == "evidence"
    assert result[0]["_rerank_score"] > result[1]["_rerank_score"]


@pytest.mark.asyncio
async def test_archive_recovery_restores_raw_source_embedding():
    context_id = uuid4()
    indexer = AsyncMock()
    indexer.update_embedding.return_value = True
    service = LifecycleService(indexer=indexer)
    service._fetch_context_row = AsyncMock(return_value={
        "uri": "ctx://team/engineering/raw", "status": "archived",
        "l0_content": None, "l1_content": None, "l2_content": "Ada: 37 engines.",
    })
    service._log_transition = AsyncMock()
    db = AsyncMock()
    db.execute.return_value = "UPDATE 1"
    await service.recover_from_archived(db, context_id, RequestContext(account_id="acme", agent_id="query-agent"))
    indexer.update_embedding.assert_awaited_once_with(db, context_id, "Ada: 37 engines.")
