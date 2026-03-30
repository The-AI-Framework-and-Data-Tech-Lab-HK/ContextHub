"""Local FS repository for trajectory bundles (Phase 1)."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


class LocalFSTrajectoryRepository:
    """Persist trajectory artifacts under local content root."""

    def __init__(self, root: str) -> None:
        # Root is typically storage.content_store.localfs_root from config.
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "_index.json"
        self._idempotency_path = self.root / "_idempotency.json"

    def _build_trajectory_base_path(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> Path:
        return (
            self.root
            / "accounts"
            / account_id
            / "scope"
            / scope
            / owner_space
            / "memories"
            / "trajectories"
            / trajectory_id
        )

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def find_trajectory_id_by_idempotency_key(self, key: str) -> str | None:
        # Lightweight lookup for duplicate commit detection.
        data = self._read_json(self._idempotency_path)
        value = data.get(key)
        return str(value) if value else None

    def save_bundle(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        visualize_graph_png: bool = False,
    ) -> str:
        # account/scope/owner_space hierarchy aligned with main memory/search.
        base = self._build_trajectory_base_path(
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
        )
        base.mkdir(parents=True, exist_ok=True)

        graph_pointer = {
            "backend": "localfs_phase1",
            "storage_layout": "accounts_scope_owner_space",
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "account_id": account_id,
            "scope": scope,
            "owner_space": owner_space,
            "raw_graph_file": str(base / "raw_graph.json"),
            "clean_graph_file": str(base / "clean_graph.json"),
            "graph_kind": ["raw", "clean"],
        }
        (base / "trajectory.json").write_text(
            json.dumps(_to_jsonable(payload["trajectory"]), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (base / "raw_graph.json").write_text(
            json.dumps(_to_jsonable(payload["raw_graph"]), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (base / "clean_graph.json").write_text(
            json.dumps(_to_jsonable(payload["clean_graph"]), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        llm_traces = payload.get("llm_extraction_traces")
        if isinstance(llm_traces, list) and llm_traces:
            extraction_dir = base / "llm_extraction"
            extraction_dir.mkdir(parents=True, exist_ok=True)
            trace_files: list[str] = []
            for idx, trace in enumerate(llm_traces, start=1):
                if not isinstance(trace, dict):
                    continue
                call_type = str(trace.get("call_type") or f"call{idx}")
                safe_type = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in call_type)
                file_path = extraction_dir / f"{idx:02d}_{safe_type}.json"
                file_path.write_text(
                    json.dumps(_to_jsonable(trace), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                trace_files.append(str(file_path))
            graph_pointer["llm_extraction_dir"] = str(extraction_dir)
            graph_pointer["llm_extraction_files"] = trace_files
        if visualize_graph_png:
            from infra.storage.fs.graph_visualizer import render_graph_png

            raw_png = str(base / "raw_graph.png")
            clean_png = str(base / "clean_graph.png")
            render_graph_png(_to_jsonable(payload["raw_graph"]), raw_png, title=f"Raw Graph: {trajectory_id}")
            render_graph_png(
                _to_jsonable(payload["clean_graph"]),
                clean_png,
                title=f"Clean Graph: {trajectory_id}",
            )
            graph_pointer["raw_graph_png"] = raw_png
            graph_pointer["clean_graph_png"] = clean_png
        (base / "graph_pointer.json").write_text(
            json.dumps(graph_pointer, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (base / ".abstract.md").write_text(str(payload["abstract"]), encoding="utf-8")
        (base / ".overview.md").write_text(str(payload["overview"]), encoding="utf-8")
        (base / "meta.json").write_text(
            json.dumps(
                {
                    "trajectory_id": trajectory_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "account_id": account_id,
                    "scope": scope,
                    "owner_space": owner_space,
                    "task_id": payload.get("task_id"),
                    "labels": payload.get("labels", {}),
                    "nodes": payload["nodes"],
                    "edges": payload["edges"],
                    "visualize_graph_png": visualize_graph_png,
                    "saved_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # Update local indexes for replay and idempotency checks.
        index = self._read_json(self._index_path)
        index[trajectory_id] = str(base)
        self._write_json(self._index_path, index)

        idem = self._read_json(self._idempotency_path)
        idem[idempotency_key] = trajectory_id
        self._write_json(self._idempotency_path, idem)
        return str(base)

    def load_trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        # Replay reads from index, then reconstructs a response-friendly bundle.
        index = self._read_json(self._index_path)
        base = index.get(trajectory_id)
        if not base:
            return None
        b = Path(base)
        if not b.exists():
            return None
        return {
            "meta": json.loads((b / "meta.json").read_text(encoding="utf-8")),
            "trajectory": json.loads((b / "trajectory.json").read_text(encoding="utf-8")),
            "graph_pointer": json.loads((b / "graph_pointer.json").read_text(encoding="utf-8")),
            "abstract": (b / ".abstract.md").read_text(encoding="utf-8"),
            "overview": (b / ".overview.md").read_text(encoding="utf-8"),
            "base_path": str(b),
        }
