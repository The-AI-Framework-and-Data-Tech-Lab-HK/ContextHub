from __future__ import annotations

from pathlib import Path

import pytest

from core.indexing.trajectory_vector_indexer import TrajectoryVectorIndexer

pytestmark = pytest.mark.unit


class _FakeVectorStore:
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.upsert_calls = 0

    def get_metadatas(self, ids: list[str]) -> dict[str, dict]:
        return {i: self.store[i]["metadata"] for i in ids if i in self.store}

    def upsert_embeddings(self, records: list[dict]) -> None:
        self.upsert_calls += 1
        for r in records:
            self.store[str(r["id"])] = r

    def query(self, embedding: list[float], top_k: int) -> list[dict]:
        return []


def _write_l0_l1(base: Path, l0: str, l1: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / ".abstract.md").write_text(l0, encoding="utf-8")
    (base / ".overview.md").write_text(l1, encoding="utf-8")


def test_indexer_skips_unchanged_and_updates_changed_content(tmp_path: Path) -> None:
    base = tmp_path / "traj"
    _write_l0_l1(base, "first abstract", "first overview")

    fake = _FakeVectorStore()
    embed_calls = {"n": 0}

    def _fake_embed(text: str) -> list[float]:
        embed_calls["n"] += 1
        return [float(len(text)), 1.0]

    indexer = TrajectoryVectorIndexer(
        vector_store=fake,
        embedding_model="dummy",
        api_key="dummy",
        include_levels=(0, 1),
        embedding_fn=_fake_embed,
    )

    first = indexer.index_trajectory(
        tenant_id="t1",
        agent_id="a1",
        account_id="acc-1",
        scope="agent",
        owner_space="a1",
        trajectory_id="trajx",
        task_type="sql_analysis",
        base_path=str(base),
    )
    assert first["upserted_docs"] == 2
    assert first["skipped_unchanged_docs"] == 0
    assert embed_calls["n"] == 2

    second = indexer.index_trajectory(
        tenant_id="t1",
        agent_id="a1",
        account_id="acc-1",
        scope="agent",
        owner_space="a1",
        trajectory_id="trajx",
        task_type="sql_analysis",
        base_path=str(base),
    )
    assert second["upserted_docs"] == 0
    assert second["skipped_unchanged_docs"] == 2
    assert embed_calls["n"] == 2

    (base / ".overview.md").write_text("second overview changed", encoding="utf-8")
    third = indexer.index_trajectory(
        tenant_id="t1",
        agent_id="a1",
        account_id="acc-1",
        scope="agent",
        owner_space="a1",
        trajectory_id="trajx",
        task_type="sql_analysis",
        base_path=str(base),
    )
    assert third["upserted_docs"] == 1
    assert third["skipped_unchanged_docs"] == 1
    assert embed_calls["n"] == 3
