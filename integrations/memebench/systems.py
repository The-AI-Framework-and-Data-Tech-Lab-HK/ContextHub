"""Wire ContextHub services for the MEME Cascade eval.

Mirrors tests/conftest.py::services but swaps in real OpenAI-compatible clients
(via the yunwu proxy in model_providers.local.json) for embedding and chat, and
builds the oracle-backed propagation registry + engine with the cascade gate on.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import asyncpg

from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.generation.base import ContentGenerator
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.openai_client import OpenAIEmbeddingClient
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import DependencyDiscoveryService
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.masking_service import MaskingService
from contexthub.services.memory_service import MemoryService
from contexthub.services.propagation_engine import PropagationEngine
from contexthub.services.retrieval_service import RetrievalService

from integrations.memebench.cost import CountingChatClient
from integrations.memebench.embedding_retry import RetryingEmbeddingClient

DEFAULT_DSN = "postgresql://contexthub:contexthub@localhost:5432/contexthub"
DEFAULT_PROVIDERS_PATH = Path(__file__).resolve().parents[2] / "model_providers.local.json"
EMBEDDING_DIM = 1536


def load_provider(
    label: str = "yunwu",
    path: str | Path = DEFAULT_PROVIDERS_PATH,
) -> dict[str, Any]:
    """Load one provider entry (base_url + api_key + models) by label."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for target in data.get("targets", []):
        if target.get("label") == label:
            return target
    raise ValueError(f"Provider label '{label}' not found in {path}")


@dataclass
class EvalSystem:
    """Bundle of everything a run needs; close() releases the pool + clients."""

    repo: PgRepository
    pool: asyncpg.Pool
    dsn: str
    indexer: IndexerService
    lifecycle: LifecycleService
    memory: MemoryService
    retrieval: RetrievalService
    acl: ACLService
    masking: MaskingService
    audit: AuditService
    embedding: OpenAIEmbeddingClient
    answer_chat: CountingChatClient      # inference: answer generation
    oracle_chat: CountingChatClient      # oracle: failure-staleness judgement
    discovery_chat: CountingChatClient   # ingest: write-time dependency discovery
    judge_chat: CountingChatClient       # judge: MEME-parity answer grading
    extract_chat: CountingChatClient     # ingest: raw-dialogue fact extraction (mode B)
    discovery: DependencyDiscoveryService            # naive (edge_mode=discovered)
    discovery_tiered: DependencyDiscoveryService     # tier-0 syntactic routing + LLM
    discovery_hard: DependencyDiscoveryService       # tier-0 syntactic hard-exclude
    extractor: ConversationExtractionService         # raw-dialogue extractor (mode B)
    rule_registry: PropagationRuleRegistry

    def build_engine(self, *, cascade_on_stale: bool = True) -> PropagationEngine:
        return PropagationEngine(
            repo=self.repo,
            pool=self.pool,
            dsn=self.dsn,
            rule_registry=self.rule_registry,
            lifecycle=self.lifecycle,
            indexer=self.indexer,
            sweep_interval=9999,
            lease_timeout=30,
            cascade_on_stale=cascade_on_stale,
        )

    async def close(self) -> None:
        await self.embedding.close()
        await self.answer_chat.close()
        await self.oracle_chat.close()
        await self.discovery_chat.close()
        await self.judge_chat.close()
        await self.extract_chat.close()
        await self.pool.close()


async def build_system(
    *,
    chat_model: str = "gpt-4o-mini",
    oracle_model: str | None = None,
    judge_model: str = "gpt-4o",
    extract_model: str = "claude-opus-4-8",
    provider_label: str = "yunwu",
    embedding_provider_label: str = "aliyun",
    embedding_model: str | None = None,
    dsn: str = DEFAULT_DSN,
    providers_path: str | Path = DEFAULT_PROVIDERS_PATH,
) -> EvalSystem:
    """Build a fully-wired EvalSystem.

    chat/oracle use ``provider_label`` (default yunwu); embedding uses
    ``embedding_provider_label`` (default aliyun text-embedding-v4, which is more
    stable than yunwu's embedding endpoint). Embedding output dimension is forced
    to EMBEDDING_DIM (1536) to match the DB vector column and MEME's baseline.
    """
    provider = load_provider(provider_label, providers_path)
    base_url = provider["base_url"]
    api_key = provider["api_key"]
    oracle_model = oracle_model or chat_model

    emb_provider = load_provider(embedding_provider_label, providers_path)
    emb_model = embedding_model or emb_provider.get("embedding_model") or "text-embedding-3-small"
    # Only pass the API-side dimensions param when the provider supports it
    # (aliyun v4). text-embedding-3-small is natively 1536, no param needed.
    emb_dimensions = EMBEDDING_DIM if emb_provider.get("embedding_supports_dimensions") else None

    pool = await asyncpg.create_pool(dsn, init=init_pg_connection, min_size=1, max_size=8)
    repo = PgRepository(pool)

    acl = ACLService()
    masking = MaskingService()
    audit = AuditService(pool=pool)
    embedding = RetryingEmbeddingClient(
        OpenAIEmbeddingClient(
            api_key=emb_provider["api_key"],
            base_url=emb_provider["base_url"],
            model=emb_model,
            expected_dimensions=EMBEDDING_DIM,
            dimensions=emb_dimensions,
            timeout=60.0,
        ),
        max_batch=emb_provider.get("embedding_max_batch"),
    )
    generator = ContentGenerator()
    indexer = IndexerService(generator, embedding, embedding_dimensions=EMBEDDING_DIM)
    lifecycle = LifecycleService(audit=audit, indexer=indexer)
    memory = MemoryService(indexer, acl, masking, audit=audit)
    retrieval_router = RetrievalRouter.default()
    retrieval = RetrievalService(
        retrieval_router, embedding, acl,
        masking_service=masking,
        audit_service=audit,
    )

    answer_chat = CountingChatClient(
        OpenAIChatClient(api_key=api_key, base_url=base_url, model=chat_model)
    )
    oracle_chat = CountingChatClient(
        OpenAIChatClient(api_key=api_key, base_url=base_url, model=oracle_model)
    )
    discovery_chat = CountingChatClient(
        OpenAIChatClient(api_key=api_key, base_url=base_url, model=chat_model)
    )
    judge_chat = CountingChatClient(
        OpenAIChatClient(api_key=api_key, base_url=base_url, model=judge_model)
    )
    extract_chat = CountingChatClient(
        OpenAIChatClient(api_key=api_key, base_url=base_url, model=extract_model)
    )
    discovery = DependencyDiscoveryService(discovery_chat)
    discovery_tiered = DependencyDiscoveryService(discovery_chat, conditional_aware=True)
    discovery_hard = DependencyDiscoveryService(discovery_chat, conditional_hard=True)
    extractor = ConversationExtractionService(extract_chat)
    rule_registry = PropagationRuleRegistry.default(chat_client=oracle_chat, repo=repo)

    return EvalSystem(
        repo=repo,
        pool=pool,
        dsn=dsn,
        indexer=indexer,
        lifecycle=lifecycle,
        memory=memory,
        retrieval=retrieval,
        acl=acl,
        masking=masking,
        audit=audit,
        embedding=embedding,
        answer_chat=answer_chat,
        oracle_chat=oracle_chat,
        discovery_chat=discovery_chat,
        judge_chat=judge_chat,
        extract_chat=extract_chat,
        discovery=discovery,
        discovery_tiered=discovery_tiered,
        discovery_hard=discovery_hard,
        extractor=extractor,
        rule_registry=rule_registry,
    )
