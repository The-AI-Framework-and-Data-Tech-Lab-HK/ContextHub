"""Wire ContextHub services for the MEME Cascade eval.

Mirrors tests/conftest.py::services but swaps in real OpenAI-compatible clients
(via the openlux proxy in model_providers.local.json) for embedding and chat, and
builds the oracle-backed propagation registry + engine with the cascade gate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    label: str = "openlux",
    path: str | Path = DEFAULT_PROVIDERS_PATH,
) -> dict[str, Any]:
    """Load one provider entry (base_url + api_key + models) by label."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for target in data.get("targets", []):
        if target.get("label") == label:
            return target
    raise ValueError(f"Provider label '{label}' not found in {path}")


def provider_from_document(label: str, document: dict[str, Any]) -> dict[str, Any]:
    targets = document.get("targets")
    if isinstance(targets, dict):
        provider = targets.get(label)
        if isinstance(provider, dict):
            return dict(provider)
    if isinstance(targets, list):
        for provider in targets:
            if isinstance(provider, dict) and provider.get("label") == label:
                return dict(provider)
    raise ValueError(f"Provider label '{label}' not found in frozen document")


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
    # None when the model was not named (feature off) — no silent model default.
    judge_chat: CountingChatClient | None       # judge: MEME-parity answer grading
    extract_chat: CountingChatClient | None     # ingest: raw-dialogue extraction (mode B)
    discovery: DependencyDiscoveryService            # naive (edge_mode=discovered)
    discovery_tiered: DependencyDiscoveryService     # tier-0 syntactic routing + LLM
    discovery_hard: DependencyDiscoveryService       # tier-0 syntactic hard-exclude
    extractor: ConversationExtractionService | None  # raw-dialogue extractor (mode B)
    rule_registry: PropagationRuleRegistry
    # 做法甲 build-side cascade tiers (only wired when build_system(cascade=True)).
    # weak = gpt-4o-mini, strong = gpt-5.6-sol; used by ingest_case_raw_cascade_e2e
    # for variables 2 (disamb) and 3 (edge discovery). variable 1 stays the fixed
    # `extractor` above; variable 4 is LLM-free (route_candidate_selection).
    cascade_cheap_chat: CountingChatClient | None = None
    cascade_strong_chat: CountingChatClient | None = None
    cascade_cheap_svc: DependencyDiscoveryService | None = None
    cascade_strong_svc: DependencyDiscoveryService | None = None
    # 做法乙 P2 propagation-side cheap tier (only wired when build_system(p2_cascade=True)).
    # oracle_chat above is the strong (verify) tier; this is the cheap gate that
    # DerivedMemoryOracleRule consults first (soundness direction: cheap-stale short-
    # circuits, cheap-fresh escalates to oracle_chat). None = single-tier oracle.
    p2_cheap_chat: CountingChatClient | None = None
    # Per-edge staleness-gate log, appended to by DerivedMemoryOracleRule. run_eval
        # clears it per case and copies it into that case's record, so the
    # cheap-tier short-circuit / escalation counts land in cases.json instead of
    # living only in process counters. Same list object for the run's lifetime.
    gate_events: list = field(default_factory=list)

    def build_engine(self, *, cascade_on_stale: bool = True) -> PropagationEngine:
        return PropagationEngine(
            repo=self.repo,
            pool=self.pool,
            dsn=self.dsn,
            rule_registry=self.rule_registry,
            lifecycle=self.lifecycle,
            sweep_interval=9999,
            lease_timeout=30,
            cascade_on_stale=cascade_on_stale,
        )

    async def close(self) -> None:
        await self.embedding.close()
        await self.answer_chat.close()
        await self.oracle_chat.close()
        await self.discovery_chat.close()
        if self.judge_chat is not None:
            await self.judge_chat.close()
        if self.extract_chat is not None:
            await self.extract_chat.close()
        if self.cascade_cheap_chat is not None:
            await self.cascade_cheap_chat.close()
        if self.cascade_strong_chat is not None:
            await self.cascade_strong_chat.close()
        if self.p2_cheap_chat is not None:
            await self.p2_cheap_chat.close()
        await self.pool.close()


async def build_system(
    *,
    chat_model: str,
    oracle_model: str | None = None,
    judge_model: str | None = None,
    extract_model: str | None = None,
    provider_label: str = "openlux",
    embedding_provider_label: str = "aliyun",
    embedding_model: str | None = None,
    dsn: str = DEFAULT_DSN,
    providers_path: str | Path = DEFAULT_PROVIDERS_PATH,
    providers_document: dict[str, Any] | None = None,
    cascade: bool = False,
    cascade_cheap_model: str | None = None,
    cascade_strong_model: str | None = None,
    p2_cascade: bool = False,
    p2_cheap_model: str | None = None,
) -> EvalSystem:
    """Build a fully-wired EvalSystem.

    MODEL ARGUMENTS CARRY NO DEFAULTS ON PURPOSE (2026-08-10). A silent
    ``extract_model`` default once sent a whole 100-case P2 run through opus when
    the recorded decision was gpt-4.1-mini, and the resulting numbers were
    mis-attributed for a month. Every model in play must be named by the caller
    at each run, so the run log and the intent cannot drift apart.

    ``chat_model`` is always required. ``judge_model`` / ``extract_model`` /
    ``cascade_*_model`` / ``p2_cheap_model`` may stay None only while the feature
    that consumes them is off; switching the feature on without naming its model
    raises ValueError rather than falling back to a guess.

    chat/oracle use ``provider_label`` (default openlux); embedding uses
    ``embedding_provider_label`` (default aliyun text-embedding-v4, which is more
    stable than the chat proxy's embedding endpoint). Embedding output dimension is forced
    to EMBEDDING_DIM (1536) to match the DB vector column and MEME's baseline.
    """
    if cascade and not (cascade_cheap_model and cascade_strong_model):
        raise ValueError(
            "cascade=True needs cascade_cheap_model and cascade_strong_model named explicitly"
        )
    if p2_cascade and not p2_cheap_model:
        raise ValueError("p2_cascade=True needs p2_cheap_model named explicitly")
    provider = (
        provider_from_document(provider_label, providers_document)
        if providers_document is not None
        else load_provider(provider_label, providers_path)
    )
    base_url = provider["base_url"]
    api_key = provider["api_key"]
    oracle_model = oracle_model or chat_model

    emb_provider = (
        provider_from_document(embedding_provider_label, providers_document)
        if providers_document is not None
        else load_provider(embedding_provider_label, providers_path)
    )
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
    # judge/extract are only built when their model was named (no silent default).
    judge_chat = extract_chat = extractor = None
    if judge_model:
        judge_chat = CountingChatClient(
            OpenAIChatClient(api_key=api_key, base_url=base_url, model=judge_model)
        )
    if extract_model:
        extract_chat = CountingChatClient(
            OpenAIChatClient(api_key=api_key, base_url=base_url, model=extract_model)
        )
        extractor = ConversationExtractionService(extract_chat)
    discovery = DependencyDiscoveryService(discovery_chat)
    discovery_tiered = DependencyDiscoveryService(discovery_chat, conditional_aware=True)
    discovery_hard = DependencyDiscoveryService(discovery_chat, conditional_hard=True)

    # 做法乙 P2 propagation-side cascade: cheap gate before the oracle (strong) tier.
    p2_cheap_chat = None
    if p2_cascade:
        p2_cheap_chat = CountingChatClient(
            OpenAIChatClient(api_key=api_key, base_url=base_url, model=p2_cheap_model)
        )
    gate_events: list = []
    rule_registry = PropagationRuleRegistry.default(
        chat_client=oracle_chat, repo=repo,
        cheap_chat=p2_cheap_chat,  # None → single-tier oracle (byte-for-byte regression)
        gate_event_sink=gate_events,
    )

    # 做法甲 build-side cascade tiers (weak/strong), only wired when cascade=True.
    cascade_cheap_chat = cascade_strong_chat = None
    cascade_cheap_svc = cascade_strong_svc = None
    if cascade:
        cascade_cheap_chat = CountingChatClient(
            OpenAIChatClient(api_key=api_key, base_url=base_url, model=cascade_cheap_model)
        )
        cascade_strong_chat = CountingChatClient(
            OpenAIChatClient(api_key=api_key, base_url=base_url, model=cascade_strong_model)
        )
        cascade_cheap_svc = DependencyDiscoveryService(cascade_cheap_chat)
        cascade_strong_svc = DependencyDiscoveryService(cascade_strong_chat)

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
        cascade_cheap_chat=cascade_cheap_chat,
        cascade_strong_chat=cascade_strong_chat,
        cascade_cheap_svc=cascade_cheap_svc,
        cascade_strong_svc=cascade_strong_svc,
        p2_cheap_chat=p2_cheap_chat,
        gate_events=gate_events,
    )
