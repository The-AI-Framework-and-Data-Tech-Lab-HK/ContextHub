"""Graph storage base protocol for AMC."""

from __future__ import annotations

from typing import Any, Protocol


class GraphStoreWriter(Protocol):
    """Write raw/clean trajectory graphs into an external graph backend."""

    def upsert_trajectory_graphs(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        raw_graph: dict[str, Any],
        clean_graph: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Upsert one trajectory's raw and clean graphs and optionally return summary."""
