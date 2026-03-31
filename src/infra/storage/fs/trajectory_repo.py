"""Local FS repository for trajectory bundles (Phase 1)."""

from __future__ import annotations

import json
import shutil
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
        self._uri_index_path = self.root / "_uri_index.json"
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

    @staticmethod
    def _trajectory_uri(*, scope: str, owner_space: str, trajectory_id: str) -> str:
        s = (scope or "agent").strip().lower() or "agent"
        return f"ctx://{s}/{owner_space}/memories/trajectories/{trajectory_id}"

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
        update_trajectory_index: bool = True,
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
        if update_trajectory_index:
            index = self._read_json(self._index_path)
            index[trajectory_id] = str(base)
            self._write_json(self._index_path, index)

        uri_index = self._read_json(self._uri_index_path)
        uri = self._trajectory_uri(scope=scope, owner_space=owner_space, trajectory_id=trajectory_id)
        uri_index[uri] = str(base)
        self._write_json(self._uri_index_path, uri_index)

        idem = self._read_json(self._idempotency_path)
        idem[idempotency_key] = trajectory_id
        self._write_json(self._idempotency_path, idem)
        return str(base)

    def load_trajectory_by_uri(self, uri: str) -> dict[str, Any] | None:
        uri_index = self._read_json(self._uri_index_path)
        base = str(uri_index.get(uri) or "")
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

    def promote_bundle_to_team(
        self,
        *,
        account_id: str,
        source_agent_id: str,
        promoted_by_agent_id: str,
        source_trajectory_id: str,
        target_team: str,
    ) -> dict[str, Any]:
        source_uri = self._trajectory_uri(
            scope="agent",
            owner_space=source_agent_id,
            trajectory_id=source_trajectory_id,
        )
        source_bundle = self.load_trajectory_by_uri(source_uri) or self.load_trajectory(source_trajectory_id)
        if not source_bundle:
            raise FileNotFoundError(f"trajectory not found: {source_trajectory_id}")

        source_meta = dict(source_bundle.get("meta") or {})
        source_scope = str(source_meta.get("scope") or "agent")
        source_owner = str(source_meta.get("owner_space") or source_agent_id)
        if source_scope != "agent" or source_owner != source_agent_id:
            raise PermissionError("can only promote agent-private trajectories owned by source agent")

        target_scope = "team"
        target_owner = target_team
        target_uri = self._trajectory_uri(
            scope=target_scope,
            owner_space=target_owner,
            trajectory_id=source_trajectory_id,
        )
        target_base = self._build_trajectory_base_path(
            account_id=account_id,
            scope=target_scope,
            owner_space=target_owner,
            trajectory_id=source_trajectory_id,
        )
        existing = self.load_trajectory_by_uri(target_uri)
        if existing is not None and target_base.exists():
            shutil.rmtree(target_base)
        target_base.mkdir(parents=True, exist_ok=True)
        src_base = Path(str(source_bundle["base_path"]))

        # Copy core artifacts into promoted team scope for standalone replay/debug.
        for filename in [
            "trajectory.json",
            "raw_graph.json",
            "clean_graph.json",
            ".abstract.md",
            ".overview.md",
        ]:
            src = src_base / filename
            if src.exists():
                shutil.copy2(src, target_base / filename)

        llm_src_dir = src_base / "llm_extraction"
        llm_dst_dir = target_base / "llm_extraction"
        if llm_src_dir.exists() and llm_src_dir.is_dir():
            shutil.copytree(llm_src_dir, llm_dst_dir, dirs_exist_ok=True)

        source_pointer = json.loads((src_base / "graph_pointer.json").read_text(encoding="utf-8"))
        promoted_pointer = dict(source_pointer)
        promoted_pointer.update(
            {
                "scope": target_scope,
                "owner_space": target_owner,
                "raw_graph_file": str(target_base / "raw_graph.json"),
                "clean_graph_file": str(target_base / "clean_graph.json"),
                "promoted_from_uri": source_uri,
                "promoted_by": promoted_by_agent_id,
                "promoted_at": datetime.now(UTC).isoformat(),
            }
        )
        if llm_dst_dir.exists():
            promoted_pointer["llm_extraction_dir"] = str(llm_dst_dir)
        (target_base / "graph_pointer.json").write_text(
            json.dumps(promoted_pointer, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        promoted_meta = dict(source_meta)
        promoted_meta.update(
            {
                "account_id": account_id,
                "scope": target_scope,
                "owner_space": target_owner,
                "promoted_from_uri": source_uri,
                "promoted_by": promoted_by_agent_id,
                "saved_at": datetime.now(UTC).isoformat(),
            }
        )
        (target_base / "meta.json").write_text(
            json.dumps(promoted_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        uri_index = self._read_json(self._uri_index_path)
        uri_index[target_uri] = str(target_base)
        self._write_json(self._uri_index_path, uri_index)
        return {
            "source_uri": source_uri,
            "target_uri": target_uri,
            "trajectory_id": source_trajectory_id,
            "scope": target_scope,
            "owner_space": target_owner,
            "base_path": str(target_base),
        }

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
