from __future__ import annotations

import pytest

from core.retrieve.semantic_recall import SemanticRecall
from core.retrieve.service import RetrieveCommand, RetrieveService

pytestmark = pytest.mark.unit


class _FakeVectorStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def get_metadatas(self, ids: list[str]) -> dict[str, dict]:
        return {}

    def upsert_embeddings(self, records: list[dict]) -> None:
        return None

    def query(self, embedding: list[float], top_k: int) -> list[dict]:
        return self.rows[:top_k]


def test_semantic_recall_filters_scope_and_groups_by_trajectory() -> None:
    rows = [
        {
            "id": "a",
            "distance": 0.1,
            "metadata": {
                "tenant_id": "tenant-a",
                "agent_id": "agent-1",
                "trajectory_id": "traj-1",
                "uri": "ctx://.../.abstract.md",
            },
        },
        {
            "id": "b",
            "distance": 0.2,
            "metadata": {
                "tenant_id": "tenant-a",
                "agent_id": "agent-1",
                "trajectory_id": "traj-1",
                "uri": "ctx://.../.overview.md",
            },
        },
        {
            "id": "c",
            "distance": 0.05,
            "metadata": {
                "tenant_id": "tenant-a",
                "agent_id": "agent-2",
                "trajectory_id": "traj-other-agent",
                "uri": "ctx://.../x",
            },
        },
    ]
    recall = SemanticRecall(
        vector_store=_FakeVectorStore(rows),
        embedding_model="dummy",
        api_key="dummy",
        embedding_fn=lambda _: [0.1, 0.2],
    )
    hits = recall.recall(tenant_id="tenant-a", agent_id="agent-1", query_text="q", top_k=5)
    assert len(hits) == 1
    assert hits[0].trajectory_id == "traj-1"
    assert len(hits[0].matched_uris) == 2
    assert hits[0].semantic_score > 0


def test_retrieve_service_returns_semantic_items() -> None:
    rows = [
        {
            "id": "a",
            "distance": 0.1,
            "metadata": {
                "tenant_id": "tenant-a",
                "agent_id": "agent-1",
                "trajectory_id": "traj-1",
                "uri": "ctx://agent/agent-1/memories/trajectories/traj-1/.abstract.md",
            },
        }
    ]
    recall = SemanticRecall(
        vector_store=_FakeVectorStore(rows),
        embedding_model="dummy",
        api_key="dummy",
        embedding_fn=lambda _: [0.1, 0.2],
    )
    service = RetrieveService(semantic_recall=recall)
    out = service.run(
        RetrieveCommand(
            tenant_id="tenant-a",
            agent_id="agent-1",
            query={"task_description": "analyze revenue", "constraints": {"tool_whitelist": ["local_db_sql"]}},
            top_k=3,
        )
    )
    assert out.warnings == []
    assert len(out.items) == 1
    assert out.items[0]["trajectory_id"] == "traj-1"
    assert out.items[0]["graph_score"] is None
    assert out.items[0]["semantic_score"] == out.items[0]["score"]
