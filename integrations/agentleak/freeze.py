"""Protocol-freeze metadata pipeline for AgentLeak Phase 5 formal runs.

A paper-eligible (formal) run must declare, BEFORE any trace is generated, the
exact recipe it will follow: the protocol snapshot (pinned by content hash), the
scenario-subset recipe (seed / n / selection rule / generator commit), the code
revisions of both repositories, and the model/provider. This module writes that
freeze bundle and, after generation, verifies that what actually ran matches the
frozen declaration and that all compared systems used the SAME scenario subset.

Design decisions baked in (user-confirmed 2026-07-07):
- (#2 A) The subset is frozen as a RECIPE (seed/n/selection_rule/generator_commit),
  not a pre-enumerated id list; realized scenario_ids are recorded after the first
  mode runs and every other mode is checked equal to them.
- (#3 B) A dirty worktree is recorded honestly (``dirty: true``) but does NOT
  disqualify a run. git state is READ ONLY here — this module never commits.

No API keys or raw vault values are read or written by this module.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

FREEZE_FILENAME = "frozen_meta.json"
SUBSET_FILENAME = "subset.json"
PROTOCOL_SNAPSHOT_FILENAME = "protocol_snapshot.md"
PROTOCOL_SHA_FILENAME = "protocol_snapshot.sha256"


def _git(repo: Path, *args: str) -> str | None:
    """Run a read-only git command in ``repo``; return stripped stdout or None."""

    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def collect_git_state(repo_path: str | Path | None) -> dict[str, Any]:
    """Read-only snapshot of a repo's HEAD commit and dirty status.

    ``dirty`` is true if the worktree has staged/unstaged changes OR if the given
    path is entirely untracked (the Phase 5 code is untracked, which still counts
    as not-cleanly-committed). Never commits, never mutates the repo.
    """

    result: dict[str, Any] = {
        "path": str(repo_path) if repo_path is not None else None,
        "commit": None,
        "dirty": True,
        "tracked": False,
    }
    if repo_path is None:
        return result
    repo = Path(repo_path)
    if not repo.exists():
        return result

    commit = _git(repo, "rev-parse", "HEAD")
    if commit is None:
        # Not a git repo (or git unavailable): unknown commit, treat as dirty.
        return result
    result["commit"] = commit

    # Is the given path tracked at all?
    tracked = _git(repo, "ls-files", "--", str(repo))
    # A porcelain status limited to the repo root tells us dirty-ness.
    status = _git(repo, "status", "--porcelain")
    result["tracked"] = bool(tracked)
    result["dirty"] = bool(status) if status is not None else True
    return result


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def freeze_formal_run(
    *,
    run_id: str,
    runs_dir: str | Path,
    seed: int,
    n: int,
    selection_rule: str,
    model: str,
    provider: str,
    guard_modes: list[str] | tuple[str, ...],
    protocol_path: str | Path,
    contexthub_repo: str | Path | None,
    agentleak_repo: str | Path | None,
    generator_commit: str | None = None,
    probe_status: str = "not_run",
    now: str | None = None,
) -> dict[str, Any]:
    """Write the freeze bundle for a formal run BEFORE generation.

    Returns the frozen_meta dict. Writes four files under ``runs/<run_id>/``:
    the protocol snapshot, its sha256, the subset recipe, and frozen_meta.json.
    The generator commit is captured from the AgentLeak repo state if not given.
    """

    run_dir = Path(runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    protocol_text = Path(protocol_path).read_text(encoding="utf-8")
    protocol_sha = hash_text(protocol_text)
    (run_dir / PROTOCOL_SNAPSHOT_FILENAME).write_text(protocol_text, encoding="utf-8")
    (run_dir / PROTOCOL_SHA_FILENAME).write_text(protocol_sha + "\n", encoding="utf-8")

    contexthub_git = collect_git_state(contexthub_repo)
    agentleak_git = collect_git_state(agentleak_repo)
    resolved_generator_commit = generator_commit or agentleak_git.get("commit")

    subset = {
        "mode": "recipe",
        "seed": int(seed),
        "n": int(n),
        "selection_rule": selection_rule,
        "generator_commit": resolved_generator_commit,
        # Filled after the first mode generates; every other mode must match.
        "realized_scenario_ids": None,
    }
    _write_json(run_dir / SUBSET_FILENAME, subset)

    frozen_meta = {
        "run_id": run_id,
        "run_class": "formal",
        "frozen_at": now or datetime.now(UTC).replace(microsecond=0).isoformat(),
        "protocol_snapshot_sha256": protocol_sha,
        "protocol_snapshot_path": str(run_dir / PROTOCOL_SNAPSHOT_FILENAME),
        "subset_path": str(run_dir / SUBSET_FILENAME),
        "model": model,
        "provider": provider,
        "guard_modes": list(guard_modes),
        "probe_status": probe_status,
        "contexthub_git": contexthub_git,
        "agentleak_source": {
            "repo_url": "https://github.com/Privatris/AgentLeak",
            "local_path": agentleak_git.get("path"),
            "commit": agentleak_git.get("commit"),
            "dirty": agentleak_git.get("dirty"),
        },
    }
    _write_json(run_dir / FREEZE_FILENAME, frozen_meta)
    return frozen_meta


def load_freeze_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    """Load frozen_meta + subset from a freeze bundle directory."""

    d = Path(bundle_dir)
    frozen = json.loads((d / FREEZE_FILENAME).read_text(encoding="utf-8"))
    subset = json.loads((d / SUBSET_FILENAME).read_text(encoding="utf-8"))
    frozen["subset"] = subset
    return frozen


def record_realized_subset(bundle_dir: str | Path, scenario_ids: list[str]) -> list[str]:
    """Lock the first mode's realized scenario_ids into the subset recipe.

    Idempotent-ish: if already recorded, leaves the existing lock untouched and
    returns it (so re-running the first mode cannot silently change the lock).
    """

    subset_path = Path(bundle_dir) / SUBSET_FILENAME
    subset = json.loads(subset_path.read_text(encoding="utf-8"))
    if subset.get("realized_scenario_ids"):
        return list(subset["realized_scenario_ids"])
    locked = sorted({str(s) for s in scenario_ids})
    subset["realized_scenario_ids"] = locked
    _write_json(subset_path, subset)
    return locked


def verify_freeze(
    *,
    bundle_dir: str | Path,
    protocol_path: str | Path,
    observed_scenario_ids: list[str],
    observed_model: str | None = None,
) -> dict[str, Any]:
    """Post-hoc check that execution matched the frozen declaration.

    Verifies: (1) the on-disk protocol still hashes to the frozen snapshot;
    (2) the model did not change mid-run; (3) the observed scenario subset equals
    the locked subset (all compared systems must share it). Returns a dict with a
    boolean ``verified`` and a list of ``failures`` (empty when verified).
    """

    failures: list[str] = []
    d = Path(bundle_dir)
    try:
        frozen = load_freeze_bundle(d)
    except (OSError, json.JSONDecodeError) as exc:
        return {"verified": False, "failures": [f"cannot load freeze bundle: {exc}"]}

    if str(frozen.get("run_class")) != "formal":
        failures.append("freeze bundle run_class is not 'formal'")

    # (1) protocol snapshot integrity
    frozen_sha = frozen.get("protocol_snapshot_sha256")
    current_sha = hash_text(Path(protocol_path).read_text(encoding="utf-8"))
    if frozen_sha != current_sha:
        failures.append("protocol snapshot hash mismatch (protocol changed since freeze)")

    # (2) model stability
    if observed_model is not None and frozen.get("model") not in (None, observed_model):
        failures.append(
            f"model changed mid-run: frozen={frozen.get('model')} observed={observed_model}"
        )

    # (3) subset match — the locked set must equal what this mode observed.
    locked = frozen.get("subset", {}).get("realized_scenario_ids")
    observed = sorted({str(s) for s in observed_scenario_ids})
    if not locked:
        failures.append("subset not locked (realized_scenario_ids empty in freeze bundle)")
    elif sorted(locked) != observed:
        only_frozen = sorted(set(locked) - set(observed))
        only_observed = sorted(set(observed) - set(locked))
        failures.append(
            "scenario subset differs from frozen lock: "
            f"missing={only_frozen[:5]} extra={only_observed[:5]}"
        )

    return {
        "verified": not failures,
        "failures": failures,
        "frozen_meta": frozen,
        "protocol_sha256": current_sha,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
