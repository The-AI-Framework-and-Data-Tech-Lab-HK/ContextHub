"""Migrate local FS trajectory bundles to accounts/scope/owner_space layout."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _target_path(*, root: Path, account_id: str, scope: str, owner_space: str, trajectory_id: str) -> Path:
    return (
        root
        / "accounts"
        / account_id
        / "scope"
        / scope
        / owner_space
        / "memories"
        / "trajectories"
        / trajectory_id
    )


def _patch_bundle_metadata(
    *,
    base: Path,
    tenant_id: str,
    agent_id: str,
    account_id: str,
    scope: str,
    owner_space: str,
) -> None:
    meta_path = base / "meta.json"
    meta = _read_json(meta_path)
    if meta:
        meta["tenant_id"] = tenant_id
        meta["agent_id"] = agent_id
        meta["account_id"] = account_id
        meta["scope"] = scope
        meta["owner_space"] = owner_space
        _write_json(meta_path, meta)

    pointer_path = base / "graph_pointer.json"
    pointer = _read_json(pointer_path)
    if pointer:
        pointer["storage_layout"] = "accounts_scope_owner_space"
        pointer["tenant_id"] = tenant_id
        pointer["agent_id"] = agent_id
        pointer["account_id"] = account_id
        pointer["scope"] = scope
        pointer["owner_space"] = owner_space
        _write_json(pointer_path, pointer)


def _collect_legacy_paths(root: Path) -> list[tuple[str, str, str, Path]]:
    out: list[tuple[str, str, str, Path]] = []
    tenant_root = root / "tenant"
    if not tenant_root.exists():
        return out
    for tenant_dir in tenant_root.iterdir():
        if not tenant_dir.is_dir():
            continue
        agent_root = tenant_dir / "agent"
        if not agent_root.exists():
            continue
        for agent_dir in agent_root.iterdir():
            if not agent_dir.is_dir():
                continue
            traj_root = agent_dir / "memories" / "trajectories"
            if not traj_root.exists():
                continue
            for trajectory_dir in traj_root.iterdir():
                if not trajectory_dir.is_dir():
                    continue
                out.append((tenant_dir.name, agent_dir.name, trajectory_dir.name, trajectory_dir))
    return out


def run(*, root: Path, dry_run: bool = False) -> dict[str, Any]:
    index_path = root / "_index.json"
    index_payload = _read_json(index_path)

    moved = 0
    skipped_exists = 0
    total = 0
    updated_index = dict(index_payload)

    for tenant_id, agent_id, trajectory_id, legacy_dir in _collect_legacy_paths(root):
        total += 1
        account_id = tenant_id
        scope = "agent"
        owner_space = agent_id
        target = _target_path(
            root=root,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
        )
        if target.exists():
            skipped_exists += 1
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_dir), str(target))
            _patch_bundle_metadata(
                base=target,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
            )
        updated_index[trajectory_id] = str(target)
        moved += 1

    if not dry_run:
        _write_json(index_path, updated_index)
    return {
        "root": str(root),
        "total_legacy_trajectories": total,
        "moved": moved,
        "skipped_target_exists": skipped_exists,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_fs_scope_layout",
        description="Migrate trajectory bundles from tenant/agent layout to accounts/scope layout.",
    )
    parser.add_argument("--root", default="./data/content", help="Local FS content root")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without moving files")
    args = parser.parse_args()
    summary = run(root=Path(args.root), dry_run=bool(args.dry_run))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
