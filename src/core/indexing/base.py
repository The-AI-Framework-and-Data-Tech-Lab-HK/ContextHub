"""Indexer protocol abstractions."""

from __future__ import annotations

from typing import Any, Protocol


class TrajectoryIndexer(Protocol):
    """Pluggable trajectory indexer protocol."""

    def index_trajectory(
        self,
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        base_path: str,
        lifecycle_status: str = "active",
        stale_flag: bool = False,
        force_reindex: bool = False,
    ) -> dict[str, Any]:
        """Index trajectory artifacts and return summary."""
