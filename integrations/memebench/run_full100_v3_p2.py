"""Frozen-v3 P1 + production durable-P2 full100 development runner.

This is deliberately a new, versioned runner.  It never calls the chronological
60/40 harness and never reads scoring gold while constructing the runtime graph.
The ``smoke`` entry point uses local deterministic clients only.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from contexthub.llm.base import NoOpEmbeddingClient
from contexthub.models.context import ContextLevel, ContextType, Scope
from contexthub.models.request import RequestContext
from contexthub.models.search import SearchRequest
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.masking_service import MaskingService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.context_service import ContextService
from contexthub.services.propagation_engine import PropagationEngine
from integrations.memebench.planned_propagation import (
    PlannedDerivedMemoryRule,
    make_planned_registry,
    plan_published_graph,
)
from integrations.memebench.common import bind_root_plan, wipe_account
from integrations.memebench.answer import (
    _ANSWER_PROMPT,
    _ANSWER_PROMPT_WITH_NOTICES,
    build_answer_prompt,
)
from integrations.memebench.ingest import EVAL_AGENT
from integrations.memebench.abs_judge import score_abs_gold
from integrations.memebench.judge import _JUDGE_PROMPT, matches
from integrations.memebench.common import DEFAULT_DATA, _token_delta, _token_snap

SCHEMA_VERSION = "meme-full100-v3-p2-e2e-v3"
SCHEMA_GENERATION = "v3"
MODE_FAMILY = "full100-v3-p2-e2e-v3"
# MEME task types this runner can score. "Cas" asks for the propagated value;
# "Abs" asks the model to abstain and say what changed, and is judged by
# abs_judge's three-part check rather than judge.matches.
SUPPORTED_TASK_TYPES = ("Cas", "Abs")
# Preflight entries that carry recorded context rather than a pass/fail gate.
# Everything else in the preflight dict must be a boolean and must be True for
# the run to be authorized.
PREFLIGHT_CONTEXT_KEYS = frozenset(
    {
        "abs_excluded_count",
        "artifact_schema",
        "canonical_view_count",
        "checkpoint_identity",
        "command_mode",
        "disk_bytes_free",
        "evaluation_hop",
        "evaluation_view_count",
        "frontier_bounds",
        "input_hashes",
        "mode_family",
        "provider_config",
        "run_config",
        "run_config_hash",
        "schema_generation",
        "task_type",
    }
)

TRACE_SECTIONS = (
    "authorization",
    "extraction",
    "p1_graph",
    "p2_queue",
    "p2_edges",
    "state_evidence",
    "retrieval",
    "answers",
    "judge",
    "cost",
)
ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations/memebench/runs"
DEFAULT_SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
DEFAULT_V3 = RUNS / "p1_full100_candidate_envelope_reevaluation_20260824_v2"
DEFAULT_OUT = RUNS / "full100_v3_p2_e2e_v3"
DEFAULT_PROVIDERS_PATH = ROOT / "model_providers.local.json"
EXPECTED_PAID_USAGE_BUCKETS = {
    "inference_llm",
    "oracle_llm",
    "p2_cheap_llm",
    "judge_llm",
}
EXTERNAL_CALL_CONTRACT = {
    "answer": {
        "stages": {"before.answer", "off.answer", "on.answer"},
        "bucket": "inference_llm",
        "model_key": "chat_model",
        "provider_key": "provider",
        "trace": "answers",
    },
    "judge": {
        "stages": {"before.judge", "off.judge", "on.judge"},
        "bucket": "judge_llm",
        "model_key": "judge_model",
        "provider_key": "provider",
        "trace": "judge",
    },
    "p2_cheap": {
        "stages": {"p2.cheap"},
        "bucket": "p2_cheap_llm",
        "model_key": "p2_cheap_model",
        "provider_key": "provider",
        "trace": "p2_edges",
    },
    "p2_strong": {
        "stages": {"p2.strong"},
        "bucket": "oracle_llm",
        "model_key": "p2_strong_model",
        "provider_key": "provider",
        "trace": "p2_edges",
    },
}
ALLOWED_EXTERNAL_CALL_STAGES = frozenset(
    stage
    for contract in EXTERNAL_CALL_CONTRACT.values()
    for stage in contract["stages"]
)
BEHAVIOR_SOURCE_PATHS = (
    "integrations/memebench/run_full100_v3_p2.py",
    "integrations/memebench/planned_propagation.py",
    # Was run_chronological_p1p2.py, which was listed only because this runner
    # borrowed bind_root_plan/wipe_account from it.  Those two functions now live
    # in common.py verbatim, so common.py is the file whose bytes actually govern
    # this run's behavior.  The chronological runner's own logic never executes here.
    "integrations/memebench/common.py",
    "integrations/memebench/systems.py",
    "integrations/memebench/answer.py",
    "integrations/memebench/judge.py",
    "integrations/memebench/cost.py",
    "integrations/memebench/loader.py",
    "integrations/memebench/ingest.py",
    "src/contexthub/db/repository.py",
    "src/contexthub/models/search.py",
    "src/contexthub/services/context_service.py",
    "src/contexthub/services/propagation_engine.py",
    "src/contexthub/services/lifecycle_service.py",
    "src/contexthub/services/retrieval_service.py",
    "src/contexthub/services/semantic_identity.py",
    "src/contexthub/services/acl_service.py",
    "src/contexthub/services/masking_service.py",
    "src/contexthub/store/context_store.py",
    "src/contexthub/retrieval/router.py",
    "src/contexthub/retrieval/rerank.py",
    "src/contexthub/retrieval/keyword_strategy.py",
    "src/contexthub/retrieval/vector_strategy.py",
    "integrations/memebench/embedding_retry.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/llm/openai_client.py",
    "src/contexthub/db/codecs.py",
)
DYNAMIC_BEHAVIOR_SOURCE_PATHS = (
    "src/contexthub/config.py",
    "integrations/memebench/systems.py",
)
DIRECT_CONTRACT = {
    "J1": {"expected_cost": 0.0, "delta": 1.0},
    "J3": {"expected_cost": 1.0, "delta": 1.0},
    "J4": {"expected_cost": 1.0, "delta": 1.0},
    "cascade": {"expected_cost": 1.0, "delta": 1.0},
    "direct-stale": {"expected_cost": 2.0, "delta": 0.0},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    atomic_bytes(path, (canonical_json(payload) + "\n").encode("utf-8"))


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sanitize_provider_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            name = str(key)
            lowered = name.casefold()
            if any(
                marker in lowered
                for marker in ("key", "token", "secret", "password", "authorization")
            ):
                safe[name] = {
                    "credential_name": name,
                    "credential_source": "provider_config",
                    "present": bool(item),
                }
            else:
                safe[name] = _sanitize_provider_value(item)
        return safe
    if isinstance(value, list):
        return [_sanitize_provider_value(item) for item in value]
    return value


def sanitized_provider_config(config: Mapping[str, Any]) -> dict[str, Any]:
    safe = _sanitize_provider_value(config)
    if not isinstance(safe, Mapping):
        raise TypeError("provider config projection must be a mapping")
    return {
        "sha256": sha256_bytes(canonical_json(safe).encode()),
        "fields": dict(safe),
    }


def provider_snapshot_from_bytes(
    provider_bytes: bytes,
    *,
    labels: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = json.loads(provider_bytes)
    if not isinstance(document, Mapping):
        raise ValueError("provider configuration must be a JSON object")
    targets = {
        str(item.get("label")): dict(item)
        for item in document.get("targets") or []
        if isinstance(item, Mapping) and item.get("label")
    }
    selected: dict[str, Any] = {}
    for label in sorted(set(labels)):
        if label not in targets:
            raise ValueError(f"provider label {label!r} is missing")
        selected[label] = targets[label]
    full_snapshot = {"targets": selected}
    sanitized = sanitized_provider_config(full_snapshot)
    return full_snapshot, sanitized


def _bundle_entry_from_bytes(
    path: Path,
    data: bytes,
    *,
    role: str,
    project_root: Path,
) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        display_path = str(resolved.relative_to(project_root))
    except ValueError:
        display_path = str(resolved)
    return {
        "path": display_path,
        "resolved_path": str(resolved),
        "role": role,
        "sha256": sha256_bytes(data),
        "byte_count": len(data),
    }


def _bundle_entry(path: Path, *, role: str, project_root: Path) -> dict[str, Any]:
    return _bundle_entry_from_bytes(
        path,
        path.resolve().read_bytes(),
        role=role,
        project_root=project_root,
    )


def _module_name_for_path(path: Path, project_root: Path) -> str | None:
    resolved = path.resolve()
    for base in (project_root / "src", project_root):
        try:
            relative = resolved.relative_to(base.resolve())
        except ValueError:
            continue
        if relative.suffix != ".py":
            return None
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return None


def _resolve_local_module(
    module: str,
    *,
    project_root: Path,
) -> list[Path]:
    if not module.startswith(("contexthub", "integrations.memebench")):
        return []
    relative = Path(*module.split("."))
    candidates = []
    for base in (project_root / "src", project_root):
        module_file = base / relative.with_suffix(".py")
        package_file = base / relative / "__init__.py"
        if module_file.is_file():
            candidates.append(module_file.resolve())
        if package_file.is_file():
            candidates.append(package_file.resolve())
    return candidates


def local_import_closure(
    project_root: Path,
    entry_paths: Sequence[str] = BEHAVIOR_SOURCE_PATHS,
) -> list[Path]:
    project_root = project_root.resolve()
    pending = [
        *(project_root / relative for relative in entry_paths),
        *(project_root / relative for relative in DYNAMIC_BEHAVIOR_SOURCE_PATHS),
    ]
    seen: set[Path] = set()
    while pending:
        path = pending.pop().resolve()
        if path in seen:
            continue
        if not path.is_file():
            raise RuntimeError(f"runtime behavior source is missing: {path}")
        seen.add(path)
        module_name = _module_name_for_path(path, project_root)
        if module_name:
            parts = module_name.split(".")
            for index in range(1, len(parts)):
                pending.extend(
                    _resolve_local_module(
                        ".".join(parts[:index]), project_root=project_root
                    )
                )
        tree = ast.parse(path.read_bytes(), filename=str(path))
        package = (module_name or "").split(".")
        if path.name != "__init__.py" and package:
            package.pop()
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = max(0, len(package) - node.level + 1)
                    prefix = package[:keep]
                    if node.module:
                        prefix.extend(node.module.split("."))
                    modules.append(".".join(prefix))
                elif node.module:
                    modules.append(node.module)
            for module in modules:
                pending.extend(_resolve_local_module(module, project_root=project_root))
    return sorted(seen)


def behavior_source_entries(project_root: Path | None = None) -> list[dict[str, Any]]:
    project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    paths = local_import_closure(project_root)
    paths.extend(sorted((project_root / "alembic/versions").glob("*.py")))
    entries = [
        _bundle_entry(
            path,
            role=(
                "database_schema_migration"
                if path.parent.name == "versions"
                else "runtime_behavior_source"
            ),
            project_root=project_root,
        )
        for path in paths
    ]
    if len({entry["resolved_path"] for entry in entries}) != len(entries):
        raise RuntimeError("behavior source allowlist contains duplicate paths")
    return sorted(entries, key=lambda entry: (entry["role"], entry["path"]))


def build_run_identity(
    corpus: "FrozenV3",
    *,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    with_stale_notices: bool = True,
    models: Mapping[str, str],
    prices: Mapping[str, Any],
    provider_config: Mapping[str, Any],
    data: str | Path = DEFAULT_DATA,
    price_table_path: str | Path | None = None,
    price_table_bytes: bytes | None = None,
    provider_config_path: str | Path | None = None,
    provider_config_bytes: bytes | None = None,
) -> dict[str, Any]:
    if evaluation_hop not in {1, 2}:
        raise ValueError("evaluation_hop must be 1 or 2")
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of {sorted(SUPPORTED_TASK_TYPES)}")
    input_hashes = corpus.input_hashes(data)
    project_root = Path(__file__).resolve().parents[2].resolve()
    input_bundle = [
        *corpus.input_bundle_entries(data),
        *behavior_source_entries(project_root),
    ]
    if price_table_path is not None:
        frozen_price_bytes = (
            price_table_bytes
            if price_table_bytes is not None
            else Path(price_table_path).read_bytes()
        )
        price_entry = _bundle_entry_from_bytes(
            Path(price_table_path),
            frozen_price_bytes,
            role="price_table",
            project_root=project_root,
        )
        if json.loads(frozen_price_bytes) != prices:
            raise ValueError("price table values do not match frozen file bytes")
    else:
        frozen_price_bytes = canonical_json(prices).encode()
        price_entry = {
            "path": "<sanitized-price-table-preimage>",
            "resolved_path": None,
            "role": "price_table",
            "sha256": sha256_bytes(frozen_price_bytes),
            "byte_count": len(frozen_price_bytes),
        }
    input_bundle.append(price_entry)
    sanitized_provider = sanitized_provider_config(provider_config)
    if provider_config_path is not None:
        if provider_config_bytes is None:
            provider_config_bytes = Path(provider_config_path).read_bytes()
        labels = tuple(
            str(label)
            for label in (
                provider_config.get("provider"),
                provider_config.get("embedding_provider"),
            )
            if label
        )
        _, projection = provider_snapshot_from_bytes(
            provider_config_bytes,
            labels=labels,
        )
        sanitized_provider = projection
        projection_bytes = canonical_json(projection["fields"]).encode()
        provider_entry = {
            "path": str(Path(provider_config_path).resolve()),
            "resolved_path": str(Path(provider_config_path).resolve()),
            "role": "provider_config_sanitized",
            "sha256": sha256_bytes(projection_bytes),
            "byte_count": len(projection_bytes),
            "provider_labels": sorted(set(labels)),
        }
    else:
        projection_bytes = canonical_json(sanitized_provider["fields"]).encode()
        provider_entry = {
            "path": "<sanitized-provider-config-preimage>",
            "resolved_path": None,
            "role": "provider_config_sanitized",
            "sha256": sha256_bytes(projection_bytes),
            "byte_count": len(projection_bytes),
            "provider_labels": [],
        }
    input_bundle.append(provider_entry)
    input_bundle.sort(key=lambda entry: (entry["role"], entry["path"]))
    config = {
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "mode_family": MODE_FAMILY,
        "alembic_revision": "008",
        "evaluation_hop": evaluation_hop,
        # Without this, a Cas run and an Abs run at the same hop and models hash
        # identically, and every downstream gate would accept one's artifacts as
        # authorization for the other.
        "task_type": task_type,
        # Retrieval behaviour, not a per-task-type tweak: when a node is withheld
        # the system explains why, for every task type. It is in the identity
        # because turning it off is a different system, and the off arm is how
        # "does explaining hurt Cas" gets measured under one source tree.
        "with_stale_notices": with_stale_notices,
        "input_hashes": input_hashes,
        "input_bundle": input_bundle,
        "models": dict(sorted(models.items())),
        "provider_config": sanitized_provider,
        "provider_bindings": {
            key: str(provider_config[key])
            for key in ("provider", "embedding_provider")
            if provider_config.get(key)
        },
        "price_table": prices,
        "prompt_versions": {
            "answer_sha256": sha256_bytes(_ANSWER_PROMPT.encode()),
            "judge_sha256": sha256_bytes(_JUDGE_PROMPT.encode()),
            # Recorded for every run so the identity says which templates were
            # reachable. Only Abs runs actually reach the notices variant.
            "answer_with_notices_sha256": sha256_bytes(
                _ANSWER_PROMPT_WITH_NOTICES.encode()
            ),
        },
    }
    return {
        "run_config_hash": sha256_bytes(canonical_json(config).encode()),
        "input_hashes": input_hashes,
        "config": config,
    }


def verify_run_identity(
    identity: Mapping[str, Any],
    *,
    corpus: "FrozenV3" | None = None,
    data: str | Path = DEFAULT_DATA,
) -> None:
    config = identity.get("config")
    if not isinstance(config, Mapping):
        raise RuntimeError("run identity lacks config preimage")
    recomputed = sha256_bytes(canonical_json(config).encode())
    if recomputed != identity.get("run_config_hash"):
        raise RuntimeError("run identity config hash changed")
    if (
        config.get("schema_version") != SCHEMA_VERSION
        or config.get("schema_generation") != SCHEMA_GENERATION
        or config.get("mode_family") != MODE_FAMILY
    ):
        raise RuntimeError("run identity schema generation mismatch")
    for entry in config.get("input_bundle") or ():
        resolved_path = entry.get("resolved_path")
        if resolved_path is None:
            continue
        path = Path(str(resolved_path))
        data_bytes = path.read_bytes()
        if entry.get("role") == "provider_config_sanitized":
            _, projection = provider_snapshot_from_bytes(
                data_bytes,
                labels=entry.get("provider_labels") or (),
            )
            data_bytes = canonical_json(projection["fields"]).encode()
        if len(data_bytes) != entry.get("byte_count") or sha256_bytes(
            data_bytes
        ) != entry.get("sha256"):
            raise RuntimeError(f"run identity input changed: {path}")
    if corpus is not None:
        corpus.verify_identity(data)


def _contains_prior_generation_token(path: Path) -> bool:
    return any(
        token.casefold() in {"v1", "v2"}
        for component in path.parts
        for token in re.split(r"[^A-Za-z0-9]+", component)
        if token
    )


def _iter_output_tree(out: Path) -> list[Path]:
    pending = [out]
    observed: list[Path] = []
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            if child.is_symlink():
                raise RuntimeError(f"v2 output layout forbids symlink: {child}")
            observed.append(child)
            if child.is_dir():
                pending.append(child)
    return observed


def _recover_output_transients(out: Path) -> None:
    temporary_pattern = re.compile(r"^\.(?P<target>.+)\.[0-9a-f]{32}\.tmp$")
    for path in sorted(
        _iter_output_tree(out), key=lambda item: len(item.parts), reverse=True
    ):
        if not path.is_file():
            continue
        match = temporary_pattern.fullmatch(path.name)
        if match is None:
            continue
        target = path.with_name(match.group("target"))
        if target.exists():
            path.unlink()
        else:
            os.replace(path, target)
    staging = out / ".staging"
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError("v2 staging path is not a real directory")
        shutil.rmtree(staging)


def _validate_output_layout(out: Path, *, command_mode: str) -> None:
    phase = {
        "preflight": 0,
        "no-api": 1,
        "paid-smoke": 2,
        "full-run": 3,
    }[command_mode]
    root_files = {
        "run_identity.json",
        "preflight.json",
        "offline_status_preflight.json",
    }
    if phase >= 1:
        root_files.add("smoke_result.json")
    if phase >= 2:
        root_files.update(
            {
                "paid_smoke_result.json",
                "paid_smoke_cases.jsonl",
                "paid_smoke_cases.jsonl.lock",
            }
        )
    if phase >= 3:
        root_files.add("run_cost_summary.json")
    allowed_namespaces = {"no-api"} if phase >= 1 else set()
    if phase >= 2:
        allowed_namespaces.add("paid-smoke")
    if phase >= 3:
        allowed_namespaces.add("full-run")
    episode = r"[a-z]{2}_\d{3}"
    task_type_pattern = f"(?:{'|'.join(SUPPORTED_TASK_TYPES)})"
    paid_case = rf"{episode}-{task_type_pattern}-[0-9a-f]{{12}}"
    # Checkpoints are per episode per task type: one file per episode would let a
    # Cas run and an Abs run overwrite each other's resume state.
    checkpoint_case = rf"{episode}-{task_type_pattern}"
    allowed_directories = {
        "checkpoints",
        "cases",
        "artifacts",
        *{f"checkpoints/{namespace}" for namespace in allowed_namespaces},
    }
    if phase >= 2:
        allowed_directories.add("artifacts/paid-smoke")
    if phase >= 3:
        allowed_directories.add("artifacts/full-run")
    for path in _iter_output_tree(out):
        relative = path.relative_to(out).as_posix()
        if path.is_dir():
            if relative == ".staging" or relative.startswith(".staging/"):
                raise RuntimeError(
                    "v2 output contains an unrecovered staging directory"
                )
            if re.fullmatch(rf"cases/{episode}", relative) and phase >= 1:
                continue
            if (
                re.fullmatch(rf"artifacts/paid-smoke/{paid_case}", relative)
                and phase >= 2
            ):
                continue
            if (
                re.fullmatch(rf"artifacts/full-run/{paid_case}", relative)
                and phase >= 3
            ):
                continue
            if relative not in allowed_directories:
                raise RuntimeError(f"v2 output contains unknown directory: {relative}")
            continue
        if "/" not in relative and relative in root_files:
            continue
        if (
            re.fullmatch(
                rf"checkpoints/({'|'.join(sorted(allowed_namespaces))})/"
                rf"{checkpoint_case}\.json(?:\.lock)?",
                relative,
            )
            if allowed_namespaces
            else False
        ):
            continue
        if phase >= 1 and re.fullmatch(
            rf"cases/{episode}/(?:artifact|manifest)\.json", relative
        ):
            continue
        if phase >= 2 and re.fullmatch(
            rf"artifacts/paid-smoke/{paid_case}/(?:artifact|manifest)\.json",
            relative,
        ):
            continue
        if phase >= 3 and re.fullmatch(
            rf"artifacts/full-run/{paid_case}/(?:artifact|manifest)\.json",
            relative,
        ):
            continue
        raise RuntimeError(f"v2 output contains unknown file: {relative}")


def ensure_output_directory(
    out: Path,
    identity: Mapping[str, Any],
    *,
    command_mode: str,
) -> None:
    if command_mode not in {"preflight", "no-api", "paid-smoke", "full-run"}:
        raise ValueError(f"unknown command mode: {command_mode}")
    if _contains_prior_generation_token(out):
        raise RuntimeError("v3 runner refuses a v1/v2 output directory")
    out.mkdir(parents=True, exist_ok=True)
    _recover_output_transients(out)
    marker_path = out / "run_identity.json"
    expected_marker = {
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "mode_family": MODE_FAMILY,
        "run_config_hash": identity["run_config_hash"],
        "allowed_command_modes": ["preflight", "no-api", "paid-smoke", "full-run"],
    }
    existing = {path.name for path in out.iterdir()}
    if existing and not marker_path.is_file():
        raise RuntimeError("non-empty output directory lacks v2 identity marker")
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("output directory identity marker is malformed") from exc
        if marker != expected_marker:
            raise RuntimeError("output directory schema/config/mode identity mismatch")
    else:
        atomic_json(marker_path, expected_marker)
    _validate_output_layout(out, command_mode=command_mode)
    for namespace_dir in (out / "checkpoints").glob("*"):
        if namespace_dir.name not in {"no-api", "paid-smoke", "full-run"}:
            raise RuntimeError(f"unexpected checkpoint namespace: {namespace_dir.name}")
        for checkpoint_file in namespace_dir.glob("*.json"):
            try:
                checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"malformed checkpoint in {namespace_dir.name}"
                ) from exc
            if (
                checkpoint.get("checkpoint_namespace") != namespace_dir.name
                or checkpoint.get("run_config_hash") != identity["run_config_hash"]
            ):
                raise RuntimeError("checkpoint namespace/config identity mismatch")
    existing_artifact_roots = (
        (out / "cases", "no-api"),
        (out / "artifacts" / "paid-smoke", "paid-smoke"),
        (out / "artifacts" / "full-run", "full-run"),
    )
    for root, expected_mode in existing_artifact_roots:
        for artifact_file in root.glob("*/artifact.json"):
            try:
                artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"malformed {expected_mode} artifact") from exc
            if (
                artifact.get("schema_version") != SCHEMA_VERSION
                or artifact.get("schema_generation") != SCHEMA_GENERATION
                or artifact.get("artifact_mode") != expected_mode
                or (artifact.get("authorization") or {}).get("run_config_hash")
                != identity["run_config_hash"]
            ):
                raise RuntimeError("artifact schema/config/mode identity mismatch")
    verify_run_identity(identity)


def _load_success_gate(
    path: Path,
    *,
    kind: str,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{kind} gate artifact is missing: {path}")
    row = json.loads(path.read_text(encoding="utf-8"))
    if row.get("success") is not True:
        raise RuntimeError(f"{kind} gate did not succeed")
    if row.get("run_config_hash") != identity["run_config_hash"]:
        raise RuntimeError(f"{kind} gate run_config_hash mismatch")
    if row.get("input_hashes") != identity["input_hashes"]:
        raise RuntimeError(f"{kind} gate input hashes mismatch")
    if row.get("run_config") != identity["config"]:
        raise RuntimeError(f"{kind} sanitized run config mismatch")
    if (
        sha256_bytes(canonical_json(row["run_config"]).encode())
        != row["run_config_hash"]
    ):
        raise RuntimeError(f"{kind} sanitized run config hash is not reproducible")
    return row


def identity_task_type(identity: Mapping[str, Any]) -> str:
    """The task type a run identity was built for, defaulting to Cas.

    Identities written before task_type entered the config have no such key; they
    were all Cas runs, so that is the fallback.
    """
    config = identity.get("config") or {}
    return str(config.get("task_type") or "Cas")


def identity_with_stale_notices(identity: Mapping[str, Any]) -> bool:
    """Whether this run explains withheld nodes to the answering model.

    Identities frozen before this key existed never explained them, so they read
    back as False and stay reproducible.
    """
    config = identity.get("config") or {}
    return bool(config.get("with_stale_notices", False))


def checkpoint_path(
    out: Path, namespace: str, episode_id: str, task_type: str = "Cas"
) -> Path:
    """Resume file for one episode of one task type.

    The task type is in the filename because a Cas run and an Abs run cover the
    same 100 episodes: sharing ``pl_001.json`` would make each run resume from
    the other's progress and skip cases it never scored.
    """
    if namespace not in {"no-api", "paid-smoke", "full-run"}:
        raise ValueError(f"unknown checkpoint namespace: {namespace}")
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of {sorted(SUPPORTED_TASK_TYPES)}")
    return out / "checkpoints" / namespace / f"{episode_id}-{task_type}.json"


def require_paid_case_gate(out: Path, identity: Mapping[str, Any]) -> None:
    _load_success_gate(out / "preflight.json", kind="preflight", identity=identity)
    smoke = _load_success_gate(
        out / "smoke_result.json", kind="no-API smoke", identity=identity
    )
    if smoke.get("synthetic_stub") is not True or smoke.get("case_count") != 2:
        raise RuntimeError("no-API gate must be an exact 2-case synthetic smoke")
    completed = smoke.get("completed") or []
    if len(completed) != 2:
        raise RuntimeError("no-API gate must name exactly two completed cases")
    for episode_id in completed:
        artifact_path = out / "cases" / episode_id / "artifact.json"
        if not artifact_path.is_file():
            raise RuntimeError(f"no-API case artifact missing: {episode_id}")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        validate_case_artifact(artifact)
        checkpoint_file = checkpoint_path(
            out, "no-api", episode_id, identity_task_type(identity)
        )
        manifest_path = artifact_path.with_name("manifest.json")
        if not checkpoint_file.is_file() or not manifest_path.is_file():
            raise RuntimeError(f"no-API checkpoint/manifest missing: {episode_id}")
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = sha256_file(artifact_path)
        if (
            artifact.get("artifact_mode") != "no-api"
            or artifact.get("synthetic_stub") is not True
            or (artifact.get("authorization") or {}).get("run_config_hash")
            != identity["run_config_hash"]
            or checkpoint.get("checkpoint_namespace") != "no-api"
            or checkpoint.get("run_config_hash") != identity["run_config_hash"]
            or checkpoint.get("artifact_sha256") != digest
            or manifest.get("artifact_sha256") != digest
            or manifest.get("run_config_hash") != identity["run_config_hash"]
            or manifest.get("artifact_mode") != "no-api"
        ):
            raise RuntimeError(f"no-API evidence closure failed: {episode_id}")
        checks = artifact.get("stale_isolation_retrieval_verified") or {}
        if checks.get("integrity_complete") is not True:
            raise RuntimeError(f"no-API retrieval integrity failed: {episode_id}")


def require_full_run_gate(out: Path, identity: Mapping[str, Any]) -> None:
    require_paid_case_gate(out, identity)
    validate_paid_smoke_evidence(out, identity)


def validate_case_artifact(artifact: Mapping[str, Any]) -> None:
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("case artifact schema_version mismatch")
    if artifact.get("schema_generation") != SCHEMA_GENERATION:
        raise ValueError("case artifact schema_generation mismatch")
    if artifact.get("artifact_mode") not in {"no-api", "paid-smoke", "full-run"}:
        raise ValueError("case artifact mode is missing or invalid")
    if artifact.get("execution_complete") is not True:
        raise ValueError("case artifact execution is not complete")
    if artifact.get("runtime_status") not in {
        "executed",
        "root-binding-miss",
        "input-invalid",
        "global-integrity-error",
    }:
        raise ValueError("case artifact runtime_status is missing or invalid")
    if not isinstance(artifact.get("root_binding_status"), str):
        raise ValueError("case artifact root_binding_status is missing")
    for section in TRACE_SECTIONS:
        value = artifact.get(section)
        if value is None or value == {} or value == [] or value == "":
            raise ValueError(f"case artifact section {section!r} is empty")
    if artifact.get("development_reevaluation") is not True:
        raise ValueError("runner must preserve the development re-evaluation label")
    authorization = artifact.get("authorization") or {}
    config = authorization.get("run_config")
    if not isinstance(config, Mapping):
        raise ValueError("case artifact lacks sanitized run-config preimage")
    recomputed = sha256_bytes(canonical_json(config).encode())
    if (
        authorization.get("run_config_hash") != recomputed
        or authorization.get("run_config_hash_recomputed") != recomputed
        or authorization.get("secrets_persisted") is not False
    ):
        raise ValueError("case artifact run-config authorization is not closed")

    def reject_secret_values(value: Any, path: str = "$") -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                lowered = str(key).casefold()
                if re.search(
                    r"(?:^|_)(?:api_key|secret|password|access_token|"
                    r"refresh_token|bearer_token)$",
                    lowered,
                ):
                    metadata = isinstance(item, Mapping) and set(item) == {
                        "credential_name",
                        "credential_source",
                        "present",
                    }
                    if not metadata or not isinstance(item.get("present"), bool):
                        raise ValueError(
                            f"secret value leaked into artifact: {path}.{key}"
                        )
                else:
                    reject_secret_values(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                reject_secret_values(item, f"{path}[{index}]")

    reject_secret_values(artifact)
    serialized = canonical_json(artifact).lower()
    for marker in ("authorization: bearer", "provider_secret"):
        if marker in serialized:
            raise ValueError(f"secret marker leaked into artifact: {marker}")
    forbidden_audit_terms = (
        '"certified"',
        '"certification',
        "production recompute",
        "production_recompute",
    )
    if any(term in serialized for term in forbidden_audit_terms):
        raise ValueError("v2 artifact contains prohibited technical-approval wording")


def write_case_artifact(case_dir: Path, artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Persist immutable body first, then a hash manifest; cleanup comes later."""

    validate_case_artifact(artifact)
    body = case_dir / "artifact.json"
    manifest = case_dir / "manifest.json"
    atomic_json(body, artifact)
    digest = sha256_file(body)
    atomic_json(
        manifest,
        {
            "schema_version": SCHEMA_VERSION,
            "schema_generation": SCHEMA_GENERATION,
            "artifact_mode": artifact["artifact_mode"],
            "episode_id": artifact["episode_id"],
            "run_config_hash": artifact["authorization"]["run_config_hash"],
            "artifact_sha256": digest,
            "artifact_bytes": body.stat().st_size,
            "written_at": utc_now().isoformat(),
        },
    )
    if sha256_file(body) != json.loads(manifest.read_text())["artifact_sha256"]:
        raise OSError("case artifact hash verification failed")
    return json.loads(manifest.read_text())


@dataclass
class Checkpoint:
    path: Path
    run_config_hash: str
    namespace: str
    stale_after: timedelta = timedelta(minutes=20)
    attempt_token: str | None = None

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def claim(self) -> bool:
        with self._locked():
            now = utc_now()
            attempts: list[dict[str, Any]] = []
            row = self._read()
            if row:
                if row.get("run_config_hash") != self.run_config_hash:
                    raise ValueError(
                        "checkpoint belongs to a different run_config_hash"
                    )
                if row.get("checkpoint_namespace") != self.namespace:
                    raise ValueError("checkpoint belongs to a different namespace")
                if row.get("status") in {"success", "execution_complete", "finalized"}:
                    return False
                heartbeat = datetime.fromisoformat(row["heartbeat_at"])
                if (
                    row.get("status") == "in_progress"
                    and now - heartbeat <= self.stale_after
                ):
                    raise RuntimeError(
                        f"case already owned by {row.get('hostname')}:{row.get('pid')}"
                    )
                attempts = list(row.get("attempts") or [])
            attempt_token = str(uuid4())
            atomic_json(
                self.path,
                {
                    "status": "in_progress",
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "heartbeat_at": now.isoformat(),
                    "run_config_hash": self.run_config_hash,
                    "checkpoint_namespace": self.namespace,
                    "attempt_token": attempt_token,
                    "attempts": attempts,
                },
            )
            self.attempt_token = attempt_token
            return True

    def heartbeat(self) -> None:
        with self._locked():
            current = self._read()
            if current.get("run_config_hash") != self.run_config_hash:
                raise ValueError("checkpoint belongs to a different run_config_hash")
            if current.get("checkpoint_namespace") != self.namespace:
                raise ValueError("checkpoint belongs to a different namespace")
            if (
                current.get("status") != "in_progress"
                or current.get("attempt_token") != self.attempt_token
            ):
                raise RuntimeError("checkpoint attempt lease lost")
            current["heartbeat_at"] = utc_now().isoformat()
            atomic_json(self.path, current)

    def finish(self, status: str, **fields: Any) -> None:
        if status not in {"success", "failed", "execution_complete"}:
            raise ValueError(status)
        with self._locked():
            current = self._read()
            if current.get("run_config_hash") != self.run_config_hash:
                raise ValueError("checkpoint belongs to a different run_config_hash")
            if current.get("checkpoint_namespace") != self.namespace:
                raise ValueError("checkpoint belongs to a different namespace")
            if (
                current.get("status") != "in_progress"
                or current.get("attempt_token") != self.attempt_token
            ):
                raise RuntimeError("checkpoint attempt lease lost before finish")
            attempts = list(current.get("attempts") or [])
            attempts.append(
                {
                    "attempt_token": self.attempt_token,
                    "status": status,
                    "finished_at": utc_now().isoformat(),
                    **fields,
                }
            )
            atomic_json(
                self.path,
                {
                    "status": status,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "heartbeat_at": utc_now().isoformat(),
                    "run_config_hash": self.run_config_hash,
                    "checkpoint_namespace": self.namespace,
                    "attempt_token": self.attempt_token,
                    "attempts": attempts,
                    **fields,
                },
            )

    def finalize(self, **fields: Any) -> None:
        with self._locked():
            current = self._read()
            if current.get("run_config_hash") != self.run_config_hash:
                raise ValueError("checkpoint belongs to a different run_config_hash")
            if current.get("checkpoint_namespace") != self.namespace:
                raise ValueError("checkpoint belongs to a different namespace")
            if current.get("status") == "finalized":
                for key, value in fields.items():
                    if current.get(key) != value:
                        raise RuntimeError("finalized checkpoint evidence conflict")
                return
            if current.get("status") != "execution_complete":
                raise RuntimeError("checkpoint is not ready to finalize")
            atomic_json(self.path, {**current, **fields, "status": "finalized"})


async def _heartbeat_checkpoint(
    checkpoint: Checkpoint,
    stop: asyncio.Event,
    *,
    interval: float = 5.0,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            checkpoint.heartbeat()


def append_paid_case_index(out: Path, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    index_path = out / "paid_smoke_cases.jsonl"
    lock_path = index_path.with_suffix(".jsonl.lock")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            existing: list[dict[str, Any]] = []
            lines = (
                index_path.read_text(encoding="utf-8").splitlines()
                if index_path.exists()
                else []
            )
            nonempty = [
                (index, line) for index, line in enumerate(lines) if line.strip()
            ]
            for position, (line_index, line) in enumerate(nonempty):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    if position != len(nonempty) - 1:
                        raise RuntimeError("paid case index is malformed") from exc
                    break
                if not isinstance(parsed, dict):
                    raise RuntimeError(f"paid case index row {line_index} is malformed")
                existing.append(parsed)
            episode_id = str(row["episode_id"])
            duplicates = [
                item for item in existing if str(item.get("episode_id")) == episode_id
            ]
            if duplicates:
                if any(item != dict(row) for item in duplicates):
                    raise RuntimeError(
                        f"paid case artifact hash conflict for {episode_id}"
                    )
                if len(duplicates) != 1:
                    raise RuntimeError(f"duplicate paid case index entry: {episode_id}")
                return existing
            updated = [*existing, dict(row)]
            atomic_bytes(
                index_path,
                "".join(f"{canonical_json(item)}\n" for item in updated).encode(),
            )
            return updated
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def finalize_paid_case(
    out: Path,
    identity: Mapping[str, Any],
    episode_id: str,
    *,
    crash_after: str | None = None,
) -> dict[str, Any]:
    checkpoint = Checkpoint(
        checkpoint_path(out, "paid-smoke", episode_id, identity_task_type(identity)),
        str(identity["run_config_hash"]),
        "paid-smoke",
    )
    checkpoint_row = checkpoint._read()
    if checkpoint_row.get("status") not in {"execution_complete", "finalized"}:
        raise RuntimeError("paid checkpoint is not ready for finalization")
    artifact_path = _require_namespace_path(
        Path(str(checkpoint_row.get("artifact_path") or "")),
        out / "artifacts" / "paid-smoke",
        label="paid smoke artifact",
    )
    manifest_path = artifact_path.with_name("manifest.json")
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_case_artifact(artifact)
    artifact_digest = sha256_file(artifact_path)
    manifest_digest = sha256_file(manifest_path)
    if (
        artifact.get("episode_id") != episode_id
        or artifact.get("artifact_mode") != "paid-smoke"
        or artifact.get("synthetic_stub") is not False
        or artifact.get("case_success") is not True
        or manifest.get("artifact_sha256") != artifact_digest
        or manifest.get("artifact_bytes") != artifact_path.stat().st_size
        or checkpoint_row.get("artifact_sha256") != artifact_digest
    ):
        raise RuntimeError("paid execution evidence is not ready to finalize")
    validate_paid_cost(
        artifact["cost"],
        artifact=artifact,
        config=identity["config"],
    )
    attempts = checkpoint_row.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RuntimeError("paid checkpoint lacks attempt ledger")
    completed = attempts[-1]
    if (
        completed.get("status") != "execution_complete"
        or completed.get("attempt_token") != checkpoint_row.get("attempt_token")
        or completed.get("call_ledger_sha256") != artifact["cost"]["call_ledger_sha256"]
        or completed.get("usage") != artifact["cost"]["paid_execution"]["layers"]
        or completed.get("cost") != artifact["cost"]["paid_execution"]
    ):
        raise RuntimeError("checkpoint/artifact successful attempt evidence mismatch")
    failed = [attempt for attempt in attempts[:-1] if attempt.get("status") == "failed"]
    if [attempt.get("cost") for attempt in failed] != artifact["cost"][
        "failed_attempts"
    ]:
        raise RuntimeError("checkpoint/artifact retry cost ledger mismatch")
    index_row = {
        "episode_id": episode_id,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_digest,
        "manifest_sha256": manifest_digest,
        "run_config_hash": identity["run_config_hash"],
        "call_ledger_sha256": artifact["cost"]["call_ledger_sha256"],
        "attempt_token": completed["attempt_token"],
        "indexed_at": completed["finished_at"],
    }
    case_rows = append_paid_case_index(out, index_row)
    if crash_after == "index":
        raise RuntimeError("injected crash after paid index")
    derived = {
        "trace_complete": all(artifact.get(section) for section in TRACE_SECTIONS),
        "cost_acceptable": True,
        "cleanup_complete": artifact.get("cleanup", {}).get("complete") is True,
        "production_retrieval_change_verified": (
            (artifact.get("production_retrieval_change_verified") or {}).get(
                "integrity_complete"
            )
            is True
        ),
        "no_leftover": not artifact["p2_queue"].get("unfinished"),
        "no_dead_letter": not any(
            row.get("delivery_status") == "dead_letter"
            for row in artifact["p2_queue"].get("events") or []
        ),
    }
    summary = {
        "success": True,
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "artifact_mode": "paid-smoke",
        "synthetic_stub": False,
        "run_config_hash": identity["run_config_hash"],
        "input_hashes": identity["input_hashes"],
        "run_config": identity["config"],
        "checkpoint_namespace": "paid-smoke",
        "case_count": len(case_rows),
        "cases": case_rows,
        **derived,
        "artifact_sha256": artifact_digest,
        "call_ledger_sha256": artifact["cost"]["call_ledger_sha256"],
        "attempt_token": completed["attempt_token"],
    }
    atomic_json(out / "paid_smoke_result.json", summary)
    if crash_after == "summary":
        raise RuntimeError("injected crash after paid summary")
    checkpoint.finalize(
        index_sha256=sha256_file(out / "paid_smoke_cases.jsonl"),
        summary_sha256=sha256_file(out / "paid_smoke_result.json"),
        call_ledger_sha256=artifact["cost"]["call_ledger_sha256"],
    )
    return summary


def recover_paid_execution_checkpoint(
    out: Path,
    identity: Mapping[str, Any],
    episode_id: str,
) -> bool:
    checkpoint = Checkpoint(
        checkpoint_path(out, "paid-smoke", episode_id, identity_task_type(identity)),
        str(identity["run_config_hash"]),
        "paid-smoke",
    )
    with checkpoint._locked():
        row = checkpoint._read()
        if row.get("status") != "in_progress":
            return False
        candidates = sorted(
            (out / "artifacts" / "paid-smoke").glob(f"{episode_id}-*/artifact.json")
        )
        if not candidates:
            return False
        if len(candidates) != 1:
            raise RuntimeError("multiple paid artifacts prevent checkpoint recovery")
        artifact_path = candidates[0]
        manifest_path = artifact_path.with_name("manifest.json")
        if not manifest_path.is_file():
            raise RuntimeError("paid recovery artifact lacks manifest")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_case_artifact(artifact)
        artifact_digest = sha256_file(artifact_path)
        attempt_token = str(row.get("attempt_token") or "")
        if (
            artifact.get("episode_id") != episode_id
            or artifact.get("attempt_token") != attempt_token
            or artifact.get("artifact_mode") != "paid-smoke"
            or artifact.get("synthetic_stub") is not False
            or artifact.get("case_success") is not True
            or manifest.get("artifact_sha256") != artifact_digest
            or manifest.get("artifact_bytes") != artifact_path.stat().st_size
        ):
            raise RuntimeError("paid recovery artifact/checkpoint identity mismatch")
        validate_paid_cost(
            artifact["cost"],
            artifact=artifact,
            config=identity["config"],
        )
        checkpoint.attempt_token = attempt_token
        attempts = list(row.get("attempts") or [])
        retry_costs = [
            attempt.get("cost")
            for attempt in attempts
            if attempt.get("status") == "failed"
        ]
        if retry_costs != artifact["cost"].get("failed_attempts"):
            raise RuntimeError("paid recovery retry ledger mismatch")
    checkpoint.finish(
        "execution_complete",
        artifact_sha256=artifact_digest,
        artifact_path=str(artifact_path),
        usage=artifact["cost"]["paid_execution"]["layers"],
        cost=artifact["cost"]["paid_execution"],
        call_ledger_sha256=artifact["cost"]["call_ledger_sha256"],
        recovered_without_external_calls=True,
    )
    return True


def _finite_nonnegative(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise RuntimeError(f"{field} must be finite and nonnegative")
    return number


def _assert_close(actual: float, expected: float, *, field: str) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12):
        raise RuntimeError(f"{field} does not close: {actual} != {expected}")


def _validate_execution_cost(
    execution: Mapping[str, Any],
    *,
    field: str,
) -> float:
    complete = execution.get("cost_complete")
    if complete not in {True, False}:
        raise RuntimeError(f"{field}.cost_complete must be boolean")
    layers = execution.get("layers")
    if not isinstance(layers, Mapping) or set(layers) != EXPECTED_PAID_USAGE_BUCKETS:
        raise RuntimeError(f"{field}.layers has missing or extra usage buckets")
    layer_total = 0.0
    for bucket, row in layers.items():
        if not isinstance(row, Mapping):
            raise RuntimeError(f"{field}.layers.{bucket} is malformed")
        for token_field in ("calls", "prompt_tokens", "completion_tokens"):
            number = _finite_nonnegative(
                row.get(token_field), field=f"{field}.layers.{bucket}.{token_field}"
            )
            if not number.is_integer():
                raise RuntimeError(
                    f"{field}.layers.{bucket}.{token_field} must be integral"
                )
        if row.get("tokens_are_real") is not True:
            raise RuntimeError(f"{field}.layers.{bucket} lacks real token usage")
        retry_unknown = row.get("retry_usage_unknown")
        if retry_unknown not in {True, False}:
            raise RuntimeError(
                f"{field}.layers.{bucket}.retry_usage_unknown must be boolean"
            )
        price = row.get("price_snapshot")
        if not isinstance(price, Mapping):
            raise RuntimeError(f"{field}.layers.{bucket} lacks price snapshot")
        input_price = _finite_nonnegative(
            price.get("input_per_million"),
            field=f"{field}.layers.{bucket}.input_price",
        )
        output_price = _finite_nonnegative(
            price.get("output_per_million"),
            field=f"{field}.layers.{bucket}.output_price",
        )
        expected_usd = (
            _finite_nonnegative(
                row["prompt_tokens"],
                field=f"{field}.layers.{bucket}.prompt_tokens",
            )
            * input_price
            + _finite_nonnegative(
                row["completion_tokens"],
                field=f"{field}.layers.{bucket}.completion_tokens",
            )
            * output_price
        ) / 1_000_000
        known = _finite_nonnegative(
            row.get("known_usd"), field=f"{field}.layers.{bucket}.known_usd"
        )
        _assert_close(known, expected_usd, field=f"{field}.layers.{bucket}.known_usd")
        layer_total += known
    known_total = _finite_nonnegative(
        execution.get("known_usd"), field=f"{field}.known_usd"
    )
    _assert_close(known_total, layer_total, field=f"{field}.known_usd")
    interval = execution.get("strict_usd_interval")
    if not isinstance(interval, list) or len(interval) != 2:
        raise RuntimeError(f"{field}.strict_usd_interval is malformed")
    lower = _finite_nonnegative(interval[0], field=f"{field}.strict_usd_interval[0]")
    _assert_close(lower, known_total, field=f"{field}.strict_usd_interval[0]")
    if complete:
        upper = _finite_nonnegative(
            interval[1], field=f"{field}.strict_usd_interval[1]"
        )
        _assert_close(upper, known_total, field=f"{field}.strict_usd_interval[1]")
        if any(
            bool(row.get("retry_usage_unknown")) for row in execution["layers"].values()
        ):
            raise RuntimeError(f"{field} is exact but contains unknown retry usage")
    elif interval[1] is not None:
        upper = _finite_nonnegative(
            interval[1], field=f"{field}.strict_usd_interval[1]"
        )
        if upper < known_total:
            raise RuntimeError(f"{field} bounded interval is below known spend")
    elif not any(
        bool(row.get("retry_usage_unknown")) for row in execution["layers"].values()
    ):
        raise RuntimeError(f"{field} is unbounded without unknown retry usage")
    return known_total


def _call_trace_rows(artifact: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows: dict[str, Mapping[str, Any]] = {}
    answers = artifact.get("answers")
    if not isinstance(answers, Mapping):
        raise RuntimeError("paid artifact answers trace is malformed")
    for stage_name, row in answers.items():
        if not isinstance(row, Mapping) or not row.get("call_id"):
            raise RuntimeError(f"paid answer trace lacks call_id: {stage_name}")
        rows[str(row["call_id"])] = row
    judge = artifact.get("judge")
    if not isinstance(judge, Mapping) or not isinstance(judge.get("calls"), list):
        raise RuntimeError("paid artifact judge trace is malformed")
    for row in judge["calls"]:
        if not isinstance(row, Mapping) or not row.get("call_id"):
            raise RuntimeError("paid judge trace lacks call_id")
        call_id = str(row["call_id"])
        if call_id in rows:
            raise RuntimeError(f"duplicate trace call_id: {call_id}")
        rows[call_id] = row
    return rows


def parse_paid_judge_output(raw_output: str) -> tuple[str, bool]:
    parsed = (raw_output or "").strip().casefold()
    if parsed.startswith("correct"):
        return "correct", False
    if parsed.startswith("incorrect"):
        return "incorrect", False
    return "incorrect", True


def score_abs_stages(
    answers: Mapping[str, str | None],
    gold: Mapping[str, str | None],
    judge_records: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Deterministic three-part scoring for an Abs case, per stage.

    Abs gold is a sentence requiring three things at once (abstain, cite the
    prior value, name the changed upstream), so a single correct/incorrect bit
    cannot say which requirement failed. This is the primary criterion: zero API,
    zero judge noise, every label mechanically derived from the gold string.

    The LLM judge still runs and is recorded alongside; where the two disagree
    the case is listed in ``disagreements`` rather than one silently winning.
    The ``before`` stage asks for the plain prior value, not an abstention, so it
    is scored by the shared containment judge like any Cas answer.
    """
    stages: dict[str, Any] = {}
    for stage_name, reference in gold.items():
        answer = answers.get(stage_name) or ""
        if not reference:
            continue
        if stage_name == "before":
            # Before the change there is nothing stale: gold is the value itself.
            stages[stage_name] = {
                "criterion": "containment",
                "correct": matches(answer, reference),
            }
            continue
        verdict = score_abs_gold(answer, reference)
        if verdict is None:
            stages[stage_name] = {
                "criterion": "abs_three_part",
                "gold_parsed": False,
                "correct": False,
            }
            continue
        stages[stage_name] = {
            "criterion": "abs_three_part",
            "gold_parsed": True,
            "abstained": verdict.abstained,
            "cited_prev": verdict.cited_prev,
            "named_upstream": verdict.named_upstream,
            "asserts_current": verdict.asserts_current,
            "correct": verdict.all_three,
        }
    llm_by_stage = {
        str(row.get("stage")): row.get("parsed_verdict") == "correct"
        for row in judge_records
    }
    disagreements = sorted(
        stage_name
        for stage_name, row in stages.items()
        if stage_name in llm_by_stage and llm_by_stage[stage_name] != row["correct"]
    )
    return {
        "stages": stages,
        "llm_judge_by_stage": llm_by_stage,
        "disagreements": disagreements,
        # MEME's trivial-pass rule: the before answer must also be right, so a
        # model that abstains unconditionally cannot score.
        "trivial_pass": bool(
            stages.get("before", {}).get("correct")
            and stages.get("on", {}).get("correct")
        ),
    }


def _validate_call_evidence(
    call: Mapping[str, Any],
    *,
    index: int,
    artifact: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
    trace_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str, float]:
    call_id = str(call.get("call_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", call_id):
        raise RuntimeError(f"calls[{index}].call_id is missing or malformed")
    kind = str(call.get("kind") or "")
    contract = EXTERNAL_CALL_CONTRACT.get(kind)
    if contract is None:
        raise RuntimeError(f"calls[{index}] has unknown external call kind")
    stage = str(call.get("stage") or "")
    if stage not in contract["stages"] or stage not in ALLOWED_EXTERNAL_CALL_STAGES:
        raise RuntimeError(f"calls[{index}] has unknown or mismatched stage")
    bucket = str(call.get("usage_bucket") or "")
    if bucket != contract["bucket"]:
        raise RuntimeError(f"calls[{index}] stage-to-bucket mismatch")
    if int(_finite_nonnegative(call.get("calls"), field=f"calls[{index}].calls")) != 1:
        raise RuntimeError(f"calls[{index}] must describe exactly one external call")
    for token_field in ("prompt_tokens", "completion_tokens"):
        value = _finite_nonnegative(
            call.get(token_field), field=f"calls[{index}].{token_field}"
        )
        if not value.is_integer():
            raise RuntimeError(f"calls[{index}].{token_field} must be integral")
    if call.get("tokens_are_real") is not True or call.get("estimated") is not False:
        raise RuntimeError(f"calls[{index}] is not real external usage")
    for evidence in ("request", "response"):
        byte_count = _finite_nonnegative(
            call.get(f"{evidence}_bytes"),
            field=f"calls[{index}].{evidence}_bytes",
        )
        if not byte_count.is_integer():
            raise RuntimeError(f"calls[{index}] has empty {evidence} evidence")
        # The request side must always carry bytes: a prompt was definitely sent.
        # The response side may legitimately be zero. A model that is told to answer
        # only from the supplied notes, and whose notes do not contain the asked-for
        # fact, returns an empty completion — one stop token, real usage, no content.
        # That is a real measurement (a retrieval miss), not missing evidence, and
        # 2026-09-02 sw_027 hit it. Rejecting it killed the whole tier, which both
        # discarded a genuine result and hid the retrieval miss behind a crash.
        # This does not open a fabrication hole: the trace cross-check below still
        # recomputes response_sha256/response_bytes from the raw recorded response,
        # so a zero here has to be a real, recorded empty string.
        if evidence == "request" and byte_count <= 0:
            raise RuntimeError(f"calls[{index}] has empty {evidence} evidence")
        if not re.fullmatch(r"[0-9a-f]{64}", str(call.get(f"{evidence}_sha256") or "")):
            raise RuntimeError(f"calls[{index}] has malformed {evidence} hash")
    raw_present = call.get("raw_output_present")
    if not isinstance(raw_present, bool):
        raise RuntimeError(f"calls[{index}] lacks raw output evidence")
    # Having allowed the zero above, pin the two fields to each other: a row may
    # not claim it has raw output while reporting no bytes. The converse (no raw
    # output, bytes > 0) is legal — a whitespace-only completion is exactly that —
    # and the trace cross-check below recomputes both fields from the raw response,
    # so it catches any other disagreement.
    if raw_present and int(call["response_bytes"]) <= 0:
        raise RuntimeError(f"calls[{index}] claims raw output with no response bytes")
    model = str(call.get("model") or "")
    provider = str(call.get("provider") or "")
    if not model or not provider:
        raise RuntimeError(f"calls[{index}] lacks model/provider identity")
    price = call.get("price_snapshot")
    if not isinstance(price, Mapping):
        raise RuntimeError(f"calls[{index}] lacks price snapshot")
    if config is not None:
        expected_model = (config.get("models") or {}).get(contract["model_key"])
        expected_provider = (config.get("provider_bindings") or {}).get(
            contract["provider_key"]
        )
        if model != expected_model or provider != expected_provider:
            raise RuntimeError(f"calls[{index}] model/provider mismatch")
        expected_price = (config.get("price_table") or {}).get(model)
        if price != expected_price:
            raise RuntimeError(f"calls[{index}] price snapshot mismatch")
        if call.get("price_version") != sha256_bytes(
            canonical_json(config.get("price_table") or {}).encode()
        ):
            raise RuntimeError(f"calls[{index}] price version mismatch")
    expected_usd = (
        _finite_nonnegative(
            call["prompt_tokens"], field=f"calls[{index}].prompt_tokens"
        )
        * _finite_nonnegative(
            price.get("input_per_million"), field=f"calls[{index}].input_price"
        )
        + _finite_nonnegative(
            call["completion_tokens"], field=f"calls[{index}].completion_tokens"
        )
        * _finite_nonnegative(
            price.get("output_per_million"), field=f"calls[{index}].output_price"
        )
    ) / 1_000_000
    usd = _finite_nonnegative(call.get("usd"), field=f"calls[{index}].usd")
    _assert_close(usd, expected_usd, field=f"calls[{index}].usd")
    if artifact is not None:
        trace = trace_rows.get(call_id)
        if trace is None:
            raise RuntimeError(f"cost call_id has no raw trace: {call_id}")
        request = trace.get("prompt")
        response = trace.get("raw_answer", trace.get("raw_output"))
        if not isinstance(request, str) or not request.strip():
            raise RuntimeError(f"trace request is empty for {call_id}")
        if not isinstance(response, str):
            raise RuntimeError(f"trace response is empty for {call_id}")
        # An empty/blank recorded response is a real outcome, not missing evidence
        # (see the response_bytes note above). It still has to agree with what the
        # cost row claims, which the hash/byte comparison below enforces, and with
        # raw_output_present, which is pinned here.
        if bool(response.strip()) is not bool(call.get("raw_output_present")):
            raise RuntimeError(f"trace response/raw_output_present disagree for {call_id}")
        if response.casefold().startswith(("placeholder", "fixture", "synthetic-stub")):
            raise RuntimeError(f"trace response is a placeholder for {call_id}")
        expected_usage = {
            "calls": call["calls"],
            "prompt_tokens": call["prompt_tokens"],
            "completion_tokens": call["completion_tokens"],
            "tokens_are_real": True,
            "retry_attempts": int(call.get("retry_attempt") or 0),
            "retry_usage_unknown": bool(call.get("retry_usage_unknown")),
        }
        if (
            trace.get("call_id") != call_id
            or trace.get("model") != model
            or trace.get("provider") != provider
            or sha256_bytes(request.encode()) != call["request_sha256"]
            or len(request.encode()) != call["request_bytes"]
            or sha256_bytes(response.encode()) != call["response_sha256"]
            or len(response.encode()) != call["response_bytes"]
            or trace.get("usage") != expected_usage
        ):
            raise RuntimeError(f"raw trace/cost evidence mismatch for {call_id}")
        if kind == "judge":
            raw_verdict, raw_fallback = parse_paid_judge_output(response)
            if (
                trace.get("parsed_verdict") != raw_verdict
                or trace.get("fallback") is not raw_fallback
            ):
                raise RuntimeError(
                    f"judge raw output/parsed verdict mismatch for {call_id}"
                )
    return stage, bucket, usd


def validate_paid_cost(
    cost: Mapping[str, Any],
    *,
    artifact: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> float:
    complete = cost.get("cost_complete")
    if complete not in {True, False}:
        raise RuntimeError("paid cost_complete must be boolean")
    execution = cost.get("paid_execution")
    if not isinstance(execution, Mapping):
        raise RuntimeError("paid_execution cost is missing")
    execution_total = _validate_execution_cost(execution, field="paid_execution")
    failed_attempts = cost.get("failed_attempts")
    if not isinstance(failed_attempts, list):
        raise RuntimeError("failed_attempts retry ledger is missing")
    retry_total = sum(
        _validate_execution_cost(item, field=f"failed_attempts[{index}]")
        for index, item in enumerate(failed_attempts)
    )
    retry_spend = _finite_nonnegative(
        cost.get("retry_spend_usd"), field="retry_spend_usd"
    )
    _assert_close(retry_spend, retry_total, field="retry_spend_usd")
    frozen = cost.get("frozen_v3")
    if not isinstance(frozen, Mapping) or frozen.get("cost_complete") not in {
        True,
        False,
    }:
        raise RuntimeError("frozen v3 cost is missing or malformed")
    frozen_total = _finite_nonnegative(frozen.get("known_usd"), field="frozen_v3")
    frozen_interval = frozen.get("strict_usd_interval")
    if not isinstance(frozen_interval, list) or len(frozen_interval) != 2:
        raise RuntimeError("frozen v3 cost interval is malformed")
    _assert_close(
        _finite_nonnegative(frozen_interval[0], field="frozen_v3.interval[0]"),
        frozen_total,
        field="frozen_v3.interval[0]",
    )
    if frozen.get("cost_complete") is True:
        _assert_close(
            _finite_nonnegative(frozen_interval[1], field="frozen_v3.interval[1]"),
            frozen_total,
            field="frozen_v3.interval[1]",
        )
    elif frozen_interval[1] is not None:
        upper = _finite_nonnegative(
            frozen_interval[1], field="frozen_v3.interval[1]"
        )
        if upper < frozen_total:
            raise RuntimeError("frozen v3 bounded interval is below known spend")
    expected_complete = bool(
        frozen.get("cost_complete") is True
        and execution.get("cost_complete") is True
        and all(item.get("cost_complete") is True for item in failed_attempts)
    )
    if complete is not expected_complete:
        raise RuntimeError("paid cost completeness does not close over attempts")
    known_total = _finite_nonnegative(cost.get("known_usd"), field="known_usd")
    _assert_close(
        known_total,
        frozen_total + execution_total + retry_total,
        field="known_usd",
    )
    interval = cost.get("strict_usd_interval")
    if not isinstance(interval, list) or len(interval) != 2:
        raise RuntimeError("strict_usd_interval is malformed")
    _assert_close(
        _finite_nonnegative(interval[0], field="strict_usd_interval[0]"),
        known_total,
        field="strict_usd_interval[0]",
    )
    if complete:
        _assert_close(
            _finite_nonnegative(interval[1], field="strict_usd_interval[1]"),
            known_total,
            field="strict_usd_interval[1]",
        )
    elif interval[1] is not None:
        upper = _finite_nonnegative(interval[1], field="strict_usd_interval[1]")
        if upper < known_total:
            raise RuntimeError("strict_usd_interval upper is below known spend")
    calls = cost.get("calls")
    if not isinstance(calls, list):
        raise RuntimeError("paid call-level cost ledger is malformed")
    if not calls:
        if artifact is None or artifact.get("runtime_status") != "input-invalid":
            raise RuntimeError("paid call-level cost ledger is empty")
        if execution_total != 0.0 or retry_total != 0.0:
            raise RuntimeError("input-invalid case has nonzero runtime spend")
        if cost.get("stage_summary") != {}:
            raise RuntimeError("input-invalid case has external stage summary")
        if cost.get("call_ledger_sha256") != sha256_bytes(
            canonical_json([]).encode()
        ):
            raise RuntimeError("input-invalid empty call ledger hash mismatch")
        summary = cost.get("episode_summary")
        if (
            not isinstance(summary, Mapping)
            or summary.get("cost_complete") is not True
            or summary.get("call_count") != 0
            or summary.get("call_ids") != []
            or float(summary.get("total_usd") or 0.0) != 0.0
        ):
            raise RuntimeError("input-invalid episode cost summary is malformed")
        return known_total
    call_total = 0.0
    external_layers: set[str] = set()
    call_ids: set[str] = set()
    trace_rows = _call_trace_rows(artifact) if artifact is not None else {}
    by_stage: dict[str, dict[str, Any]] = {}
    by_bucket: dict[str, dict[str, Any]] = {
        bucket: {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "known_usd": 0.0,
            "call_ids": [],
        }
        for bucket in EXPECTED_PAID_USAGE_BUCKETS
    }
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            raise RuntimeError(f"calls[{index}] is malformed")
        call_id = str(call.get("call_id") or "")
        if call_id in call_ids:
            raise RuntimeError(f"duplicate cost call_id: {call_id}")
        call_ids.add(call_id)
        stage, bucket, usd = _validate_call_evidence(
            call,
            index=index,
            artifact=artifact,
            config=config,
            trace_rows=trace_rows,
        )
        external_layers.add(str(call["kind"]))
        stage_row = by_stage.setdefault(
            stage,
            {
                "usage_bucket": bucket,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "known_usd": 0.0,
                "call_ids": [],
            },
        )
        if stage_row["usage_bucket"] != bucket:
            raise RuntimeError(f"stage {stage} maps to multiple usage buckets")
        for field in ("calls", "prompt_tokens", "completion_tokens"):
            stage_row[field] += int(call[field])
            by_bucket[bucket][field] += int(call[field])
        stage_row["known_usd"] += usd
        stage_row["call_ids"].append(call_id)
        by_bucket[bucket]["known_usd"] += usd
        by_bucket[bucket]["call_ids"].append(call_id)
        call_total += usd
    if not {"answer", "judge"}.issubset(external_layers):
        raise RuntimeError("paid artifact lacks real answer and judge usage")
    if artifact is not None and set(trace_rows) != call_ids:
        raise RuntimeError("raw trace contains a call_id absent from cost ledger")
    stage_summary = cost.get("stage_summary")
    if stage_summary != by_stage:
        raise RuntimeError("call-to-stage cost summary does not close")
    for bucket, calculated in by_bucket.items():
        row = execution["layers"][bucket]
        for field in ("calls", "prompt_tokens", "completion_tokens"):
            if int(row[field]) != calculated[field]:
                raise RuntimeError(f"call-to-bucket {bucket}.{field} does not close")
        _assert_close(
            float(row["known_usd"]),
            calculated["known_usd"],
            field=f"call-to-bucket {bucket}.known_usd",
        )
        if config is not None:
            kind = next(
                name
                for name, contract in EXTERNAL_CALL_CONTRACT.items()
                if contract["bucket"] == bucket
            )
            expected_model = (config.get("models") or {}).get(
                EXTERNAL_CALL_CONTRACT[kind]["model_key"]
            )
            if row.get("model") != expected_model:
                raise RuntimeError(f"bucket {bucket} model mismatch")
    expected_ledger_hash = sha256_bytes(canonical_json(calls).encode())
    if cost.get("call_ledger_sha256") != expected_ledger_hash:
        raise RuntimeError("paid call ledger hash mismatch")
    summary = cost.get("episode_summary")
    if (
        not isinstance(summary, Mapping)
        or summary.get("cost_complete") is not execution.get("cost_complete")
    ):
        raise RuntimeError("episode cost summary completeness disagrees")
    if int(_finite_nonnegative(summary.get("call_count"), field="call_count")) != len(
        calls
    ):
        raise RuntimeError("episode call_count does not close")
    if summary.get("call_ids") != [str(call["call_id"]) for call in calls]:
        raise RuntimeError("episode call_id order does not close")
    _assert_close(
        _finite_nonnegative(summary.get("total_usd"), field="episode total_usd"),
        call_total,
        field="episode total_usd",
    )
    _assert_close(call_total, execution_total, field="call ledger vs paid_execution")
    return known_total


def _require_namespace_path(path: Path, root: Path, *, label: str) -> Path:
    unresolved = path.absolute()
    cursor = unresolved
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise RuntimeError(f"{label} uses a symlink")
        if cursor == root.absolute():
            break
        cursor = cursor.parent
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"{label} escapes its namespace") from exc
    return resolved


def validate_paid_smoke_evidence(
    out: Path,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    paid = _load_success_gate(
        out / "paid_smoke_result.json", kind="paid smoke", identity=identity
    )
    index_path = out / "paid_smoke_cases.jsonl"
    if not index_path.is_file():
        raise RuntimeError("paid smoke append-only index is missing")
    try:
        index_rows = [
            json.loads(line)
            for line in index_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("paid smoke append-only index is malformed") from exc
    if len(index_rows) != 1:
        raise RuntimeError("paid smoke gate requires exactly one indexed case")
    index_row = index_rows[0]
    episode_id = str(index_row.get("episode_id") or "")
    if not episode_id:
        raise RuntimeError("paid smoke index lacks episode identity")
    checkpoint_dir = out / "checkpoints" / "paid-smoke"
    checkpoint_paths = sorted(checkpoint_dir.glob("*.json"))
    expected_stem = f"{episode_id}-{identity_task_type(identity)}"
    if len(checkpoint_paths) != 1 or checkpoint_paths[0].stem != expected_stem:
        raise RuntimeError("paid smoke checkpoint namespace is not exactly closed")
    try:
        checkpoint = json.loads(checkpoint_paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("paid smoke checkpoint is malformed") from exc
    if (
        checkpoint.get("status") != "finalized"
        or checkpoint.get("checkpoint_namespace") != "paid-smoke"
        or checkpoint.get("run_config_hash") != identity["run_config_hash"]
    ):
        raise RuntimeError("paid smoke checkpoint identity/status mismatch")
    artifact_path = _require_namespace_path(
        Path(str(checkpoint.get("artifact_path") or "")),
        out / "artifacts" / "paid-smoke",
        label="paid smoke artifact",
    )
    if str(artifact_path) != str(Path(str(index_row.get("artifact_path"))).resolve()):
        raise RuntimeError("paid smoke checkpoint/index artifact path mismatch")
    if not artifact_path.is_file():
        raise RuntimeError("paid smoke artifact is missing")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("paid smoke artifact is malformed") from exc
    validate_case_artifact(artifact)
    if (
        artifact.get("episode_id") != episode_id
        or artifact.get("artifact_mode") != "paid-smoke"
        or artifact.get("synthetic_stub") is not False
        or artifact.get("case_success") is not True
        or (artifact.get("authorization") or {}).get("run_config_hash")
        != identity["run_config_hash"]
        or (artifact.get("authorization") or {}).get("run_config") != identity["config"]
    ):
        raise RuntimeError("paid smoke artifact mode/schema/config mismatch")
    manifest_path = artifact_path.with_name("manifest.json")
    if not manifest_path.is_file():
        raise RuntimeError("paid smoke manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("paid smoke manifest is malformed") from exc
    artifact_digest = sha256_file(artifact_path)
    manifest_digest = sha256_file(manifest_path)
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("schema_generation") != SCHEMA_GENERATION
        or manifest.get("artifact_mode") != "paid-smoke"
        or manifest.get("episode_id") != episode_id
        or manifest.get("run_config_hash") != identity["run_config_hash"]
        or manifest.get("artifact_sha256") != artifact_digest
        or manifest.get("artifact_bytes") != artifact_path.stat().st_size
        or checkpoint.get("artifact_sha256") != artifact_digest
        or index_row.get("artifact_sha256") != artifact_digest
        or index_row.get("manifest_sha256") != manifest_digest
        or index_row.get("run_config_hash") != identity["run_config_hash"]
        or index_row.get("call_ledger_sha256")
        != artifact["cost"].get("call_ledger_sha256")
        or index_row.get("attempt_token") != checkpoint.get("attempt_token")
    ):
        raise RuntimeError("paid smoke checkpoint/index/manifest hash closure failed")
    artifacts = {
        path.resolve()
        for path in (out / "artifacts" / "paid-smoke").glob("*/artifact.json")
    }
    manifests = {
        path.resolve()
        for path in (out / "artifacts" / "paid-smoke").glob("*/manifest.json")
    }
    if artifacts != {artifact_path} or manifests != {manifest_path.resolve()}:
        raise RuntimeError("paid smoke namespace contains orphan or extra artifacts")
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise RuntimeError("paid smoke checkpoint lacks attempt/retry ledger")
    if attempts[-1].get("status") != "execution_complete" or not isinstance(
        attempts[-1].get("usage"), Mapping
    ):
        raise RuntimeError("paid smoke successful attempt usage is missing")
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt.get("cost"), Mapping):
            raise RuntimeError(f"paid smoke attempt {index} lacks frozen cost")
        _validate_execution_cost(
            attempt["cost"], field=f"checkpoint.attempts[{index}].cost"
        )
    successful_attempt = attempts[-1]
    if (
        successful_attempt.get("attempt_token") != checkpoint.get("attempt_token")
        or successful_attempt.get("call_ledger_sha256")
        != artifact["cost"].get("call_ledger_sha256")
        or successful_attempt.get("usage")
        != artifact["cost"]["paid_execution"]["layers"]
        or successful_attempt.get("cost") != artifact["cost"]["paid_execution"]
    ):
        raise RuntimeError("paid smoke checkpoint/artifact attempt mismatch")
    failed_attempts = [
        attempt for attempt in attempts[:-1] if attempt.get("status") == "failed"
    ]
    if [attempt.get("cost") for attempt in failed_attempts] != artifact["cost"].get(
        "failed_attempts"
    ):
        raise RuntimeError("paid smoke retry ledger does not match artifact cost")
    validate_paid_cost(
        artifact["cost"],
        artifact=artifact,
        config=identity["config"],
    )
    derived = {
        "trace_complete": all(artifact.get(section) for section in TRACE_SECTIONS),
        "cost_acceptable": True,
        "cleanup_complete": artifact.get("cleanup", {}).get("complete") is True,
        "production_retrieval_change_verified": (
            (artifact.get("production_retrieval_change_verified") or {}).get(
                "integrity_complete"
            )
            is True
        ),
        "no_leftover": not artifact["p2_queue"].get("unfinished"),
        "no_dead_letter": not any(
            row.get("delivery_status") == "dead_letter"
            for row in artifact["p2_queue"].get("events") or []
        ),
    }
    if (
        paid.get("synthetic_stub") is not False
        or paid.get("schema_version") != SCHEMA_VERSION
        or paid.get("schema_generation") != SCHEMA_GENERATION
        or paid.get("artifact_mode") != "paid-smoke"
        or paid.get("checkpoint_namespace") != "paid-smoke"
        or paid.get("case_count") != 1
        or paid.get("cases") != index_rows
        or paid.get("artifact_sha256") != artifact_digest
        or paid.get("call_ledger_sha256") != artifact["cost"].get("call_ledger_sha256")
        or paid.get("attempt_token") != checkpoint.get("attempt_token")
        or checkpoint.get("index_sha256") != sha256_file(index_path)
        or checkpoint.get("summary_sha256")
        != sha256_file(out / "paid_smoke_result.json")
        or any(paid.get(key) is not value for key, value in derived.items())
    ):
        raise RuntimeError("paid smoke summary disagrees with primary evidence")
    verify_run_identity(identity)
    return {
        "episode_id": episode_id,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_digest,
        "known_usd": artifact["cost"]["known_usd"],
    }


_CANONICAL_FULL100_EPISODES: tuple[str, ...] | None = None


def canonical_full100_episode_ids() -> tuple[str, ...]:
    global _CANONICAL_FULL100_EPISODES
    if _CANONICAL_FULL100_EPISODES is None:
        _CANONICAL_FULL100_EPISODES = tuple(FrozenV3().episode_ids)
    return _CANONICAL_FULL100_EPISODES


def validate_full_run_completion(
    out: Path,
    *,
    expected_episode_ids: Sequence[str],
    run_config_hash: str,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> list[dict[str, Any]]:
    expected = list(expected_episode_ids)
    canonical_expected = evaluation_episode_ids(
        FrozenV3(), evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    if (
        len(expected) != len(canonical_expected)
        or len(set(expected)) != len(canonical_expected)
        or set(expected) != set(canonical_expected)
    ):
        # Name the actual expected set. The old label hardcoded "canonical 100",
        # which for an Abs run (90 after exclusion) reported a count the tier never
        # had and pointed the reader at the dataset instead of at the real cause:
        # this function's task_type default silently falling back to "Cas".
        raise RuntimeError(
            f"full-run expected episode set is not canonical {task_type} hop"
            f"{evaluation_hop} ({len(canonical_expected)}): got {len(expected)}"
        )
    if len(expected) != len(set(expected)):
        raise RuntimeError("expected full-run episode list contains duplicates")
    checkpoint_dir = out / "checkpoints" / "full-run"
    checkpoint_paths = sorted(checkpoint_dir.glob("*.json"))
    # Checkpoint stems carry the task type, so compare against the expected set
    # named the same way. A stem without one is a pre-task_type Cas checkpoint.
    expected_names = {f"{episode_id}-{task_type}" for episode_id in expected}
    observed_names = [path.stem for path in checkpoint_paths]
    missing = sorted(expected_names - set(observed_names))
    extra = sorted(set(observed_names) - expected_names)
    if missing or extra or len(checkpoint_paths) != len(expected):
        raise RuntimeError(
            f"full-run checkpoint set is not closed: missing={missing}, extra={extra}"
        )

    rows: list[dict[str, Any]] = []
    artifact_paths: set[Path] = set()
    artifact_hashes: set[str] = set()
    artifact_root = out / "artifacts" / "full-run"
    for episode_id in expected:
        try:
            checkpoint = json.loads(
                (checkpoint_dir / f"{episode_id}-{task_type}.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"full-run checkpoint is not parseable: {episode_id}"
            ) from exc
        if (
            checkpoint.get("status") != "success"
            or checkpoint.get("checkpoint_namespace") != "full-run"
            or checkpoint.get("run_config_hash") != run_config_hash
        ):
            raise RuntimeError(f"full-run checkpoint is not successful: {episode_id}")
        artifact_value = checkpoint.get("artifact_path")
        if not artifact_value:
            raise RuntimeError(f"full-run checkpoint lacks artifact_path: {episode_id}")
        artifact_path = _require_namespace_path(
            Path(str(artifact_value)),
            artifact_root,
            label=f"full-run artifact {episode_id}",
        )
        if artifact_path in artifact_paths:
            raise RuntimeError(f"duplicate full-run artifact path: {artifact_path}")
        artifact_paths.add(artifact_path)
        if not artifact_path.is_file():
            raise RuntimeError(f"full-run artifact missing: {artifact_path}")
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"full-run artifact is not parseable: {episode_id}"
            ) from exc
        if (
            artifact.get("episode_id") != episode_id
            or artifact.get("evaluation_hop", 1) != evaluation_hop
            or artifact.get("synthetic_stub") is not False
            or artifact.get("artifact_mode") != "full-run"
            or (artifact.get("authorization") or {}).get("run_config_hash")
            != run_config_hash
        ):
            raise RuntimeError(
                f"full-run artifact identity/synthetic mismatch: {episode_id}"
            )
        validate_case_artifact(artifact)
        attempts = checkpoint.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise RuntimeError(f"full-run checkpoint lacks attempts: {episode_id}")
        successful_attempt = attempts[-1]
        if (
            successful_attempt.get("status") != "success"
            or successful_attempt.get("attempt_token")
            != checkpoint.get("attempt_token")
            or successful_attempt.get("call_ledger_sha256")
            != artifact["cost"].get("call_ledger_sha256")
            or successful_attempt.get("usage")
            != artifact["cost"]["paid_execution"]["layers"]
            or successful_attempt.get("cost") != artifact["cost"]["paid_execution"]
        ):
            raise RuntimeError(
                f"full-run checkpoint/artifact attempt mismatch: {episode_id}"
            )
        failed_attempts = [
            attempt for attempt in attempts[:-1] if attempt.get("status") == "failed"
        ]
        if [attempt.get("cost") for attempt in failed_attempts] != artifact["cost"].get(
            "failed_attempts"
        ):
            raise RuntimeError(f"full-run retry ledger mismatch: {episode_id}")
        manifest_path = artifact_path.with_name("manifest.json")
        if not manifest_path.is_file():
            raise RuntimeError(f"full-run manifest missing: {episode_id}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"full-run manifest is not parseable: {episode_id}"
            ) from exc
        digest = sha256_file(artifact_path)
        if (
            manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("schema_generation") != SCHEMA_GENERATION
            or manifest.get("artifact_mode") != "full-run"
            or manifest.get("episode_id") != episode_id
            or manifest.get("run_config_hash") != run_config_hash
            or manifest.get("artifact_sha256") != digest
            or checkpoint.get("artifact_sha256") != digest
            or manifest.get("artifact_bytes") != artifact_path.stat().st_size
        ):
            raise RuntimeError(f"full-run artifact hash closure failed: {episode_id}")
        if digest in artifact_hashes:
            raise RuntimeError(f"duplicate full-run artifact hash: {digest}")
        artifact_hashes.add(digest)
        known_usd = validate_paid_cost(
            artifact["cost"],
            artifact=artifact,
            config=artifact["authorization"]["run_config"],
        )
        rows.append(
            {
                "episode_id": episode_id,
                "artifact_path": str(artifact_path),
                "artifact_sha256": digest,
                "known_usd": known_usd,
            }
        )
    observed_artifacts = {
        path.resolve() for path in artifact_root.glob("*/artifact.json")
    }
    observed_manifests = {
        path.resolve() for path in artifact_root.glob("*/manifest.json")
    }
    expected_manifests = {path.with_name("manifest.json") for path in artifact_paths}
    if observed_artifacts != artifact_paths or observed_manifests != expected_manifests:
        raise RuntimeError("full-run artifact namespace contains orphan or extra files")
    unexpected_files = [
        path
        for path in artifact_root.rglob("*")
        if path.is_file()
        and path.resolve() not in artifact_paths
        and path.resolve() not in expected_manifests
    ]
    if unexpected_files:
        raise RuntimeError("full-run artifact namespace contains unexpected files")
    return rows


class FrozenV3:
    def __init__(self, shared: Path = DEFAULT_SHARED, v3: Path = DEFAULT_V3):
        self.shared = shared
        self.v3 = v3
        self._snapshot_bytes: dict[Path, bytes] = {}
        self._snapshot_hashes: dict[Path, str] = {}
        self._dataset_snapshots: dict[Path, bytes] = {}

        def snapshot(path: Path) -> bytes:
            resolved = path.resolve()
            data = resolved.read_bytes()
            self._snapshot_bytes[resolved] = data
            self._snapshot_hashes[resolved] = sha256_bytes(data)
            return data

        self.index = json.loads(snapshot(shared / "shared_manifest_index.json"))
        self._episodes: dict[str, dict[str, Any]] = {}
        for index_row in self.index["episodes"]:
            episode_id = str(index_row["episode_id"])
            data = snapshot(shared / index_row["manifest_path"])
            if sha256_bytes(data) != index_row["manifest_file_sha256"]:
                raise ValueError(f"shared manifest hash mismatch for {episode_id}")
            self._episodes[episode_id] = json.loads(data)
        self._selected: dict[str, list[dict[str, Any]]] = {}
        for line in snapshot(v3 / "case_success.jsonl").decode().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            result = row.get("result") or row
            self._selected.setdefault(str(result["episode_id"]), []).append(result)
        self._cost = {
            str(row["episode_id"]): row
            for row in (
                json.loads(line)
                for line in snapshot(v3 / "per_episode_cost.jsonl")
                .decode()
                .splitlines()
                if line.strip()
            )
        }
        snapshot(v3 / "output_hashes.json")

    @property
    def episode_ids(self) -> list[str]:
        return [str(row["episode_id"]) for row in self.index["episodes"]]

    def episode(self, episode_id: str) -> dict[str, Any]:
        return json.loads(canonical_json(self._episodes[episode_id]))

    def selections(self, episode_id: str) -> list[dict[str, Any]]:
        return list(self._selected.get(episode_id, ()))

    def selected_edges(self, episode_id: str) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for result in self.selections(episode_id):
            targets = [str(item) for item in result.get("target_node_ids") or []]
            for selected in result.get("selected_sources") or []:
                for source in selected.get("source_node_ids") or []:
                    for target in targets:
                        key = (str(source), target)
                        if key in seen:
                            continue
                        seen.add(key)
                        edges.append(
                            {
                                "dependency_node_id": key[0],
                                "dependent_node_id": key[1],
                                "source_evidence_id": selected.get(
                                    "source_evidence_id"
                                ),
                                "target_evidence_id": result.get("target_evidence_id"),
                                "source_origin": selected.get("source_origin"),
                            }
                        )
        return edges

    def episode_cost(self, episode_id: str) -> dict[str, Any]:
        return dict(self._cost[episode_id])

    def input_hashes(self, data: str | Path = DEFAULT_DATA) -> dict[str, str]:
        dataset_path = Path(data).resolve()
        if dataset_path not in self._dataset_snapshots:
            dataset_bytes = dataset_path.read_bytes()
            parsed = json.loads(dataset_bytes)
            if not isinstance(parsed, list):
                raise ValueError("MEME dataset snapshot must be a JSON list")
            self._dataset_snapshots[dataset_path] = dataset_bytes
        return {
            "shared_index_sha256": self._snapshot_hashes[
                (self.shared / "shared_manifest_index.json").resolve()
            ],
            "shared_episode_set_sha256": sha256_bytes(
                canonical_json(
                    {
                        row["episode_id"]: row["manifest_file_sha256"]
                        for row in self.index["episodes"]
                    }
                ).encode()
            ),
            "v3_case_success_sha256": self._snapshot_hashes[
                (self.v3 / "case_success.jsonl").resolve()
            ],
            "v3_output_hashes_sha256": self._snapshot_hashes[
                (self.v3 / "output_hashes.json").resolve()
            ],
            "v3_per_episode_cost_sha256": self._snapshot_hashes[
                (self.v3 / "per_episode_cost.jsonl").resolve()
            ],
            "dataset_sha256": sha256_bytes(self._dataset_snapshots[dataset_path]),
            "runner_source_sha256": sha256_file(Path(__file__)),
        }

    def input_bundle_entries(
        self, data: str | Path = DEFAULT_DATA
    ) -> list[dict[str, Any]]:
        self.input_hashes(data)
        project_root = Path(__file__).resolve().parents[2].resolve()
        roles: dict[Path, str] = {
            (self.shared / "shared_manifest_index.json").resolve(): "shared_index",
            (self.v3 / "case_success.jsonl").resolve(): "v3_selection",
            (self.v3 / "per_episode_cost.jsonl").resolve(): "v3_cost",
            (self.v3 / "output_hashes.json").resolve(): "v3_output_hashes",
            Path(data).resolve(): "dataset",
        }
        for row in self.index["episodes"]:
            roles[(self.shared / row["manifest_path"]).resolve()] = (
                "shared_episode_manifest"
            )
        entries = []
        for path, role in sorted(roles.items(), key=lambda item: str(item[0])):
            snapshot = (
                self._dataset_snapshots[path]
                if role == "dataset"
                else self._snapshot_bytes[path]
            )
            try:
                display_path = str(path.relative_to(project_root))
            except ValueError:
                display_path = str(path)
            entries.append(
                {
                    "path": display_path,
                    "resolved_path": str(path),
                    "role": role,
                    "sha256": sha256_bytes(snapshot),
                    "byte_count": len(snapshot),
                }
            )
        return entries

    def dataset_episodes(self, data: str | Path = DEFAULT_DATA) -> list[dict[str, Any]]:
        self.input_hashes(data)
        return json.loads(self._dataset_snapshots[Path(data).resolve()])

    def verify_identity(self, data: str | Path = DEFAULT_DATA) -> None:
        for path, expected in self._snapshot_hashes.items():
            if sha256_bytes(path.read_bytes()) != expected:
                raise RuntimeError(f"frozen input changed after snapshot: {path}")
        dataset_path = Path(data).resolve()
        self.input_hashes(dataset_path)
        expected = sha256_bytes(self._dataset_snapshots[dataset_path])
        if sha256_bytes(dataset_path.read_bytes()) != expected:
            raise RuntimeError(f"dataset changed after snapshot: {dataset_path}")


@dataclass(frozen=True)
class RootAlias:
    node_id: str
    context_id: str
    text: str
    evidence_ids: tuple[str, ...]
    source_origins: tuple[str, ...]
    alignment_status: str
    alignment_reason: str | None
    original_session_id: str | None
    alias_score: int


@dataclass(frozen=True)
class RootBindingCandidate:
    node_id: str
    text: str
    disposition: str
    reason: str
    original_session_id: str | None
    alignment_status: str


@dataclass(frozen=True)
class RootIdentity:
    episode_id: str
    entity: str
    normalized_before: str
    normalized_after: str
    group_id: str
    change_id: str
    aliases: tuple[RootAlias, ...]
    applicability: str
    ambiguity_status: str
    ambiguity_reasons: tuple[str, ...]
    direct_candidates: tuple[RootBindingCandidate, ...] = ()
    ambiguous_candidates: tuple[RootBindingCandidate, ...] = ()
    rejected_reason_clause_candidates: tuple[RootBindingCandidate, ...] = ()

    @property
    def alias_node_ids(self) -> tuple[str, ...]:
        return tuple(alias.node_id for alias in self.aliases)

    @property
    def alias_context_ids(self) -> tuple[str, ...]:
        return tuple(alias.context_id for alias in self.aliases)


@dataclass(frozen=True)
class RootWorkload:
    episode_id: str
    entity: str
    before: str
    after: str
    root_identity: RootIdentity
    change_evidence_text: str


@dataclass(frozen=True)
class RuntimeEpisodeInput:
    episode_id: str
    entity: str
    before: str
    after: str
    before_question: str
    after_question: str
    change_evidence_text: str


@dataclass(frozen=True)
class EvaluationAnnotations:
    episode_id: str
    before_reference: str
    after_reference: str
    target_entity: str
    annotated_hop: int | None


@dataclass(frozen=True)
class RetrievalEvidenceContract:
    """Post-runtime evaluation mapping; never consumed by runtime decisions."""

    before_question: str
    after_question: str
    root_group_id: str
    old_identity_id: str
    old_target_node_ids: tuple[str, ...]
    replacement_identity_id: str
    replacement_node_ids: tuple[str, ...]
    old_reference: str
    replacement_reference: str
    p1_path_status: str = "not-evaluated"
    old_mapping_status: str = "not-evaluated"
    replacement_mapping_status: str = "not-evaluated"
    surface_candidate_node_ids: tuple[str, ...] = ()

    @property
    def old_target_node_id(self) -> str:
        return self.old_target_node_ids[0]

    @property
    def replacement_node_id(self) -> str:
        return self.replacement_node_ids[0]


def _text_contains_reference(text: str, reference: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", reference.casefold())
    normalized = set(re.findall(r"[a-z0-9]+", text.casefold()))
    return bool(tokens) and all(token in normalized for token in tokens)


_ROOT_ALIAS_STOPWORDS = frozenset(
    {
        "a",
        "am",
        "an",
        "at",
        "currently",
        "has",
        "have",
        "i",
        "in",
        "is",
        "live",
        "lives",
        "my",
        "the",
        "their",
        "user",
        "work",
        "works",
    }
)


def _normalize_identity_value(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _root_alias_score(text: str, entity: str, before: str) -> int | None:
    """Score direct root assertions without consulting target-side information."""
    normalized_before = _normalize_identity_value(before)
    if not normalized_before:
        return None
    clauses = [
        _normalize_identity_value(clause)
        for clause in re.split(r"[.;!?\n]+", text)
        if normalized_before in _normalize_identity_value(clause)
    ]
    if not clauses:
        return None
    before_tokens = set(normalized_before.split())
    entity_tokens = set(_normalize_identity_value(entity.replace("_", " ")).split())
    scores = []
    for clause in clauses:
        tokens = clause.split()
        extra = [
            token
            for token in tokens
            if token not in before_tokens
            and token not in entity_tokens
            and token not in _ROOT_ALIAS_STOPWORDS
        ]
        scores.append(len(extra))
    return min(scores)


_DIRECT_ROOT_PATTERNS: dict[str, tuple[str, ...]] = {
    "health_condition": (
        r"\bhealth condition\b",
        r"\bdiagnosed with\b",
    ),
    "relationship_status": (
        r"\brelationship status\b",
        r"\b(?:i am|i'm|the user is)\b",
    ),
    "deploy_target": (r"\bdeploys?\s+to\b", r"\bdeployment (?:target|platform)\b"),
    "auth_provider": (
        r"\bauth(?:entication)? provider\b",
        r"\buses?\b.+\bfor authentication\b",
    ),
    "residence_location": (
        r"\b(?:i|the user)\s+(?:live|lives|reside|resides)\b",
        r"\bresidence\b",
    ),
    "employer": (
        r"\b(?:i|you|the user)\s+(?:work|works)\s+(?:at|for)\b",
        r"\bemployer is\b",
    ),
    "team_lead": (
        r"\bteam(?:'s)? lead is\b",
        r"\bleads? the team\b",
    ),
    "database": (
        r"\bdatabase is\b",
        r"\busing\b.+\bas (?:our|their|the) database\b",
    ),
    "framework": (
        r"\bframework is\b",
        r"\busing\b.+\bas (?:our|their|the) framework\b",
    ),
    "school": (
        r"\b(?:study|studying|studies|attend|attends)\b",
        r"\b(?:my|the user's) school is\b",
    ),
}


def _classify_root_candidate(text: str, entity: str, before: str) -> tuple[str, str]:
    """Classify a proposition by its main assertion, never by graph/gold data."""
    normalized_text = _normalize_identity_value(text)
    normalized_before = _normalize_identity_value(before)
    if not normalized_before or normalized_before not in normalized_text:
        return "unbound", "before-value-absent"
    lowered = text.casefold()
    if lowered.lstrip().startswith(("if ", "when ")):
        return "rejected-reason-clause", "conditional-main-clause"
    before_start = lowered.find(before.casefold())
    dependency_markers = (
        "—",
        "this depends",
        "depends on",
        "determined by",
        "through my employer",
        "through our employer",
        "assigned it",
        "if the team lead changes",
        "if my employer changes",
        "if my health condition changes",
        "if we switch",
        "if we change",
    )
    marker_positions = [lowered.find(marker) for marker in dependency_markers]
    marker_positions = [position for position in marker_positions if position >= 0]
    if marker_positions and before_start >= min(marker_positions):
        return "rejected-reason-clause", "before-value-only-in-dependency-clause"
    patterns = _DIRECT_ROOT_PATTERNS.get(entity, ())
    primary_clause = re.split(r"\s+[—;]\s+|;\s*this\b", lowered, maxsplit=1)[0]
    if entity == "health_condition":
        escaped_before = re.escape(before.casefold())
        if re.search(
            rf"\b(?:i have|the user has|the user's health condition is)\s+"
            rf"{escaped_before}\b",
            primary_clause,
        ):
            return "direct", "entity-specific-direct-assertion"
    if any(re.search(pattern, primary_clause) for pattern in patterns):
        return "direct", "entity-specific-direct-assertion"
    if not patterns and re.search(
        r"\b(?:i am|i'm|the user is)\b", primary_clause
    ):
        return "direct", "generic-copular-direct-assertion"
    return "ambiguous", "value-in-main-clause-without-direct-entity-assertion"


def _root_identity_payload(identity: RootIdentity) -> dict[str, Any]:
    return {
        "episode_id": identity.episode_id,
        "entity": identity.entity,
        "normalized_before": identity.normalized_before,
        "normalized_after": identity.normalized_after,
        "group_id": identity.group_id,
        "change_id": identity.change_id,
        "alias_node_ids": list(identity.alias_node_ids),
        "alias_context_ids": list(identity.alias_context_ids),
        "applicability": identity.applicability,
        "ambiguity_status": identity.ambiguity_status,
        "ambiguity_reasons": list(identity.ambiguity_reasons),
        "direct_candidates": [candidate.__dict__ for candidate in identity.direct_candidates],
        "ambiguous_candidates": [
            candidate.__dict__ for candidate in identity.ambiguous_candidates
        ],
        "rejected_reason_clause_candidates": [
            candidate.__dict__
            for candidate in identity.rejected_reason_clause_candidates
        ],
        "aliases": [
            {
                "node_id": alias.node_id,
                "context_id": alias.context_id,
                "text": alias.text,
                "evidence_ids": list(alias.evidence_ids),
                "source_origins": list(alias.source_origins),
                "alignment_status": alias.alignment_status,
                "alignment_reason": alias.alignment_reason,
                "original_session_id": alias.original_session_id,
                "alias_score": alias.alias_score,
            }
            for alias in identity.aliases
        ],
    }


def resolve_root_identity(
    corpus: FrozenV3,
    episode_id: str,
    *,
    data: str | Path = DEFAULT_DATA,
) -> RootIdentity:
    dataset_matches = [
        episode
        for episode in corpus.dataset_episodes(data)
        if str(episode.get("episode_id")) == episode_id
    ]
    if len(dataset_matches) != 1:
        raise ValueError(f"unique MEME root episode missing for {episode_id}")
    dataset_episode = dataset_matches[0]
    entity = str(dataset_episode.get("root") or "").strip()
    root_change = dataset_episode.get("root_change") or {}
    before = str(root_change.get("before") or "").strip()
    after = str(root_change.get("after") or "").strip()
    if not entity or not before or not after:
        raise ValueError(f"incomplete root identity for {episode_id}")

    episode = corpus.episode(episode_id)
    alignments = {str(row["node_id"]): row for row in episode.get("alignments") or ()}
    direct_rows: list[tuple[int, Mapping[str, Any]]] = []
    direct_candidates: list[RootBindingCandidate] = []
    ambiguous_candidates: list[RootBindingCandidate] = []
    rejected_candidates: list[RootBindingCandidate] = []
    for node in episode.get("nodes") or ():
        node_id = str(node["node_id"])
        text = str(node.get("text") or "")
        score = _root_alias_score(text, entity, before)
        if score is None:
            continue
        alignment = alignments.get(node_id) or {}
        disposition, reason = _classify_root_candidate(text, entity, before)
        candidate = RootBindingCandidate(
            node_id=node_id,
            text=text,
            disposition=disposition,
            reason=reason,
            original_session_id=(
                str(node["original_session_id"])
                if node.get("original_session_id")
                else None
            ),
            alignment_status=str(alignment.get("status") or "unrecorded"),
        )
        if disposition == "direct":
            direct_rows.append((score, node))
            direct_candidates.append(candidate)
        elif disposition == "rejected-reason-clause":
            rejected_candidates.append(candidate)
        else:
            ambiguous_candidates.append(candidate)

    alias_rows = direct_rows
    aliases: list[RootAlias] = []
    ambiguity_reasons: list[str] = []
    for score, node in sorted(alias_rows, key=lambda row: str(row[1]["node_id"])):
        node_id = str(node["node_id"])
        alignment = alignments.get(node_id) or {}
        evidence_ids: set[str] = set()
        if alignment.get("evidence_id"):
            evidence_ids.add(str(alignment["evidence_id"]))
        evidence_ids.update(
            f"turn:{turn['turn_sha256']}"
            for turn in node.get("original_turns") or ()
            if turn.get("turn_sha256")
        )
        source_origins: set[str] = set()
        if node.get("source_origin"):
            source_origins.add(str(node["source_origin"]))
        if node.get("original_session_id"):
            source_origins.add(str(node["original_session_id"]))
        if not evidence_ids:
            ambiguity_reasons.append(f"alias_without_evidence:{node_id}")
        aliases.append(
            RootAlias(
                node_id=node_id,
                context_id=str(node_uuid(episode_id, node_id)),
                text=str(node.get("text") or ""),
                evidence_ids=tuple(sorted(evidence_ids)),
                source_origins=tuple(sorted(source_origins)),
                alignment_status=str(alignment.get("status") or "unrecorded"),
                alignment_reason=(
                    str(alignment["reason"]) if alignment.get("reason") else None
                ),
                original_session_id=(
                    str(alignment["original_session_id"])
                    if alignment.get("original_session_id")
                    else None
                ),
                alias_score=score,
            )
        )

    normalized_before = _normalize_identity_value(before)
    normalized_after = _normalize_identity_value(after)
    group_preimage = {
        "episode_id": episode_id,
        "entity": entity,
        "normalized_before": normalized_before,
    }
    group_id = f"root-group-{sha256_bytes(canonical_json(group_preimage).encode())}"
    change_id = "root-change-" + sha256_bytes(
        canonical_json(
            {
                "group_id": group_id,
                "normalized_after": normalized_after,
                "alias_node_ids": [alias.node_id for alias in aliases],
            }
        ).encode()
    )
    if not aliases:
        ambiguity_reasons.append("no-direct-root-proposition")
    applicability = "applicable" if aliases and not ambiguity_reasons else "root_binding_miss"
    ambiguity_status = (
        "unambiguous"
        if len(aliases) == 1 and not ambiguity_reasons
        else "resolved_alias_group"
        if not ambiguity_reasons
        else "ambiguous"
    )
    return RootIdentity(
        episode_id=episode_id,
        entity=entity,
        normalized_before=normalized_before,
        normalized_after=normalized_after,
        group_id=group_id,
        change_id=change_id,
        aliases=tuple(aliases),
        applicability=applicability,
        ambiguity_status=ambiguity_status,
        ambiguity_reasons=tuple(sorted(ambiguity_reasons)),
        direct_candidates=tuple(direct_candidates),
        ambiguous_candidates=tuple(ambiguous_candidates),
        rejected_reason_clause_candidates=tuple(rejected_candidates),
    )


def _unique_dataset_episode(
    corpus: "FrozenV3",
    episode_id: str,
    *,
    data: str | Path = DEFAULT_DATA,
) -> Mapping[str, Any]:
    matches = [
        episode
        for episode in corpus.dataset_episodes(data)
        if str(episode.get("episode_id")) == episode_id
    ]
    if len(matches) != 1:
        raise ValueError(f"unique MEME episode missing for {episode_id}")
    return matches[0]


def canonical_evaluation_episode_ids(
    corpus: "FrozenV3",
    *,
    evaluation_hop: int,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> tuple[str, ...]:
    """Derive the scoreable view from dataset question metadata, never labels.

    Returns what the dataset offers for this task type and hop. Research-level
    exclusions (see abs_excluded_episodes.json) are applied separately, so this
    stays a pure dataset-drift detector.
    """
    if evaluation_hop not in {1, 2}:
        raise ValueError("evaluation_hop must be 1 or 2")
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of {sorted(SUPPORTED_TASK_TYPES)}")
    ids: list[str] = []
    seen: set[str] = set()
    for episode in corpus.dataset_episodes(data):
        episode_id = str(episode.get("episode_id") or "")
        before_rows = [
            row
            for row in (episode.get("before_questions") or {}).get("questions") or ()
            if row.get("task_type") == task_type and row.get("hop") == evaluation_hop
        ]
        if not before_rows:
            continue
        if len(before_rows) != 1:
            raise ValueError(
                f"evaluation hop {evaluation_hop} question is ambiguous for {episode_id}"
            )
        before_row = before_rows[0]
        after_rows = [
            row
            for row in (episode.get("after_questions") or {}).get("questions") or ()
            if row.get("task_type") == task_type
            and row.get("hop") == evaluation_hop
            and row.get("question") == before_row.get("question")
            and row.get("entity") == before_row.get("entity")
        ]
        if len(after_rows) != 1:
            raise ValueError(
                f"evaluation hop {evaluation_hop} pair is ambiguous for {episode_id}"
            )
        if not episode_id or episode_id in seen:
            raise ValueError(f"duplicate or missing canonical episode id: {episode_id!r}")
        seen.add(episode_id)
        ids.append(episode_id)
    frozen_ids = set(corpus.episode_ids)
    if not set(ids).issubset(frozen_ids):
        raise ValueError("dataset evaluation view is not contained in frozen corpus")
    return tuple(ids)


ABS_EXCLUSIONS_PATH = Path(__file__).resolve().parent / "abs_excluded_episodes.json"


def abs_excluded_episode_ids(
    hop: int | None = None, path: Path | None = None
) -> frozenset[str]:
    """Episodes withheld from the Abs evaluation set, with recorded reasons.

    Exclusions are per hop, not per episode: an episode excluded at hop1 for a
    predeclaration contradiction may still carry a perfectly scoreable hop2 Abs
    task. Passing ``hop`` returns only that hop's exclusions; omitting it returns
    all of them, which is a superset and must not be used to filter one hop.

    Two grounds only: a predeclaration the graph can never stale (10 hop1 cases,
    where the dataset's own gold contradicts its own conversation), and one hop2
    target fact never uttered in any session. The 8 hop2 cases whose edge the
    frozen graph missed are deliberately NOT here -- that is P1 recall failing on
    the system under test, so they stay in the denominator.
    """
    payload = json.loads((path or ABS_EXCLUSIONS_PATH).read_text(encoding="utf-8"))
    return frozenset(
        str(row["episode_id"])
        for row in payload["excluded"]
        if hop is None or int(row["hop"]) == hop
    )


def evaluation_episode_ids(
    corpus: "FrozenV3",
    *,
    evaluation_hop: int,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> tuple[str, ...]:
    """The canonical dataset view minus task-type-specific exclusions."""
    canonical = canonical_evaluation_episode_ids(
        corpus, evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    if task_type != "Abs":
        return canonical
    excluded = abs_excluded_episode_ids(evaluation_hop)
    return tuple(episode_id for episode_id in canonical if episode_id not in excluded)


def resolve_runtime_episode_input(
    corpus: "FrozenV3",
    episode_id: str,
    *,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> RuntimeEpisodeInput:
    """Read only intervention and selected-view question text; never answer/gold."""
    if evaluation_hop not in {1, 2}:
        raise ValueError("evaluation_hop must be 1 or 2")
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of {sorted(SUPPORTED_TASK_TYPES)}")
    episode = _unique_dataset_episode(corpus, episode_id, data=data)
    entity = str(episode.get("root") or "").strip()
    change = episode.get("root_change") or {}
    before = str(change.get("before") or "").strip()
    after = str(change.get("after") or "").strip()
    before_rows = [
        row
        for row in (episode.get("before_questions") or {}).get("questions") or ()
        if row.get("task_type") == task_type and row.get("hop") == evaluation_hop
    ]
    if len(before_rows) != 1:
        raise ValueError(f"runtime question missing for {episode_id}")
    before_row = before_rows[0]
    after_rows = [
        row
        for row in (episode.get("after_questions") or {}).get("questions") or ()
        if row.get("task_type") == task_type
        and row.get("hop") == evaluation_hop
        and row.get("question") == before_row.get("question")
        and row.get("entity") == before_row.get("entity")
    ]
    before_question = str(before_row.get("question") or "").strip()
    after_question = (
        str(after_rows[0].get("question") or "").strip()
        if len(after_rows) == 1
        else ""
    )
    if not entity or not before or not after or not before_question or not after_question:
        raise ValueError(f"incomplete runtime episode input for {episode_id}")
    change_turns = [
        str(turn.get("content") or "")
        for session in episode.get("sessions") or ()
        if "change" in str(session.get("session_id") or "")
        for turn in session.get("conversation") or []
        if turn.get("role") == "user"
        and after.casefold() in str(turn.get("content") or "").casefold()
    ]
    entity_phrase = entity.replace("_", " ").casefold()
    change_turns.sort(
        key=lambda text: (
            entity_phrase not in text.casefold(),
            len(text),
            text,
        )
    )
    if not change_turns:
        raise ValueError(f"root after-value has no change evidence turn: {episode_id}")
    return RuntimeEpisodeInput(
        episode_id=episode_id,
        entity=entity,
        before=before,
        after=after,
        before_question=before_question,
        after_question=after_question,
        change_evidence_text=change_turns[0],
    )


def load_evaluation_annotations(
    corpus: "FrozenV3",
    episode_id: str,
    *,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> EvaluationAnnotations:
    """Gold-only sidecar. Call only after all runtime answer decisions are fixed."""
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of {sorted(SUPPORTED_TASK_TYPES)}")
    episode = _unique_dataset_episode(corpus, episode_id, data=data)
    before_rows = [
        row
        for row in (episode.get("before_questions") or {}).get("questions") or ()
        if row.get("task_type") == task_type and row.get("hop") == evaluation_hop
    ]
    if len(before_rows) != 1:
        raise ValueError(f"evaluation question missing for {episode_id}")
    before_row = before_rows[0]
    after_rows = [
        row
        for row in (episode.get("after_questions") or {}).get("questions") or ()
        if row.get("task_type") == task_type
        and row.get("hop") == evaluation_hop
        and row.get("question") == before_row.get("question")
        and row.get("entity") == before_row.get("entity")
    ]
    if len(after_rows) != 1:
        raise ValueError(f"evaluation question pair is ambiguous for {episode_id}")
    return EvaluationAnnotations(
        episode_id=episode_id,
        before_reference=str(before_row.get("expected_answer") or ""),
        after_reference=str(after_rows[0].get("gold_answer") or ""),
        target_entity=str((before_row.get("entity") or ["target"])[0]),
        annotated_hop=(
            int(before_row["hop"]) if before_row.get("hop") is not None else None
        ),
    )


def resolve_retrieval_evidence_contract(
    corpus: "FrozenV3",
    episode_id: str,
    *,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
    annotations: EvaluationAnnotations | None = None,
) -> RetrievalEvidenceContract:
    """Post-hoc mapping. Missing paths/surfaces are data, never exceptions."""
    runtime_input = resolve_runtime_episode_input(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    annotations = annotations or load_evaluation_annotations(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    identity = resolve_root_identity(corpus, episode_id, data=data)
    episode = corpus.episode(episode_id)
    edges = corpus.selected_edges(episode_id)
    reachable = set(identity.alias_node_ids)
    changed = True
    while changed:
        changed = False
        for edge in edges:
            source = str(edge["dependency_node_id"])
            target = str(edge["dependent_node_id"])
            if source in reachable and target not in reachable:
                reachable.add(target)
                changed = True

    old_reference = annotations.before_reference
    replacement_reference = annotations.after_reference
    old_candidates = [
        node
        for node in episode["nodes"]
        if str(node["node_id"]) not in identity.alias_node_ids
        and _text_contains_reference(str(node.get("text") or ""), old_reference)
    ]
    replacement_candidates = [
        node
        for node in episode["nodes"]
        if str(node["node_id"]) not in identity.alias_node_ids
        and _text_contains_reference(str(node.get("text") or ""), replacement_reference)
    ]
    old_candidates.sort(
        key=lambda row: (len(str(row.get("text") or "")), row["node_id"])
    )
    replacement_candidates.sort(
        key=lambda row: (len(str(row.get("text") or "")), row["node_id"])
    )
    old_reference_tokens = set(
        re.findall(r"[a-z0-9]+", old_reference.casefold())
    )
    surface_candidates = []
    if not old_candidates and old_reference_tokens:
        for node in episode["nodes"]:
            if str(node["node_id"]) in identity.alias_node_ids:
                continue
            node_tokens = set(
                re.findall(r"[a-z0-9]+", str(node.get("text") or "").casefold())
            )
            if len(old_reference_tokens & node_tokens) / len(old_reference_tokens) >= 0.5:
                surface_candidates.append(node)
    surface_candidates.sort(
        key=lambda row: (len(str(row.get("text") or "")), row["node_id"])
    )
    old_node_ids = tuple(str(row["node_id"]) for row in old_candidates)
    surface_node_ids = tuple(str(row["node_id"]) for row in surface_candidates)
    reachable_old_node_ids = tuple(
        node_id
        for node_id in old_node_ids or surface_node_ids
        if node_id in reachable
    )
    replacement_node_ids = tuple(str(row["node_id"]) for row in replacement_candidates)
    return RetrievalEvidenceContract(
        before_question=runtime_input.before_question,
        after_question=runtime_input.after_question,
        root_group_id=identity.group_id,
        old_identity_id="old-identity-"
        + sha256_bytes(
            canonical_json(
                {
                    "episode_id": episode_id,
                    "node_ids": old_node_ids,
                    "reference": _normalize_identity_value(old_reference),
                }
            ).encode()
        ),
        old_target_node_ids=old_node_ids,
        replacement_identity_id="replacement-identity-"
        + sha256_bytes(
            canonical_json(
                {
                    "episode_id": episode_id,
                    "node_ids": replacement_node_ids,
                    "reference": _normalize_identity_value(replacement_reference),
                }
            ).encode()
        ),
        replacement_node_ids=replacement_node_ids,
        old_reference=old_reference,
        replacement_reference=replacement_reference,
        p1_path_status=(
            "present"
            if reachable_old_node_ids
            else "root-binding-miss"
            if identity.applicability != "applicable"
            else "p1-system-miss"
        ),
        old_mapping_status=(
            "mapped"
            if old_node_ids
            else "surface-mismatch"
            if surface_node_ids
            else "unobservable"
        ),
        replacement_mapping_status=(
            "mapped" if replacement_node_ids else "replacement-not-declared"
        ),
        surface_candidate_node_ids=surface_node_ids,
    )


def resolve_root_workload(
    corpus: FrozenV3,
    episode_id: str,
    *,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> RootWorkload:
    """Bind a MEME root change to one frozen semantic alias group."""
    runtime_input = resolve_runtime_episode_input(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    root_identity = resolve_root_identity(corpus, episode_id, data=data)
    return RootWorkload(
        episode_id=episode_id,
        entity=runtime_input.entity,
        before=runtime_input.before,
        after=runtime_input.after,
        root_identity=root_identity,
        change_evidence_text=runtime_input.change_evidence_text,
    )


def node_uuid(episode_id: str, frozen_node_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{SCHEMA_VERSION}:{episode_id}:{frozen_node_id}")


def collapse_root_alias_edges(
    edges: Sequence[Mapping[str, Any]],
    root_identity: RootIdentity,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collapse root aliases while preserving the complete frozen P1 graph audit."""
    alias_ids = set(root_identity.alias_node_ids)
    exact_seen: set[tuple[str, str]] = set()
    root_target_owner: dict[str, str] = {}
    suppressed_intra: list[dict[str, Any]] = []
    suppressed_reconvergent: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    for edge in sorted(
        (dict(row) for row in edges),
        key=lambda row: (
            str(row["dependency_node_id"]),
            str(row["dependent_node_id"]),
        ),
    ):
        source = str(edge["dependency_node_id"])
        target = str(edge["dependent_node_id"])
        key = (source, target)
        if key in exact_seen:
            continue
        exact_seen.add(key)
        if source in alias_ids and target in alias_ids:
            suppressed_intra.append(edge)
            continue
        if source in alias_ids:
            owner = root_target_owner.get(target)
            if owner is not None:
                suppressed_reconvergent.append(
                    {**edge, "retained_dependency_node_id": owner}
                )
                continue
            root_target_owner[target] = source
        normalized.append(edge)
    return normalized, {
        "root_group_id": root_identity.group_id,
        "alias_node_ids": list(root_identity.alias_node_ids),
        "published_edge_count": len(exact_seen),
        "runtime_edge_count": len(normalized),
        "suppressed_intra_identity_edges": suppressed_intra,
        "suppressed_reconvergent_root_edges": suppressed_reconvergent,
        "union_root_targets": sorted(root_target_owner),
    }


async def import_frozen_graph(
    db,
    account: str,
    episode: Mapping[str, Any],
    edges,
    *,
    root_identity: RootIdentity,
):
    episode_id = str(episode["episode_id"])
    nodes = {str(node["node_id"]): node for node in episode["nodes"]}
    for frozen_id, node in nodes.items():
        body = str(node.get("text") or "")
        node_id = node_uuid(episode_id, frozen_id)
        await db.execute(
            """
            INSERT INTO contexts (
              id, uri, context_type, scope, owner_space, account_id,
              l0_content, l1_content, l2_content, tags
            )
            VALUES ($1, $2, 'memory', 'agent', $3, $4,
                    $5, $5, $5, $6)
            """,
            node_id,
            f"ctx://agent/meme-full100/memories/{episode_id}-{frozen_id}",
            EVAL_AGENT,
            account,
            body,
            [
                f"frozen_node:{frozen_id}",
                f"origin:{node.get('source_origin', 'shared_preprocessing')}",
            ],
        )
    runtime_edges, alias_edge_audit = collapse_root_alias_edges(edges, root_identity)
    imported_edges: list[tuple[UUID, UUID]] = []
    for edge in runtime_edges:
        source = node_uuid(episode_id, edge["dependency_node_id"])
        target = node_uuid(episode_id, edge["dependent_node_id"])
        if (
            edge["dependency_node_id"] not in nodes
            or edge["dependent_node_id"] not in nodes
        ):
            raise ValueError("v3 edge endpoint missing from frozen shared nodes")
        await db.execute(
            """
            INSERT INTO dependencies (
              dependent_id, dependency_id, dep_type, dependency_version
            )
            VALUES ($1, $2, 'derived_from', 1)
            ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
            """,
            target,
            source,
        )
        imported_edges.append((source, target))
    return nodes, imported_edges, alias_edge_audit


async def apply_root_identity_change(
    db,
    workload: RootWorkload,
    *,
    expected_versions: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Atomically CAS every physical alias inside the caller's transaction."""
    identity = workload.root_identity
    if identity.applicability != "applicable":
        raise ValueError(
            f"root identity is not applicable: {identity.ambiguity_reasons}"
        )
    aliases = sorted(identity.aliases, key=lambda alias: alias.context_id)
    context_ids = [UUID(alias.context_id) for alias in aliases]
    rows = await db.fetch(
        """
        SELECT id::text AS context_id, version, l2_content
          FROM contexts
         WHERE id = ANY($1::uuid[]) AND status != 'deleted'
         ORDER BY id
         FOR UPDATE
        """,
        context_ids,
    )
    current = {str(row["context_id"]): row for row in rows}
    if set(current) != set(identity.alias_context_ids):
        raise ValueError("root alias batch is incomplete")
    for alias in aliases:
        row = current[alias.context_id]
        if int(row["version"]) != int(expected_versions[alias.context_id]):
            raise ValueError(f"root alias version CAS failed: {alias.context_id}")
        if workload.before.casefold() not in str(row["l2_content"] or "").casefold():
            raise ValueError(f"root alias before-value mismatch: {alias.context_id}")

    changes = []
    identity_payload = _root_identity_payload(identity)
    for alias in aliases:
        expected_version = int(expected_versions[alias.context_id])
        change = await ContextService.apply_system_content_change(
            db,
            context_id=UUID(alias.context_id),
            expected_version=expected_version,
            l0_content=workload.change_evidence_text,
            l1_content=workload.change_evidence_text,
            l2_content=workload.change_evidence_text,
            actor="meme-runtime-workload",
            diff_summary=f"{workload.entity}: {workload.before} -> {workload.after}",
            metadata={
                "runtime_safe_workload": {
                    "entity": workload.entity,
                    "before": workload.before,
                    "after": workload.after,
                    "root_group_id": identity.group_id,
                    "root_change_id": identity.change_id,
                    "alias_node_id": alias.node_id,
                    "alias_context_id": alias.context_id,
                    "change_evidence_text": workload.change_evidence_text,
                },
                "root_identity": identity_payload,
                "scoring_only_fields": [],
            },
            idempotency_key=(
                f"meme-root-group:{identity.change_id}:{alias.node_id}:"
                f"{expected_version + 1}"
            ),
            graph_scope="v3",
        )
        changes.append(
            {
                **change,
                "alias_node_id": alias.node_id,
                "alias_context_id": alias.context_id,
                "root_group_id": identity.group_id,
                "root_change_id": identity.change_id,
            }
        )
    return changes


async def create_after_root_fallback(
    db,
    workload: RootWorkload,
    *,
    account: str,
) -> list[dict[str, Any]]:
    """Create an independent after-root fact without touching unbound old nodes."""
    context_id = uuid5(
        NAMESPACE_URL,
        f"{SCHEMA_VERSION}:{workload.episode_id}:unbound-after-root:"
        f"{workload.entity}:{_normalize_identity_value(workload.after)}",
    )
    uri = (
        f"ctx://agent/meme-full100/runtime-roots/{workload.episode_id}-"
        f"{context_id}"
    )
    await db.execute(
        """
        INSERT INTO contexts (
          id, uri, context_type, scope, owner_space, account_id,
          l0_content, l1_content, l2_content, tags
        )
        VALUES ($1, $2, 'memory', 'agent', $3, $4, $5, $5, $5, $6)
        ON CONFLICT (id) DO NOTHING
        """,
        context_id,
        uri,
        EVAL_AGENT,
        account,
        workload.change_evidence_text,
        [
            "runtime_root_fallback:true",
            f"root_entity:{workload.entity}",
            f"root_change_id:{workload.root_identity.change_id}",
        ],
    )
    return [
        {
            "change_kind": "created-independent-after-root",
            "context_id": str(context_id),
            "new_version": 1,
            "root_group_id": workload.root_identity.group_id,
            "root_change_id": workload.root_identity.change_id,
            "fallback": True,
            "old_root_nodes_modified": False,
            "propagation_frontier_proven": False,
            "reason": list(workload.root_identity.ambiguity_reasons),
        }
    ]


class OfflineChat:
    model = "offline-stub"

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        del prompt, max_tokens
        raise AssertionError(
            "no-API smoke selected direct-stale but attempted a model call"
        )


class OfflineIndexer:
    async def generate(self, context_type, content, metadata=None):
        del context_type, metadata
        raise RuntimeError(
            "OfflineIndexer is pass-through-only and cannot perform semantic recompute"
        )

    async def update_embedding(self, db, context_id, content):
        del db, context_id, content
        return True


def validate_stale_isolation_retrieval_contract(
    off: Mapping[str, Any],
    on: Mapping[str, Any],
    *,
    contract: RetrievalEvidenceContract | None = None,
    runtime_input: RuntimeEpisodeInput | None = None,
    state_evidence: Mapping[str, Any],
    root_context_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate trace integrity only; correctness observations never raise."""
    if runtime_input is None and contract is None:
        raise ValueError("runtime input is required")
    expected_query = (
        runtime_input.after_question
        if runtime_input is not None
        else str(contract.after_question)
    )
    off_retrieval = off.get("retrieval") or {}
    on_retrieval = on.get("retrieval") or {}
    off_materialized = off_retrieval.get("final_materialized") or []
    on_materialized = on_retrieval.get("final_materialized") or []
    off_services = [str(row.get("service_content") or "") for row in off_materialized]
    on_services = [str(row.get("service_content") or "") for row in on_materialized]
    contexts = list(state_evidence.get("contexts") or [])

    def state_for(frozen_node_id: str) -> Mapping[str, Any] | None:
        suffix = f"-{frozen_node_id}"
        return next(
            (row for row in contexts if str(row.get("uri") or "").endswith(suffix)),
            None,
        )

    old_states = [
        state
        for node_id in (contract.old_target_node_ids if contract else ())
        if (state := state_for(node_id)) is not None
    ]
    replacement_states = [
        state
        for node_id in (contract.replacement_node_ids if contract else ())
        if (state := state_for(node_id)) is not None
    ]
    old_context_ids = {str(state.get("node_id")) for state in old_states}
    replacement_context_ids = {
        str(state.get("node_id")) for state in replacement_states
    }
    off_ids = {str(row.get("node_id")) for row in off_materialized}
    on_ids = {str(row.get("node_id")) for row in on_materialized}
    unresolved_ids = {
        str(row.get("context_id"))
        for row in state_evidence.get("invalidations") or []
        if str(row.get("context_id")) in old_context_ids
        and row.get("resolution_status") == "unresolved"
    }
    integrity = {
        "production_retrieval": bool(
            off_retrieval.get("retrieval_id") and on_retrieval.get("retrieval_id")
        ),
        "paid_query_unchanged": bool(
            off_retrieval.get("request", {}).get("query")
            == on_retrieval.get("request", {}).get("query")
            == expected_query
        ),
    }
    if not all(integrity.values()):
        raise RuntimeError(f"retrieval trace integrity failed: {integrity}")
    observations = {
        "service_content_changed": off_services != on_services,
        "context_hash_changed": bool(
            off_retrieval.get("context_hash")
            and on_retrieval.get("context_hash")
            and off_retrieval["context_hash"] != on_retrieval["context_hash"]
        ),
        "answer_prompt_changed": bool(
            off.get("prompt_sha256")
            and on.get("prompt_sha256")
            and off["prompt_sha256"] != on.get("prompt_sha256")
        ),
        "old_identity_instances_complete": (
            contract is not None
            and len(old_states) == len(contract.old_target_node_ids)
        ),
        "old_identity_visible_off": bool(old_context_ids)
        and old_context_ids.issubset(off_ids),
        "old_identity_hidden_on": bool(old_context_ids)
        and old_context_ids.isdisjoint(on_ids),
        "old_identity_state_invalidated": bool(old_states)
        and all(
            state.get("status") == "stale"
            or state.get("validity_status") in {"stale", "invalid"}
            for state in old_states
        ),
        "old_identity_has_unresolved_cause": bool(old_context_ids)
        and old_context_ids.issubset(unresolved_ids),
        "replacement_identity_instances_complete": (
            contract is not None
            and len(replacement_states) == len(contract.replacement_node_ids)
        ),
        "replacement_identity_visible_on": bool(replacement_context_ids)
        and replacement_context_ids.issubset(on_ids),
        "replacement_identity_active_fresh": bool(replacement_states)
        and all(
            state.get("status") == "active" and state.get("validity_status") == "fresh"
            for state in replacement_states
        ),
        "replacement_identity_is_not_root": replacement_context_ids.isdisjoint(
            {str(item) for item in root_context_ids}
        ),
    }
    return {
        **integrity,
        **observations,
        "integrity_complete": True,
        "system_outcome_passed": all(observations.values()),
    }


def evaluate_runtime_trace(
    corpus: FrozenV3,
    episode_id: str,
    artifact: Mapping[str, Any],
    *,
    annotations: EvaluationAnnotations,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> dict[str, Any]:
    """Gold-scoring sidecar computed only from a closed runtime trace."""
    contract = resolve_retrieval_evidence_contract(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
        annotations=annotations,
    )
    off_rows = (
        (artifact.get("retrieval") or {}).get("off", {}).get("final_materialized")
        or []
    )
    on_rows = (
        (artifact.get("retrieval") or {}).get("on", {}).get("final_materialized")
        or []
    )
    off_ids = {str(row.get("node_id")) for row in off_rows}
    on_ids = {str(row.get("node_id")) for row in on_rows}
    evaluated_old_node_ids = (
        contract.old_target_node_ids or contract.surface_candidate_node_ids
    )
    old_ids = {
        str(node_uuid(episode_id, node_id))
        for node_id in evaluated_old_node_ids
    }
    replacement_ids = {
        str(node_uuid(episode_id, node_id))
        for node_id in contract.replacement_node_ids
    }
    old_tokens = set(re.findall(r"[a-z0-9]+", annotations.before_reference.casefold()))
    surface_candidates = []
    if not contract.old_target_node_ids and old_tokens:
        for node in corpus.episode(episode_id)["nodes"]:
            node_tokens = set(
                re.findall(r"[a-z0-9]+", str(node.get("text") or "").casefold())
            )
            overlap = len(old_tokens & node_tokens) / len(old_tokens)
            if overlap >= 0.5:
                surface_candidates.append(str(node["node_id"]))
    mapping_status = contract.old_mapping_status
    root_status = str(artifact.get("root_binding_status") or "unknown")
    frontier_status = str(artifact.get("frontier_status") or "unknown")
    old_off = (
        "present"
        if old_ids & off_ids
        else "not-retrieved"
        if old_ids
        else "not-mapped"
    )
    old_on = (
        "old-visible-on"
        if old_ids & on_ids
        else "hidden"
        if old_ids
        else "not-mapped"
    )
    replacement_on = (
        "present"
        if replacement_ids & on_ids
        else "replacement-not-retrieved"
        if replacement_ids
        else "not-declared"
    )
    failures = [
        (
            "root-binding",
            root_status != "bound",
        ),
        ("p1", contract.p1_path_status != "present"),
        ("p2", frontier_status not in {"complete", "empty-no-binding"}),
        ("retrieval-old-off", old_off != "present"),
        ("retrieval-old-on", old_on == "old-visible-on"),
        # Abs asks the model to abstain because no current value exists, so a
        # missing replacement is the premise of the task, not a failure. The
        # ladder above it still applies: the old value must be withheld in ON.
        *(
            ()
            if task_type == "Abs"
            else (("retrieval-replacement", replacement_on != "present"),)
        ),
        ("evaluation-mapping", mapping_status != "mapped"),
    ]
    earliest = next((layer for layer, failed in failures if failed), None)
    return {
        "phase": "post-hoc-after-runtime",
        "runtime_decisions_mutable": False,
        "annotations": annotations.__dict__,
        "retrieval_evidence_contract": {
            **contract.__dict__,
            "old_target_context_ids": sorted(old_ids),
            "replacement_context_ids": sorted(replacement_ids),
        },
        "p1_path_status": contract.p1_path_status,
        "frontier_status": frontier_status,
        "old_visibility_off": old_off,
        "old_visibility_on": old_on,
        "replacement_visibility_on": replacement_on,
        "surface_mapping_status": mapping_status,
        "surface_candidate_node_ids": sorted(surface_candidates),
        "earliest_failure_layer": earliest,
        "task_type": task_type,
        "failure_ladder": [layer for layer, _ in failures],
    }


async def production_no_api_stage(
    system,
    *,
    stage_name: str,
    account: str,
    query: str,
    with_stale_notices: bool = False,
) -> dict[str, Any]:
    retrieval = RetrievalService(
        RetrievalRouter.default(),
        NoOpEmbeddingClient(),
        ACLService(),
        masking_service=MaskingService(),
    )
    request = SearchRequest(
        query=query,
        top_k=100,
        level=ContextLevel.L2,
        include_stale=False,
        include_stale_notices=with_stale_notices,
        context_type=[ContextType.MEMORY],
        scope=[Scope.AGENT],
    )
    async with system.repo.session(account) as db:
        response = await retrieval.search(
            db,
            request,
            RequestContext(account_id=account, agent_id=EVAL_AGENT),
        )
    notes = [
        result.l1_content or result.l0_content or result.l2_content or ""
        for result in response.results
    ]
    prompt = build_answer_prompt(
        "\n".join(f"- {note}" for note in notes if note) or "(no notes found)",
        query,
        response.stale_notices if with_stale_notices else (),
    )
    return {
        "prompt": prompt,
        "prompt_sha256": sha256_bytes(prompt.encode()),
        "raw_answer": f"synthetic-stub:{stage_name}:not-answered",
        "usage": {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tokens_are_real": False,
        },
        "retrieval": {
            "retrieval_id": response.retrieval_id,
            "request": request.model_dump(mode="json"),
            "candidates": response.trace.get("candidates", []),
            "final": [result.model_dump(mode="json") for result in response.results],
            "final_materialized": response.trace.get("final_materialized", []),
            "context_hash": response.trace.get("context_hash"),
        },
        "synthetic_stub": True,
    }


async def collect_runtime_trace(pool, account: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        events = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT event_id::text, context_id::text, change_type,
                       delivery_status, attempt_count, source_version,
                       parent_event_id::text, root_event_id::text, plan_id::text,
                       graph_scope, depth, last_error, terminal_reason, metadata
                  FROM change_events WHERE account_id = $1
                 ORDER BY timestamp, event_id
                """,
                account,
            )
        ]
        effects = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT p.event_id::text, p.effect_key, p.effect_type,
                       p.target_context_id::text, p.source_version, p.status,
                       p.result
                  FROM propagation_effects p
                  JOIN change_events e ON e.event_id = p.event_id
                 WHERE e.account_id = $1
                 ORDER BY p.event_id, p.effect_key
                """,
                account,
            )
        ]
        ledger = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT r.event_id::text, r.edge_key, r.plan_id::text,
                       r.source_version, r.risk_delta
                  FROM propagation_risk_ledger r
                  JOIN change_events e ON e.event_id = r.event_id
                 WHERE e.account_id = $1
                 ORDER BY r.event_id, r.edge_key
                """,
                account,
            )
        ]
        traces = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT t.trace_id, t.event_id::text, t.trace_type, t.payload
                  FROM propagation_trace t
                  JOIN change_events e ON e.event_id = t.event_id
                 WHERE e.account_id = $1 ORDER BY t.trace_id
                """,
                account,
            )
        ]
        contexts = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT id::text AS node_id, uri, version, status,
                       validity_status, semantic_identity,
                       l0_content, l1_content, l2_content
                  FROM contexts WHERE account_id = $1 ORDER BY uri
                """,
                account,
            )
        ]
        dependencies = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT d.dependent_id::text, d.dependency_id::text,
                       d.dep_type, d.dependency_version,
                       source.version AS source_current_version
                  FROM dependencies d
                  JOIN contexts dependent ON dependent.id = d.dependent_id
                  JOIN contexts source ON source.id = d.dependency_id
                 WHERE dependent.account_id = $1 AND source.account_id = $1
                 ORDER BY d.dependent_id, d.dependency_id, d.dep_type
                """,
                account,
            )
        ]
        invalidations = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT i.context_id::text, i.cause_event_id::text,
                       i.source_context_id::text, i.source_version,
                       i.reason_hash, i.created_at::text,
                       i.resolved_at::text,
                       CASE WHEN i.resolved_at IS NULL
                            THEN 'unresolved' ELSE 'resolved' END
                         AS resolution_status
                  FROM context_invalidations i
                  JOIN contexts c ON c.id = i.context_id
                 WHERE c.account_id = $1
                 ORDER BY i.context_id, i.cause_event_id
                """,
                account,
            )
        ]
    return {
        "events": events,
        "effects": effects,
        "risk_ledger": ledger,
        "trace": traces,
        "contexts": contexts,
        "dependencies": dependencies,
        "invalidations": invalidations,
    }


async def collect_state_evidence(pool, account: str) -> dict[str, Any]:
    runtime = await collect_runtime_trace(pool, account)
    return {
        "contexts": runtime["contexts"],
        "dependencies": runtime["dependencies"],
        "invalidations": runtime["invalidations"],
    }


def no_api_layer_cost() -> dict[str, Any]:
    layers = {}
    for layer in (
        "extract",
        "p1_cheap",
        "p1_strong",
        "p2_cheap",
        "p2_strong",
        "retrieval",
        "answer",
        "judge",
        "embedding",
    ):
        layers[layer] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tokens_are_real": True,
            "estimated_calls": 0,
            "retry_spend_usd": 0.0,
            "model": "offline-stub",
            "price_snapshot": {"input_per_million": 0.0, "output_per_million": 0.0},
            "known_usd": 0.0,
        }
    return {
        "layers": layers,
        "cost_complete": True,
        "known_usd": 0.0,
        "strict_usd_interval": [0.0, 0.0],
    }


async def run_smoke_case(
    system,
    corpus: FrozenV3,
    episode_id: str,
    out: Path,
    *,
    stage_callback: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
    identity: Mapping[str, Any] | None = None,
    artifact_mode: str = "no-api",
    evaluation_hop: int = 1,
    data: str | Path = DEFAULT_DATA,
) -> Path:
    if artifact_mode not in {"no-api", "paid-smoke", "full-run"}:
        raise ValueError(f"unknown artifact mode: {artifact_mode}")
    episode = corpus.episode(episode_id)
    identity = identity or build_run_identity(
        corpus,
        evaluation_hop=evaluation_hop,
        models={"all": "offline-stub"},
        prices={
            "offline-stub": {
                "input_per_million": 0.0,
                "output_per_million": 0.0,
            }
        },
        provider_config={"provider": "offline", "embedding_provider": "offline"},
        data=data,
    )
    task_type = identity_task_type(identity)
    with_stale_notices = identity_with_stale_notices(identity)
    runtime_input = resolve_runtime_episode_input(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    workload = resolve_root_workload(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    frozen_edges = corpus.selected_edges(episode_id)
    account = f"meme-v3-smoke-{episode_id}"
    case_dir = out / "cases" / episode_id
    await wipe_account(system, account)
    primary_error: Exception | None = None
    artifact_path: Path | None = None
    observed: dict[str, dict[str, Any]] = {}
    questions = {
        "before": runtime_input.before_question,
        "off": runtime_input.after_question,
        "on": runtime_input.after_question,
    }
    state_evidence: dict[str, dict[str, Any]] = {}
    try:
        async with system.repo.session(account) as db:
            _nodes, edges, alias_edge_audit = await import_frozen_graph(
                db,
                account,
                episode,
                frozen_edges,
                root_identity=workload.root_identity,
            )
        state_evidence["before"] = await collect_state_evidence(system.pool, account)
        if stage_callback is not None:
            observed["before"] = await stage_callback("before", account)
        else:
            observed["before"] = await production_no_api_stage(
                system,
                stage_name="before",
                account=account,
                query=questions["before"],
                with_stale_notices=with_stale_notices,
            )
        async with system.repo.session(account) as db:
            if workload.root_identity.applicability == "applicable":
                expected_root_versions = {
                    context_id: 1
                    for context_id in workload.root_identity.alias_context_ids
                }
                root_updates = await apply_root_identity_change(
                    db,
                    workload,
                    expected_versions=expected_root_versions,
                )
            else:
                root_updates = await create_after_root_fallback(
                    db,
                    workload,
                    account=account,
                )
        root_context_ids = set(workload.root_identity.alias_context_ids)
        root_event_ids = {
            str(row["event_id"]) for row in root_updates if row.get("event_id")
        }
        state_evidence["off"] = await collect_state_evidence(system.pool, account)
        if stage_callback is not None:
            observed["off"] = await stage_callback("off", account)
        else:
            observed["off"] = await production_no_api_stage(
                system,
                stage_name="off",
                account=account,
                query=questions["off"],
                with_stale_notices=with_stale_notices,
            )
        planning_edges = edges if root_event_ids else []
        plan = (
            plan_published_graph(
                planning_edges,
                contract=DIRECT_CONTRACT,
                epsilon=0.1,
                contract_method="cp-upper",
            )
            if planning_edges
            else {
                "assignments": [],
                "assignment_audit": [],
                "planned_mode_mix": {},
                "epsilon_prop": 0.1,
                "max_path_risk": 0.0,
            }
        )
        async with system.pool.acquire() as conn:
            version_rows = await conn.fetch(
                """
                SELECT id::text AS node_id, version
                  FROM contexts WHERE account_id = $1
                """,
                account,
            )
        plan["source_versions"] = {
            row["node_id"]: int(row["version"]) for row in version_rows
        }
        rule = PlannedDerivedMemoryRule(
            modes=plan["assignments"],
            repo=system.repo,
            strong_chat=OfflineChat(),
            cheap_chat=OfflineChat(),
        )
        plan["assignment_audit"] = [
            {**item, "mode": "direct-stale"} for item in plan["assignment_audit"]
        ]
        plan["assignments"] = [
            type(item)(item.dependency_id, item.dependent_id, "direct-stale")
            for item in plan["assignments"]
        ]
        plan["planned_mode_mix"] = {"direct-stale": len(planning_edges)}
        system.rule_registry = make_planned_registry(rule)
        if planning_edges:
            await bind_root_plan(system, account, plan, DIRECT_CONTRACT)
            offline_indexer = OfflineIndexer()
            engine = PropagationEngine(
                system.repo,
                system.pool,
                "postgresql://unused",
                system.rule_registry,
                LifecycleService(indexer=offline_indexer),
                cascade_on_stale=True,
                worker_id=f"offline-smoke-{episode_id}",
            )
            engine._running = True
            for _ in range(max(8, len(planning_edges) * 2)):
                report = await engine.drain_once()
                if report.claimed == 0:
                    break
        runtime = await collect_runtime_trace(system.pool, account)
        state_evidence["on"] = {
            "contexts": runtime["contexts"],
            "dependencies": runtime["dependencies"],
            "invalidations": runtime["invalidations"],
        }
        if stage_callback is not None:
            observed["on"] = await stage_callback("on", account)
        else:
            observed["on"] = await production_no_api_stage(
                system,
                stage_name="on",
                account=account,
                query=questions["on"],
                with_stale_notices=with_stale_notices,
            )
        retrieval_change_checks = validate_stale_isolation_retrieval_contract(
            observed["off"],
            observed["on"],
            runtime_input=runtime_input,
            state_evidence=state_evidence["on"],
            root_context_ids=sorted(root_context_ids),
        )
        unfinished = [
            row for row in runtime["events"] if row["delivery_status"] != "succeeded"
        ]
        executed = [row for row in runtime["effects"] if row["status"] == "succeeded"]
        ledger_keys = {
            (row["event_id"], row["edge_key"]) for row in runtime["risk_ledger"]
        }
        expected_ledger = {
            (
                row["event_id"],
                f"{row['result']['edge'][0]}->{row['result']['edge'][1]}",
            )
            for row in executed
            if row["effect_type"] == "dependency" and row["result"].get("edge")
        }
        total_risk = sum(float(row["risk_delta"]) for row in runtime["risk_ledger"])
        risk_complete = (
            ledger_keys == expected_ledger
            and len(ledger_keys) == len(runtime["risk_ledger"])
            and total_risk <= float(plan["epsilon_prop"])
            and all(float(row["risk_delta"]) >= 0 for row in runtime["risk_ledger"])
        )
        reachable_nodes = {UUID(context_id) for context_id in root_context_ids}
        expected_edge_pairs: set[tuple[UUID, UUID]] = set()
        changed = True
        while changed:
            changed = False
            for src, dst in edges:
                if src in reachable_nodes and (src, dst) not in expected_edge_pairs:
                    expected_edge_pairs.add((src, dst))
                    if dst not in reachable_nodes:
                        reachable_nodes.add(dst)
                    changed = True
        expected_edges = {f"{src}->{dst}" for src, dst in expected_edge_pairs}
        executed_edges = {
            f"{row['result']['edge'][0]}->{row['result']['edge'][1]}"
            for row in executed
            if row["effect_type"] == "dependency" and row["result"].get("edge")
        }
        root_events = [
            row for row in runtime["events"] if row["event_id"] in root_event_ids
        ]
        frontier_complete = bool(root_event_ids) and (
            len(root_events) == len(root_event_ids)
            and {row["context_id"] for row in root_events} == root_context_ids
            and all(
                (row.get("metadata") or {}).get("root_identity", {}).get("group_id")
                == workload.root_identity.group_id
                for row in root_events
            )
            and not unfinished
            and expected_edges == executed_edges
        )
        capability_observations = {
            "durable_invalidation_observed": bool(
                root_event_ids
                and not unfinished
                and risk_complete
                and not any(
                    row["delivery_status"] == "dead_letter" for row in runtime["events"]
                )
                and not rule.planning_errors
            ),
            "semantic_recompute_available": False,
            "semantic_recompute_observed": False,
            "receding_horizon_recompute_observed": False,
            "continuation_mode": "invalidation-only fail-closed",
            "development_reevaluation": True,
            "held_out_data_independent": False,
            "observation_limitations": [
                "semantic_recompute_unavailable",
                "semantic_recompute_not_observed",
                "receding_horizon_recompute_not_observed",
            ],
        }
        question = f"Offline replay question for {episode_id}"
        answer = observed["on"]["raw_answer"]
        judge_prompt = canonical_json(
            {
                "synthetic_stub": True,
                "question": question,
                "answer": answer,
                "instruction": "schema-only; no correctness judgment",
            }
        )
        root_binding_status = (
            "bound"
            if workload.root_identity.applicability == "applicable"
            else "root-binding-miss"
        )
        frontier_status = (
            "empty-no-binding"
            if root_binding_status == "root-binding-miss"
            else "complete"
            if frontier_complete
            else "p2-miss"
        )
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "schema_generation": SCHEMA_GENERATION,
            "artifact_mode": artifact_mode,
            "episode_id": episode_id,
            "evaluation_hop": evaluation_hop,
            "task_type": task_type,
            "with_stale_notices": with_stale_notices,
            "development_reevaluation": True,
            "held_out_data_independent": False,
            "synthetic_stub": stage_callback is None,
            "execution_complete": True,
            "runtime_status": (
                "executed" if root_binding_status == "bound" else "root-binding-miss"
            ),
            "root_binding_status": root_binding_status,
            "p1_path_status": "pending-post-hoc-evaluation",
            "frontier_status": frontier_status,
            "old_visibility_off": "pending-post-hoc-evaluation",
            "old_visibility_on": "pending-post-hoc-evaluation",
            "replacement_visibility_on": "pending-post-hoc-evaluation",
            "earliest_failure_layer": (
                "root-binding" if root_binding_status == "root-binding-miss" else None
            ),
            "stale_isolation_retrieval_verified": retrieval_change_checks,
            "production_retrieval_change_verified": retrieval_change_checks,
            "authorization": {
                "run_config_hash": identity["run_config_hash"],
                "input_hashes": identity["input_hashes"],
                "run_config": identity["config"],
                "run_config_hash_recomputed": sha256_bytes(
                    canonical_json(identity["config"]).encode()
                ),
                "secrets_persisted": False,
            },
            "workload": {
                "runtime_safe_fields": {
                    "episode_id": workload.episode_id,
                    "entity": workload.entity,
                    "before": workload.before,
                    "after": workload.after,
                    "change_evidence_text": workload.change_evidence_text,
                    "root_identity": _root_identity_payload(workload.root_identity),
                    "root_group_id": workload.root_identity.group_id,
                    "root_change_id": workload.root_identity.change_id,
                    "root_context_ids": sorted(root_context_ids),
                    "alias_updates": root_updates,
                    "event_ids": sorted(root_event_ids),
                    "runtime_episode_input": runtime_input.__dict__,
                    "fallback": (
                        root_updates
                        if root_binding_status == "root-binding-miss"
                        else []
                    ),
                },
                "scoring_only_fields": [],
            },
            "retrieval_evidence_contract": {
                "phase": "pending-post-hoc-evaluation",
                "gold_entered_retrieval_query": False,
                "references_loaded": False,
            },
            "capability_observations": capability_observations,
            "extraction": {
                "nodes": episode["nodes"],
                "raw_sessions": episode["raw_sessions"],
                "extractor_calls": episode["extractor_calls"],
                "alignments": episode["alignments"],
            },
            "p1_graph": {
                "nodes": episode["nodes"],
                "candidate_snapshot": episode["candidate_traces"],
                "hmax": episode["same_session_candidates"],
                "routed_candidates": episode["selector_cases"],
                "selected_edges": frozen_edges,
                "root_identity": _root_identity_payload(workload.root_identity),
                "root_alias_edge_collapse": alias_edge_audit,
                "persisted_edges": [
                    {"dependency_id": str(src), "dependent_id": str(dst)}
                    for src, dst in edges
                ],
                "frozen_v3_artifact_sha256": identity["input_hashes"][
                    "v3_case_success_sha256"
                ],
            },
            "p2_queue": {
                "events": runtime["events"],
                "trace": runtime["trace"],
                "unfinished": unfinished,
                "root_group_id": workload.root_identity.group_id,
                "root_change_id": workload.root_identity.change_id,
                "root_event_ids": sorted(root_event_ids),
            },
            "p2_edges": {
                "planned": plan["assignment_audit"],
                "executed": runtime["effects"],
                "decision_trace": rule.executed_audit,
                "risk_ledger": runtime["risk_ledger"],
                "planning_errors": rule.planning_errors,
                "expected_reachable_frontier": sorted(expected_edges),
                "executed_frontier": sorted(executed_edges),
                "root_alias_union": alias_edge_audit,
                "invalidation_frontier_complete": frontier_complete,
                "frontier_complete": frontier_complete,
                "capability": "invalidation-only fail-closed",
                "semantic_recompute_available": False,
                "semantic_recompute_observed": False,
                "receding_horizon_recompute_observed": False,
                "planner_max_path_risk": plan["max_path_risk"],
                "cumulative_executed_risk": total_risk,
            },
            "state_evidence": {
                **state_evidence,
                "scope": "single account/case",
                "content_bounded_to_imported_frozen_graph": True,
                "filter_contract": retrieval_change_checks,
                "root_alias_updates": root_updates,
            },
            "retrieval": {
                "before": observed["before"]["retrieval"],
                "off": observed["off"]["retrieval"],
                "on": observed["on"]["retrieval"],
                "filter_reason": "production RetrievalService active+fresh exact-version",
            },
            "answers": observed,
            "judge": {
                "prompt": judge_prompt,
                "question": question,
                "gold": None,
                "answer": answer,
                "raw_output": "synthetic-stub:not-scored",
                "parsed_verdict": {"status": "not_scored"},
                "fallback": False,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "gold_entered_runtime": False,
                "synthetic_stub": True,
            },
            "cost": {
                "frozen_v3_episode": corpus.episode_cost(episode_id),
                "frozen_shared_preprocessing": episode["cost"],
                "this_execution": no_api_layer_cost(),
                "cost_complete": bool(
                    episode["cost"]["shared_preprocessing_total"]["cost_complete"]
                    and corpus.episode_cost(episode_id)["new"][
                        "cost_incomplete_attempts"
                    ]
                    == 0
                ),
                "known_usd": corpus.episode_cost(episode_id)["new"]["deployment"],
                "strict_usd_interval": [
                    corpus.episode_cost(episode_id)["new"]["deployment"],
                    (
                        corpus.episode_cost(episode_id)["new"]["deployment"]
                        if (
                            episode["cost"]["shared_preprocessing_total"][
                                "cost_complete"
                            ]
                            and corpus.episode_cost(episode_id)["new"][
                                "cost_incomplete_attempts"
                            ]
                            == 0
                        )
                        else None
                    ),
                ],
            },
        }
        if artifact_mode == "no-api":
            annotations = load_evaluation_annotations(
                corpus,
                episode_id,
                evaluation_hop=evaluation_hop,
                task_type=task_type,
                data=data,
            )
            evaluation = evaluate_runtime_trace(
                corpus,
                episode_id,
                artifact,
                annotations=annotations,
                evaluation_hop=evaluation_hop,
                task_type=task_type,
                data=data,
            )
            artifact["post_hoc_evaluation"] = evaluation
            artifact["retrieval_evidence_contract"] = evaluation[
                "retrieval_evidence_contract"
            ]
            for field in (
                "p1_path_status",
                "frontier_status",
                "old_visibility_off",
                "old_visibility_on",
                "replacement_visibility_on",
                "earliest_failure_layer",
            ):
                artifact[field] = evaluation[field]
        verify_run_identity(identity, corpus=corpus, data=data)
        write_case_artifact(case_dir, artifact)
        artifact_path = case_dir / "artifact.json"
    except Exception as exc:
        primary_error = exc
    try:
        await wipe_account(system, account)
    except Exception as cleanup_exc:
        if primary_error is not None:
            raise RuntimeError(
                f"case failed ({primary_error}); cleanup also failed ({cleanup_exc})"
            ) from cleanup_exc
        raise
    if primary_error is not None:
        raise primary_error
    assert artifact_path is not None
    return artifact_path


def analyze_frozen_frontier_bounds(
    corpus: FrozenV3,
    *,
    data: str | Path = DEFAULT_DATA,
) -> dict[str, Any]:
    max_events = 0
    max_depth = 0
    max_episode = None
    for episode_id in corpus.episode_ids:
        workload = resolve_root_workload(corpus, episode_id, data=data)
        runtime_edges, _ = collapse_root_alias_edges(
            corpus.selected_edges(episode_id),
            workload.root_identity,
        )
        adjacency: dict[str, list[str]] = {}
        for edge in runtime_edges:
            adjacency.setdefault(str(edge["dependency_node_id"]), []).append(
                str(edge["dependent_node_id"])
            )
        roots = set(workload.root_identity.alias_node_ids)
        reachable = set(roots)
        stack = list(roots)
        while stack:
            source = stack.pop()
            for target in adjacency.get(source, ()):
                if target not in reachable:
                    reachable.add(target)
                    stack.append(target)
        indegree = {node: 0 for node in reachable}
        for source in reachable:
            for target in adjacency.get(source, ()):
                if target in indegree:
                    indegree[target] += 1
        queue = [node for node, degree in indegree.items() if degree == 0]
        topological: list[str] = []
        while queue:
            source = queue.pop()
            topological.append(source)
            for target in adjacency.get(source, ()):
                if target not in indegree:
                    continue
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(topological) != len(reachable):
            raise ValueError(f"frozen reachable frontier is cyclic: {episode_id}")
        path_count = {node: 0 for node in reachable}
        for root in roots:
            path_count[root] = 1
        depth = {node: 0 for node in reachable}
        for source in topological:
            for target in adjacency.get(source, ()):
                if target not in reachable:
                    continue
                path_count[target] += path_count[source]
                depth[target] = max(depth[target], depth[source] + 1)
        event_count = sum(path_count.values())
        episode_depth = max(depth.values(), default=0)
        if event_count > max_events:
            max_events = event_count
            max_episode = episode_id
        max_depth = max(max_depth, episode_depth)
    return {
        "frozen_reachable_graphs_are_dag": True,
        "max_expected_events_per_root": max_events,
        "max_expected_depth": max_depth,
        "max_event_episode": max_episode,
        "runtime_max_events_per_root": 10_000,
        "runtime_max_depth": 64,
        "within_runtime_bounds": max_events <= 10_000 and max_depth <= 64,
        "reconvergence_semantics": "event/effect idempotency is per parent event path",
    }


def canonical_offline_status_preflight(
    corpus: FrozenV3 | None = None,
    *,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    data: str | Path = DEFAULT_DATA,
) -> dict[str, Any]:
    """Close all canonical statuses without DB, retrieval, or model clients."""
    corpus = corpus or FrozenV3()
    rows = []
    expected_ids = evaluation_episode_ids(
        corpus, evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    for episode_id in expected_ids:
        try:
            runtime_input = resolve_runtime_episode_input(
                corpus,
                episode_id,
                evaluation_hop=evaluation_hop,
                task_type=task_type,
                data=data,
            )
        except ValueError as exc:
            rows.append(
                {
                    "episode_id": episode_id,
                    "runtime_status": "input-invalid",
                    "root_binding_status": "not-attempted",
                    "p1_path_status": "not-evaluated",
                    "reason": str(exc),
                }
            )
            continue
        identity = resolve_root_identity(corpus, episode_id, data=data)
        annotations = load_evaluation_annotations(
            corpus,
            episode_id,
            evaluation_hop=evaluation_hop,
            task_type=task_type,
            data=data,
        )
        outcome = resolve_retrieval_evidence_contract(
            corpus,
            episode_id,
            evaluation_hop=evaluation_hop,
            task_type=task_type,
            data=data,
            annotations=annotations,
        )
        rows.append(
            {
                "episode_id": episode_id,
                "runtime_status": (
                    "executed"
                    if identity.applicability == "applicable"
                    else "root-binding-miss"
                ),
                "root_binding_status": (
                    "bound"
                    if identity.applicability == "applicable"
                    else "root-binding-miss"
                ),
                "direct_alias_count": len(identity.aliases),
                "ambiguous_candidate_count": len(identity.ambiguous_candidates),
                "rejected_reason_clause_count": len(
                    identity.rejected_reason_clause_candidates
                ),
                "p1_path_status": outcome.p1_path_status,
                "surface_mapping_status": outcome.old_mapping_status,
                "replacement_mapping_status": outcome.replacement_mapping_status,
                "runtime_input_sha256": sha256_bytes(
                    canonical_json(runtime_input.__dict__).encode()
                ),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_hop": evaluation_hop,
        "case_count": len(rows),
        "all_statuses_closed": len(rows) == len(expected_ids),
        "rows": rows,
        "statistics": {
            "direct_aliases": sum(int(row.get("direct_alias_count", 0)) for row in rows),
            "multi_alias_episodes": sum(
                int(row.get("direct_alias_count", 0)) > 1 for row in rows
            ),
            "root_binding_miss": sum(
                row["root_binding_status"] == "root-binding-miss" for row in rows
            ),
            "input_invalid": sum(
                row["runtime_status"] == "input-invalid" for row in rows
            ),
            "p1_system_miss": sum(
                row["p1_path_status"] == "p1-system-miss" for row in rows
            ),
            "surface_mismatch": sum(
                row.get("surface_mapping_status") == "surface-mismatch"
                for row in rows
            ),
            "rejected_reason_clause_candidates": sum(
                int(row.get("rejected_reason_clause_count", 0)) for row in rows
            ),
            "ambiguous_candidates": sum(
                int(row.get("ambiguous_candidate_count", 0)) for row in rows
            ),
        },
    }


async def preflight(
    system,
    corpus: FrozenV3,
    out: Path,
    models: Mapping[str, str],
    *,
    prices: Mapping[str, Any] | None = None,
    provider_config: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
    command_mode: str = "preflight",
    evaluation_hop: int = 1,
    data: str | Path = DEFAULT_DATA,
):
    identity = identity or build_run_identity(
        corpus,
        evaluation_hop=evaluation_hop,
        models=models,
        prices=prices or {},
        provider_config=provider_config or {},
        data=data,
    )
    ensure_output_directory(out, identity, command_mode=command_mode)
    checks: dict[str, Any] = {
        "schema_generation": SCHEMA_GENERATION,
        "mode_family": MODE_FAMILY,
        "command_mode": command_mode,
        "run_config_hash": identity["run_config_hash"],
        "input_hashes": identity["input_hashes"],
        "run_config": identity["config"],
        "evaluation_hop": evaluation_hop,
    }
    async with system.pool.acquire() as conn:
        checks["revision_008"] = (
            await conn.fetchval("SELECT version_num FROM alembic_version") == "008"
        )
        checks["db_clean"] = (
            await conn.fetchval(
                """
                SELECT COUNT(*) FROM contexts
                 WHERE account_id LIKE 'meme-v3-%'
                """
            )
            == 0
        )
    checks["shared_100_episodes"] = len(corpus.episode_ids) == 100
    task_type = identity_task_type(identity)
    checks["task_type"] = task_type
    canonical_ids = canonical_evaluation_episode_ids(
        corpus, evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    checks["canonical_view_nonempty"] = bool(canonical_ids)
    checks["canonical_view_unique"] = len(canonical_ids) == len(set(canonical_ids))
    checks["canonical_view_count"] = len(canonical_ids)
    # Exact expected counts, per task type and hop. These are deliberately hard
    # numbers rather than "any count": they are what catches a corpus bump or a
    # broken question filter. A count that legitimately changes should be edited
    # here on purpose, never relaxed away.
    checks["canonical_hop1_100"] = (
        evaluation_hop != 1 or task_type != "Cas" or len(canonical_ids) == 100
    )
    checks["canonical_hop2_64"] = (
        evaluation_hop != 2 or task_type != "Cas" or len(canonical_ids) == 64
    )
    checks["canonical_abs_hop1_100"] = (
        evaluation_hop != 1 or task_type != "Abs" or len(canonical_ids) == 100
    )
    checks["canonical_abs_hop2_30"] = (
        evaluation_hop != 2 or task_type != "Abs" or len(canonical_ids) == 30
    )
    # Post-exclusion evaluation set: 119 Abs cases = 90 hop1 + 29 hop2.
    evaluation_ids = evaluation_episode_ids(
        corpus, evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    checks["evaluation_view_count"] = len(evaluation_ids)
    checks["abs_excluded_count"] = (
        len(canonical_ids) - len(evaluation_ids) if task_type == "Abs" else 0
    )
    checks["canonical_abs_hop1_90_after_exclusion"] = (
        evaluation_hop != 1 or task_type != "Abs" or len(evaluation_ids) == 90
    )
    checks["canonical_abs_hop2_29_after_exclusion"] = (
        evaluation_hop != 2 or task_type != "Abs" or len(evaluation_ids) == 29
    )
    offline_status = canonical_offline_status_preflight(
        corpus, evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    checks["offline_status_scan_complete"] = bool(
        offline_status["all_statuses_closed"]
        # The status scan covers the evaluated set, which for Abs is smaller than
        # the canonical dataset view by the recorded exclusions.
        and offline_status["case_count"] == len(evaluation_ids)
    )
    atomic_json(out / "offline_status_preflight.json", offline_status)
    checks["v3_hash_present"] = (corpus.v3 / "output_hashes.json").is_file()
    checks["models_explicit"] = bool(models) and all(models.values())
    checks["disk_bytes_free"] = shutil.disk_usage(out.parent).free
    checks["disk_sufficient"] = checks["disk_bytes_free"] > 500_000_000
    checks["artifact_schema"] = list(TRACE_SECTIONS)
    checks["gold_isolation"] = all(
        "gold" not in corpus.episode(ep) for ep in corpus.episode_ids
    )
    checks["checkpoint_identity"] = SCHEMA_VERSION
    checks["provider_checked_without_request"] = True
    checks["provider_config"] = identity["config"]["provider_config"]
    checks["price_table_present"] = all(
        model == "offline-stub" or (prices is not None and model in prices)
        for model in models.values()
    )
    checks["frontier_bounds"] = analyze_frozen_frontier_bounds(corpus, data=data)
    checks["frozen_reachable_graphs_are_dag"] = checks["frontier_bounds"][
        "frozen_reachable_graphs_are_dag"
    ]
    checks["frontier_within_runtime_bounds"] = checks["frontier_bounds"][
        "within_runtime_bounds"
    ]
    # `passed` is the conjunction of every boolean gate. Non-boolean entries are
    # recorded context, not gates, and every one of them must be listed in
    # PREFLIGHT_CONTEXT_KEYS; an unlisted one would otherwise silently make
    # `passed` false forever (this bit us once when the Abs keys were added).
    unlisted_context = sorted(
        key
        for key, value in checks.items()
        if not isinstance(value, bool) and key not in PREFLIGHT_CONTEXT_KEYS
    )
    if unlisted_context:
        raise RuntimeError(
            "preflight recorded non-boolean keys that are not declared as "
            f"context: {unlisted_context}"
        )
    checks["passed"] = all(
        value is True
        for key, value in checks.items()
        if key not in PREFLIGHT_CONTEXT_KEYS
    )
    checks["success"] = checks["passed"]
    atomic_json(out / "preflight.json", checks)
    return checks


async def run_no_api_smoke(
    system,
    out: Path = DEFAULT_OUT,
    limit: int = 2,
    *,
    models: Mapping[str, str] | None = None,
    prices: Mapping[str, Any] | None = None,
    price_table_path: str | Path | None = None,
    price_table_bytes: bytes | None = None,
    provider_config: Mapping[str, Any] | None = None,
    provider_config_path: str | Path | None = None,
    provider_config_bytes: bytes | None = None,
    evaluation_hop: int = 1,
    task_type: str = "Cas",
    with_stale_notices: bool = True,
    data: str | Path = DEFAULT_DATA,
):
    if limit != 2:
        raise ValueError("formal no-API smoke gate requires exactly two cases")
    corpus = FrozenV3()
    models = models or {"all": "offline-stub"}
    prices = prices or {
        "offline-stub": {"input_per_million": 0, "output_per_million": 0}
    }
    identity = build_run_identity(
        corpus,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        with_stale_notices=with_stale_notices,
        models=models,
        prices=prices,
        provider_config=provider_config or {},
        data=data,
        price_table_path=price_table_path,
        price_table_bytes=price_table_bytes,
        provider_config_path=provider_config_path,
        provider_config_bytes=provider_config_bytes,
    )
    config_hash = str(identity["run_config_hash"])
    checks = await preflight(
        system,
        corpus,
        out,
        models,
        prices=prices,
        provider_config=provider_config,
        identity=identity,
        command_mode="no-api",
        evaluation_hop=evaluation_hop,
        data=data,
    )
    if not checks["passed"]:
        raise RuntimeError(f"preflight failed: {checks}")
    completed = []
    expected_ids = evaluation_episode_ids(
        corpus,
        evaluation_hop=evaluation_hop,
        task_type=identity_task_type(identity),
        data=data,
    )
    for episode_id in expected_ids[:limit]:
        checkpoint = Checkpoint(
            checkpoint_path(out, "no-api", episode_id, identity_task_type(identity)),
            config_hash,
            "no-api",
        )
        if not checkpoint.claim():
            completed.append(episode_id)
            continue
        try:
            stop = asyncio.Event()
            heartbeat = asyncio.create_task(_heartbeat_checkpoint(checkpoint, stop))
            artifact = await run_smoke_case(
                system,
                corpus,
                episode_id,
                out,
                identity=identity,
                evaluation_hop=evaluation_hop,
                data=data,
            )
            verify_run_identity(identity, corpus=corpus, data=data)
            checkpoint.finish("success", artifact_sha256=sha256_file(artifact))
            completed.append(episode_id)
        except Exception as exc:
            checkpoint.finish("failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if "stop" in locals():
                stop.set()
                await heartbeat
    artifacts = {
        ep: json.loads((out / "cases" / ep / "artifact.json").read_text())
        for ep in completed
    }
    real_change_verified = all(
        row["production_retrieval_change_verified"].get("integrity_complete") is True
        for row in artifacts.values()
    )
    if not real_change_verified:
        raise RuntimeError(
            "no-API smoke did not change ON/OFF service context and answer prompt"
        )
    corpus.verify_identity(data)
    result = {
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "artifact_mode": "no-api",
        "evaluation_hop": evaluation_hop,
        "no_api": True,
        "success": len(completed) == 2,
        "synthetic_stub": True,
        "case_count": len(completed),
        "real_meme_change_verified": real_change_verified,
        "production_retrieval_change_verified": real_change_verified,
        "run_config_hash": identity["run_config_hash"],
        "input_hashes": identity["input_hashes"],
        "run_config": identity["config"],
        "checkpoint_namespace": "no-api",
        "completed": completed,
        "all_trace_sections_nonempty": all(
            all(
                json.loads((out / "cases" / ep / "artifact.json").read_text()).get(s)
                for s in TRACE_SECTIONS
            )
            for ep in completed
        ),
    }
    atomic_json(out / "smoke_result.json", result)
    return result


def _complete_paid_usage(
    usage: Mapping[str, Any],
    baseline: Mapping[str, tuple[Any, ...]],
) -> dict[str, Any]:
    completed = {str(bucket): dict(row) for bucket, row in usage.items()}
    for bucket in EXPECTED_PAID_USAGE_BUCKETS:
        if bucket in completed:
            continue
        if bucket not in baseline:
            raise RuntimeError(f"paid system lacks required cost bucket: {bucket}")
        completed[bucket] = {
            "model": baseline[bucket][0],
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    if set(completed) != EXPECTED_PAID_USAGE_BUCKETS:
        raise RuntimeError("paid usage contains an unexpected cost bucket")
    return completed


def _priced_cost(system, usage: Mapping[str, Any], prices: Mapping[str, Any]):
    attrs = {
        "inference_llm": "answer_chat",
        "judge_llm": "judge_chat",
        "oracle_llm": "oracle_chat",
        "p2_cheap_llm": "p2_cheap_chat",
    }
    layers: dict[str, Any] = {}
    complete = True
    total = 0.0
    for bucket, row in usage.items():
        model = str(row["model"])
        price = prices.get(model)
        client = getattr(system, attrs.get(bucket, ""), None)
        zero_usage = (
            int(row["calls"]) == 0
            and int(row["prompt_tokens"]) == 0
            and int(row["completion_tokens"]) == 0
        )
        real = bool(
            row.get(
                "tokens_are_real",
                zero_usage or bool(getattr(client, "tokens_are_real", False)),
            )
        )
        retry_unknown = bool(row.get("retry_usage_unknown"))
        if not isinstance(price, Mapping) or not real or retry_unknown:
            complete = False
            known = (
                (
                    int(row["prompt_tokens"]) * float(price["input_per_million"])
                    + int(row["completion_tokens"]) * float(price["output_per_million"])
                )
                / 1_000_000
                if isinstance(price, Mapping) and real
                else 0.0
            )
        else:
            known = (
                int(row["prompt_tokens"]) * float(price["input_per_million"])
                + int(row["completion_tokens"]) * float(price["output_per_million"])
            ) / 1_000_000
        total += known
        layers[bucket] = {
            **row,
            "tokens_are_real": real,
            "estimated_calls": int(getattr(client, "_estimated_calls", 0)),
            "retry_attempts": int(row.get("retry_attempts") or 0),
            "retry_usage_unknown": retry_unknown,
            "retry_spend_usd": 0.0 if not retry_unknown else None,
            "price_snapshot": price,
            "known_usd": known,
        }
    return {
        "layers": layers,
        "cost_complete": complete,
        "known_usd": total,
        "strict_usd_interval": [total, total] if complete else [total, None],
    }


def _retry_snap(system) -> dict[str, tuple[int, bool]]:
    attrs = {
        "inference_llm": "answer_chat",
        "judge_llm": "judge_chat",
        "oracle_llm": "oracle_chat",
        "p2_cheap_llm": "p2_cheap_chat",
    }
    return {
        bucket: (
            int(getattr(getattr(system, attr, None), "retry_attempts", 0)),
            bool(getattr(getattr(system, attr, None), "retry_usage_unknown", False)),
        )
        for bucket, attr in attrs.items()
    }


def _paid_usage_delta(
    system,
    token_before: Mapping[str, tuple[Any, ...]],
    retry_before: Mapping[str, tuple[int, bool]],
) -> dict[str, Any]:
    usage = _complete_paid_usage(
        _token_delta(token_before, _token_snap(system)),
        token_before,
    )
    retry_after = _retry_snap(system)
    attrs = {
        "inference_llm": "answer_chat",
        "judge_llm": "judge_chat",
        "oracle_llm": "oracle_chat",
        "p2_cheap_llm": "p2_cheap_chat",
    }
    for bucket, row in usage.items():
        before_count, before_unknown = retry_before.get(bucket, (0, False))
        after_count, after_unknown = retry_after.get(bucket, (0, False))
        client = getattr(system, attrs[bucket], None)
        zero_usage = (
            int(row["calls"]) == 0
            and int(row["prompt_tokens"]) == 0
            and int(row["completion_tokens"]) == 0
        )
        row["tokens_are_real"] = zero_usage or bool(
            getattr(client, "tokens_are_real", False)
        )
        row["retry_attempts"] = max(0, after_count - before_count)
        row["retry_usage_unknown"] = after_unknown and not before_unknown
    return usage


def _external_call_id(
    *,
    episode_id: str,
    attempt_token: str,
    stage: str,
    ordinal: int,
) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "episode_id": episode_id,
                "attempt_token": attempt_token,
                "stage": stage,
                "ordinal": ordinal,
            }
        ).encode()
    )


def _build_external_call(
    *,
    call_id: str,
    kind: str,
    stage: str,
    model: str,
    provider: str,
    prompt: str,
    response: str,
    usage: Mapping[str, Any],
    prices: Mapping[str, Any],
) -> dict[str, Any]:
    contract = EXTERNAL_CALL_CONTRACT[kind]
    price = prices.get(model)
    if not isinstance(price, Mapping):
        raise RuntimeError(f"missing frozen price for {model}")
    usd = (
        int(usage["prompt_tokens"]) * float(price["input_per_million"])
        + int(usage["completion_tokens"]) * float(price["output_per_million"])
    ) / 1_000_000
    return {
        "call_id": call_id,
        "kind": kind,
        "stage": stage,
        "usage_bucket": contract["bucket"],
        "model": model,
        "provider": provider,
        "calls": int(usage["calls"]),
        "prompt_tokens": int(usage["prompt_tokens"]),
        "completion_tokens": int(usage["completion_tokens"]),
        "tokens_are_real": usage.get("tokens_are_real") is True,
        "estimated": False,
        "retry_attempt": int(usage.get("retry_attempts") or 0),
        "retry_usage_unknown": bool(usage.get("retry_usage_unknown")),
        "request_sha256": sha256_bytes(prompt.encode()),
        "request_bytes": len(prompt.encode()),
        "response_sha256": sha256_bytes(response.encode()),
        "response_bytes": len(response.encode()),
        "raw_output_present": bool(response.strip()),
        "price_version": sha256_bytes(canonical_json(prices).encode()),
        "price_snapshot": dict(price),
        "usd": usd,
    }


def _execution_cost_from_calls(
    calls: Sequence[Mapping[str, Any]],
    *,
    models: Mapping[str, str],
    prices: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    layers: dict[str, Any] = {}
    stage_summary: dict[str, Any] = {}
    for kind, contract in EXTERNAL_CALL_CONTRACT.items():
        bucket = str(contract["bucket"])
        model = str(models[contract["model_key"]])
        bucket_calls = [call for call in calls if call["usage_bucket"] == bucket]
        retry_usage_unknown = any(
            bool(call.get("retry_usage_unknown")) for call in bucket_calls
        )
        known_usd = sum(float(call["usd"]) for call in bucket_calls)
        layers[bucket] = {
            "model": model,
            "calls": sum(int(call["calls"]) for call in bucket_calls),
            "prompt_tokens": sum(int(call["prompt_tokens"]) for call in bucket_calls),
            "completion_tokens": sum(
                int(call["completion_tokens"]) for call in bucket_calls
            ),
            "tokens_are_real": all(
                call.get("tokens_are_real") is True for call in bucket_calls
            ),
            "estimated_calls": 0,
            "retry_attempts": sum(
                int(call.get("retry_attempt") or 0) for call in bucket_calls
            ),
            "retry_usage_unknown": retry_usage_unknown,
            "retry_spend_usd": None if retry_usage_unknown else 0.0,
            "price_snapshot": prices.get(model),
            "known_usd": known_usd,
        }
    for call in calls:
        stage = str(call["stage"])
        row = stage_summary.setdefault(
            stage,
            {
                "usage_bucket": call["usage_bucket"],
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "known_usd": 0.0,
                "call_ids": [],
            },
        )
        for field in ("calls", "prompt_tokens", "completion_tokens"):
            row[field] += int(call[field])
        row["known_usd"] += float(call["usd"])
        row["call_ids"].append(call["call_id"])
    total = sum(float(row["known_usd"]) for row in layers.values())
    complete = not any(row["retry_usage_unknown"] for row in layers.values())
    return (
        {
            "layers": layers,
            "cost_complete": complete,
            "known_usd": total,
            "strict_usd_interval": [total, total if complete else None],
        },
        stage_summary,
    )


def write_input_invalid_paid_case(
    *,
    corpus: FrozenV3,
    episode_id: str,
    out: Path,
    identity: Mapping[str, Any],
    artifact_namespace: str,
    prices: Mapping[str, Any],
    error: str,
    attempt_token: str,
    evaluation_hop: int = 1,
) -> Path:
    """Close a per-case invalid input without making any external call."""
    execution_cost, stage_summary = _execution_cost_from_calls(
        [],
        models=identity["config"]["models"],
        prices=prices,
    )
    frozen_known = float(corpus.episode_cost(episode_id)["new"]["deployment"])
    frozen_complete = (
        int(corpus.episode_cost(episode_id)["new"]["cost_incomplete_attempts"]) == 0
    )
    frozen_cost = {
        "cost_complete": frozen_complete,
        "known_usd": frozen_known,
        "strict_usd_interval": [
            frozen_known,
            frozen_known if frozen_complete else None,
        ],
    }
    cost = {
        "frozen_v3": frozen_cost,
        "paid_execution": execution_cost,
        "failed_attempts": [],
        "retry_spend_usd": 0.0,
        "cost_complete": frozen_complete,
        "known_usd": frozen_known,
        "strict_usd_interval": [
            frozen_known,
            frozen_known if frozen_complete else None,
        ],
        "calls": [],
        "call_ledger_sha256": sha256_bytes(canonical_json([]).encode()),
        "stage_summary": stage_summary,
        "episode_summary": {
            "total_usd": 0.0,
            "call_count": 0,
            "call_ids": [],
            "cost_complete": True,
        },
    }
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "artifact_mode": artifact_namespace,
        "episode_id": episode_id,
        "evaluation_hop": evaluation_hop,
        "synthetic_stub": False,
        "execution_complete": True,
        "case_success": False,
        "runtime_status": "input-invalid",
        "root_binding_status": "not-attempted",
        "p1_path_status": "not-evaluated",
        "frontier_status": "not-executed",
        "old_visibility_off": "not-evaluated",
        "old_visibility_on": "not-evaluated",
        "replacement_visibility_on": "not-evaluated",
        "earliest_failure_layer": "input",
        "attempt_token": attempt_token,
        "development_reevaluation": True,
        "held_out_data_independent": False,
        "authorization": {
            "run_config": identity["config"],
            "run_config_hash": identity["run_config_hash"],
            "run_config_hash_recomputed": identity["run_config_hash"],
            "input_hashes": identity["input_hashes"],
            "secrets_persisted": False,
        },
        "extraction": {"status": "not-executed", "reason": error},
        "p1_graph": {"status": "not-executed", "reason": error},
        "p2_queue": {"status": "not-executed", "events": [], "unfinished": []},
        "p2_edges": {"status": "not-executed", "executed": []},
        "state_evidence": {"status": "not-executed"},
        "retrieval": {"status": "not-executed"},
        "answers": {"status": "not-executed"},
        "judge": {
            "status": "not-executed",
            "calls": [],
            "gold_entered_runtime": False,
            "runtime_feedback_applied": False,
        },
        "cost": cost,
        "cleanup": {"complete": True, "leftover_events": 0, "dead_letter_events": 0},
    }
    final_dir = (
        out
        / "artifacts"
        / artifact_namespace
        / f"{episode_id}-input-invalid"
    )
    validate_paid_cost(cost, artifact=artifact, config=identity["config"])
    write_case_artifact(final_dir, artifact)
    return final_dir / "artifact.json"


async def run_one_paid_case(
    system,
    *,
    corpus: FrozenV3 | None = None,
    episode_id: str,
    out: Path,
    prices: Mapping[str, Any],
    identity: Mapping[str, Any],
    artifact_namespace: str,
    evaluation_hop: int = 1,
    data: str = DEFAULT_DATA,
    prior_attempts: Sequence[Mapping[str, Any]] = (),
    attempt_token: str,
) -> Path:
    """Run exactly one paid answer/judge case after the no-API smoke is green."""

    corpus = corpus or FrozenV3()
    task_type = identity_task_type(identity)
    with_stale_notices = identity_with_stale_notices(identity)
    expected_ids = evaluation_episode_ids(
        corpus, evaluation_hop=evaluation_hop, task_type=task_type, data=data
    )
    if episode_id not in expected_ids:
        raise ValueError(
            f"{episode_id} is not in canonical hop{evaluation_hop} evaluation view"
        )
    try:
        runtime_input = resolve_runtime_episode_input(
            corpus,
            episode_id,
            evaluation_hop=evaluation_hop,
            task_type=task_type,
            data=data,
        )
    except ValueError as exc:
        return write_input_invalid_paid_case(
            corpus=corpus,
            episode_id=episode_id,
            out=out,
            identity=identity,
            artifact_namespace=artifact_namespace,
            prices=prices,
            error=f"{type(exc).__name__}: {exc}",
            attempt_token=attempt_token,
            evaluation_hop=evaluation_hop,
        )
    if system.judge_chat is None:
        raise ValueError("paid case requires an explicit judge model")
    # Frozen nodes do not carry provider embeddings.  Force production keyword
    # retrieval so this one-case smoke makes no hidden embedding API calls.
    system.retrieval._embedding = NoOpEmbeddingClient()
    token_before = _token_snap(system)
    retry_before = _retry_snap(system)
    questions = {
        "before": runtime_input.before_question,
        "off": runtime_input.after_question,
        "on": runtime_input.after_question,
    }

    async def stage(stage_name: str, account: str) -> dict[str, Any]:
        question = questions[stage_name]
        request = SearchRequest(
            query=question,
            top_k=8,
            level=ContextLevel.L2,
            include_stale=False,
            # Requested in every arm, so the ON/OFF difference stays "was the
            # node staled" and never "did the prompt template change". In OFF
            # nothing is stale, so no notices come back and the frozen prompt is
            # used -- the same outcome as before, now for a stated reason.
            include_stale_notices=with_stale_notices,
            context_type=[ContextType.MEMORY],
            scope=[Scope.AGENT],
        )
        async with system.repo.session(account) as db:
            response = await system.retrieval.search(
                db,
                request,
                RequestContext(account_id=account, agent_id=EVAL_AGENT),
            )
        notes = [
            result.l1_content or result.l0_content or result.l2_content or ""
            for result in response.results
        ]
        prompt = build_answer_prompt(
            "\n".join(f"- {note}" for note in notes if note) or "(no notes found)",
            question,
            response.stale_notices if with_stale_notices else (),
        )
        return {
            "prompt": prompt,
            "prompt_sha256": sha256_bytes(prompt.encode()),
            "raw_answer": None,
            "usage": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "tokens_are_real": False,
            },
            "retrieval": {
                "retrieval_id": response.retrieval_id,
                "request": request.model_dump(mode="json"),
                "candidates": response.trace.get("candidates", []),
                "final": [
                    result.model_dump(mode="json") for result in response.results
                ],
                "final_materialized": response.trace.get("final_materialized", []),
                "context_hash": response.trace.get("context_hash"),
                # What the model was told about withheld nodes, recorded so the
                # answer can be audited against its actual input.
                "stale_notices": [
                    notice.model_dump(mode="json")
                    for notice in response.stale_notices
                ],
            },
        }

    staging = out / ".staging" / episode_id
    staged_path = await run_smoke_case(
        system,
        corpus,
        episode_id,
        staging,
        stage_callback=stage,
        identity=identity,
        artifact_mode=artifact_namespace,
        evaluation_hop=evaluation_hop,
        data=data,
    )
    artifact = json.loads(staged_path.read_text(encoding="utf-8"))
    # The OFF/ON production-retrieval relationship has passed before the first
    # paid answer or judge call.  Only now execute answer generation.
    verify_run_identity(identity, corpus=corpus, data=data)
    provider = str(identity["config"]["provider_bindings"]["provider"])
    answer_model = str(identity["config"]["models"]["chat_model"])
    for stage_name in ("before", "off", "on"):
        answer_row = artifact["answers"][stage_name]
        before = (
            system.answer_chat.call_count,
            system.answer_chat.prompt_tokens,
            system.answer_chat.completion_tokens,
            system.answer_chat.retry_attempts,
            system.answer_chat.retry_usage_unknown,
        )
        answer_row["raw_answer"] = await system.answer_chat.complete(
            answer_row["prompt"], max_tokens=50
        )
        answer_row["usage"] = {
            "calls": system.answer_chat.call_count - before[0],
            "prompt_tokens": system.answer_chat.prompt_tokens - before[1],
            "completion_tokens": system.answer_chat.completion_tokens - before[2],
            "tokens_are_real": system.answer_chat.tokens_are_real,
            "retry_attempts": system.answer_chat.retry_attempts - before[3],
            "retry_usage_unknown": (
                system.answer_chat.retry_usage_unknown and not before[4]
            ),
        }
        call_stage = f"{stage_name}.answer"
        answer_row["call_id"] = _external_call_id(
            episode_id=episode_id,
            attempt_token=attempt_token,
            stage=call_stage,
            ordinal=0,
        )
        answer_row["model"] = answer_model
        answer_row["provider"] = provider
    annotations = load_evaluation_annotations(
        corpus,
        episode_id,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    evaluation = evaluate_runtime_trace(
        corpus,
        episode_id,
        artifact,
        annotations=annotations,
        evaluation_hop=evaluation_hop,
        task_type=task_type,
        data=data,
    )
    artifact["post_hoc_evaluation"] = evaluation
    artifact["retrieval_evidence_contract"] = evaluation[
        "retrieval_evidence_contract"
    ]
    for field in (
        "p1_path_status",
        "frontier_status",
        "old_visibility_off",
        "old_visibility_on",
        "replacement_visibility_on",
        "earliest_failure_layer",
    ):
        artifact[field] = evaluation[field]
    gold = {
        "before": annotations.before_reference,
        "off": annotations.after_reference,
        "on": annotations.after_reference,
    }
    judge_records = []
    for stage_name in ("before", "off", "on"):
        answer = artifact["answers"][stage_name]["raw_answer"]
        reference = gold[stage_name]
        if reference is None:
            continue
        prompt = _JUDGE_PROMPT.format(
            question=questions[stage_name],
            gold=reference,
            answer=answer,
        )
        before = (
            system.judge_chat.call_count,
            system.judge_chat.prompt_tokens,
            system.judge_chat.completion_tokens,
            system.judge_chat.retry_attempts,
            system.judge_chat.retry_usage_unknown,
        )
        raw = await system.judge_chat.complete(prompt, max_tokens=4)
        parsed_verdict, fallback = parse_paid_judge_output(raw)
        usage_row = {
            "calls": system.judge_chat.call_count - before[0],
            "prompt_tokens": system.judge_chat.prompt_tokens - before[1],
            "completion_tokens": system.judge_chat.completion_tokens - before[2],
            "tokens_are_real": system.judge_chat.tokens_are_real,
            "retry_attempts": system.judge_chat.retry_attempts - before[3],
            "retry_usage_unknown": (
                system.judge_chat.retry_usage_unknown and not before[4]
            ),
        }
        call_stage = f"{stage_name}.judge"
        judge_records.append(
            {
                "call_id": _external_call_id(
                    episode_id=episode_id,
                    attempt_token=attempt_token,
                    stage=call_stage,
                    ordinal=0,
                ),
                "stage": stage_name,
                "question": questions[stage_name],
                "gold": reference,
                "answer": answer,
                "prompt": prompt,
                "raw_output": raw,
                "parsed_verdict": parsed_verdict,
                "fallback": fallback,
                "usage": usage_row,
                "model": str(identity["config"]["models"]["judge_model"]),
                "provider": provider,
            }
        )
    artifact["judge"] = {
        "calls": judge_records,
        "gold_entered_runtime": False,
        "scoring_started_after_runtime_cleanup": True,
        "runtime_feedback_applied": False,
    }
    if task_type == "Abs":
        artifact["judge"]["abs_scoring"] = score_abs_stages(
            {name: artifact["answers"][name]["raw_answer"] for name in gold},
            gold,
            judge_records,
        )
    incorrect_stages = [
        row["stage"]
        for row in judge_records
        if row["parsed_verdict"] != "correct"
    ]
    artifact["post_hoc_evaluation"]["judge_incorrect_stages"] = incorrect_stages
    if incorrect_stages and artifact.get("earliest_failure_layer") is None:
        artifact["earliest_failure_layer"] = "answer-or-judge"
        artifact["post_hoc_evaluation"][
            "earliest_failure_layer"
        ] = "answer-or-judge"
    usage = _paid_usage_delta(system, token_before, retry_before)
    frozen_cost = artifact["cost"]
    retry_costs = [
        dict(attempt["cost"])
        for attempt in prior_attempts
        if attempt.get("status") == "failed"
    ]
    retry_known = sum(float(item["known_usd"]) for item in retry_costs)
    call_costs: list[dict[str, Any]] = []
    for stage_name, answer_row in artifact["answers"].items():
        usage_row = answer_row["usage"]
        call_costs.append(
            _build_external_call(
                call_id=answer_row["call_id"],
                kind="answer",
                stage=f"{stage_name}.answer",
                model=answer_row["model"],
                provider=answer_row["provider"],
                prompt=answer_row["prompt"],
                response=answer_row["raw_answer"],
                usage=usage_row,
                prices=prices,
            )
        )
    for judge_row in judge_records:
        call_costs.append(
            _build_external_call(
                call_id=judge_row["call_id"],
                kind="judge",
                stage=f"{judge_row['stage']}.judge",
                model=judge_row["model"],
                provider=judge_row["provider"],
                prompt=judge_row["prompt"],
                response=judge_row["raw_output"],
                usage=judge_row["usage"],
                prices=prices,
            )
        )
    for decision in artifact["p2_edges"].get("decision_trace") or []:
        for tier in ("cheap", "strong"):
            if decision.get(f"{tier}_calls"):
                raise RuntimeError(
                    "paid runner unexpectedly observed P2 external calls before "
                    "call-level trace binding"
                )
    execution_cost, stage_summary = _execution_cost_from_calls(
        call_costs,
        models=identity["config"]["models"],
        prices=prices,
    )
    observed_execution = _priced_cost(system, usage, prices)
    for bucket in EXPECTED_PAID_USAGE_BUCKETS:
        for field in ("model", "calls", "prompt_tokens", "completion_tokens"):
            if (
                execution_cost["layers"][bucket][field]
                != observed_execution["layers"][bucket][field]
            ):
                raise RuntimeError(
                    f"raw client counters disagree with call ledger: {bucket}.{field}"
                )
    known_total = (
        float(frozen_cost["known_usd"])
        + float(execution_cost["known_usd"])
        + retry_known
    )
    complete = bool(
        frozen_cost["cost_complete"]
        and execution_cost["cost_complete"]
        and all(item["cost_complete"] for item in retry_costs)
    )
    artifact["cost"] = {
        "frozen_v3": frozen_cost,
        "paid_execution": execution_cost,
        "failed_attempts": retry_costs,
        "retry_spend_usd": retry_known,
        "cost_complete": complete,
        "known_usd": known_total,
        "strict_usd_interval": [known_total, known_total if complete else None],
        "calls": call_costs,
        "call_ledger_sha256": sha256_bytes(canonical_json(call_costs).encode()),
        "stage_summary": stage_summary,
        "episode_summary": {
            "total_usd": sum(float(row["usd"]) for row in call_costs),
            "call_count": len(call_costs),
            "call_ids": [row["call_id"] for row in call_costs],
            "cost_complete": execution_cost["cost_complete"],
        },
    }
    artifact["paid_case_smoke"] = True
    artifact["case_success"] = True
    artifact["synthetic_stub"] = False
    artifact["attempt_token"] = attempt_token
    artifact["cleanup"] = {
        "complete": True,
        "leftover_events": 0,
        "dead_letter_events": 0,
    }
    if artifact_namespace not in {"paid-smoke", "full-run"}:
        raise ValueError(f"unknown paid artifact namespace: {artifact_namespace}")
    target = annotations.target_entity or "target"
    # Abs and Cas happen never to score the same entity in one episode, so the
    # bare target hash has not collided -- but that is luck, not design. Name the
    # task type so the path stops depending on it.
    task_type = str(identity["config"]["task_type"])
    final_dir = (
        out
        / "artifacts"
        / artifact_namespace
        / f"{episode_id}-{task_type}-{sha256_bytes(target.encode())[:12]}"
    )
    validate_paid_cost(
        artifact["cost"],
        artifact=artifact,
        config=identity["config"],
    )
    verify_run_identity(identity, corpus=corpus, data=data)
    write_case_artifact(final_dir, artifact)
    if staging.exists():
        shutil.rmtree(staging)
    return final_dir / "artifact.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "smoke", "paid-case", "run"])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--episode-id")
    parser.add_argument("--hop", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--task-type",
        choices=SUPPORTED_TASK_TYPES,
        default="Cas",
        help="MEME task type to score (Cas asks for the new value, Abs for abstention)",
    )
    parser.add_argument(
        "--no-stale-notices",
        dest="with_stale_notices",
        action="store_false",
        help=(
            "Withhold stale nodes without telling the model why. Off-arm for "
            "measuring what the explanation itself contributes."
        ),
    )
    parser.set_defaults(with_stale_notices=True)
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--chat-model")
    parser.add_argument("--judge-model")
    parser.add_argument("--p2-cheap-model")
    parser.add_argument("--p2-strong-model")
    parser.add_argument("--extract-model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--provider")
    parser.add_argument("--embedding-provider")
    parser.add_argument("--price-table", type=Path)
    parser.add_argument("--providers-path", type=Path, default=DEFAULT_PROVIDERS_PATH)
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    if args.command == "run" and args.limit is not None:
        raise ValueError(
            "formal run is fixed at all 100 episodes for hop1 or the complete "
            "canonical hop2 view; --limit is forbidden"
        )
    provider_bytes: bytes | None = None
    providers_document: dict[str, Any] | None = None
    if args.command in {"preflight", "smoke", "paid-case", "run"}:
        required = (
            "chat_model",
            "judge_model",
            "p2_cheap_model",
            "p2_strong_model",
            "extract_model",
            "embedding_model",
            "provider",
            "embedding_provider",
            "price_table",
        )
        if args.command == "paid-case":
            required = ("episode_id", *required)
        missing = [name for name in required if not getattr(args, name)]
        if missing:
            raise ValueError(
                f"{args.command} requires explicit flags: "
                + ", ".join("--" + name.replace("_", "-") for name in missing)
            )
        provider_bytes = args.providers_path.read_bytes()
        providers_document, _ = provider_snapshot_from_bytes(
            provider_bytes,
            labels=(args.provider, args.embedding_provider),
        )
        if args.command in {"paid-case", "run"}:
            from integrations.memebench.systems import build_system

            system = await build_system(
                chat_model=args.chat_model,
                oracle_model=args.p2_strong_model,
                extract_model=args.extract_model,
                judge_model=args.judge_model,
                provider_label=args.provider,
                embedding_provider_label=args.embedding_provider,
                embedding_model=args.embedding_model,
                cascade=False,
                p2_cascade=True,
                p2_cheap_model=args.p2_cheap_model,
                providers_document=providers_document,
            )
        else:
            import asyncpg

            from contexthub.config import Settings
            from contexthub.db.codecs import init_pg_connection
            from contexthub.db.repository import PgRepository
            from types import SimpleNamespace

            pool = await asyncpg.create_pool(
                Settings().asyncpg_database_url,
                min_size=1,
                max_size=5,
                init=init_pg_connection,
            )
            system = SimpleNamespace(
                pool=pool, repo=PgRepository(pool), rule_registry=None
            )
    else:
        import asyncpg

        from contexthub.config import Settings
        from contexthub.db.codecs import init_pg_connection
        from contexthub.db.repository import PgRepository
        from types import SimpleNamespace

        pool = await asyncpg.create_pool(
            Settings().asyncpg_database_url,
            min_size=1,
            max_size=5,
            init=init_pg_connection,
        )
        system = SimpleNamespace(pool=pool, repo=PgRepository(pool), rule_registry=None)
    try:
        if args.command == "preflight":
            price_bytes = args.price_table.read_bytes()
            prices = json.loads(price_bytes)
            corpus = FrozenV3()
            models = {
                name: getattr(args, name)
                for name in (
                    "chat_model",
                    "judge_model",
                    "p2_cheap_model",
                    "p2_strong_model",
                    "extract_model",
                    "embedding_model",
                )
            }
            identity = build_run_identity(
                corpus,
                evaluation_hop=args.hop,
                task_type=args.task_type,
                with_stale_notices=args.with_stale_notices,
                models=models,
                prices=prices,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                data=args.data,
                price_table_path=args.price_table,
                price_table_bytes=price_bytes,
                provider_config_path=args.providers_path,
                provider_config_bytes=provider_bytes,
            )
            await preflight(
                system,
                corpus,
                args.out,
                models,
                prices=prices,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                identity=identity,
                evaluation_hop=args.hop,
                data=args.data,
            )
        elif args.command == "smoke":
            price_bytes = args.price_table.read_bytes()
            prices = json.loads(price_bytes)
            models = {
                name: getattr(args, name)
                for name in (
                    "chat_model",
                    "judge_model",
                    "p2_cheap_model",
                    "p2_strong_model",
                    "extract_model",
                    "embedding_model",
                )
            }
            await run_no_api_smoke(
                system,
                args.out,
                args.limit or 2,
                models=models,
                prices=prices,
                price_table_path=args.price_table,
                price_table_bytes=price_bytes,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                provider_config_path=args.providers_path,
                provider_config_bytes=provider_bytes,
                evaluation_hop=args.hop,
                task_type=args.task_type,
                with_stale_notices=args.with_stale_notices,
                data=args.data,
            )
        elif args.command == "paid-case":
            price_bytes = args.price_table.read_bytes()
            prices = json.loads(price_bytes)
            corpus = FrozenV3()
            models = {
                name: getattr(args, name)
                for name in (
                    "chat_model",
                    "judge_model",
                    "p2_cheap_model",
                    "p2_strong_model",
                    "extract_model",
                    "embedding_model",
                )
            }
            identity = build_run_identity(
                corpus,
                evaluation_hop=args.hop,
                task_type=args.task_type,
                with_stale_notices=args.with_stale_notices,
                models=models,
                prices=prices,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                data=args.data,
                price_table_path=args.price_table,
                price_table_bytes=price_bytes,
                provider_config_path=args.providers_path,
                provider_config_bytes=provider_bytes,
            )
            checks = await preflight(
                system,
                corpus,
                args.out,
                models,
                prices=prices,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                identity=identity,
                command_mode="paid-smoke",
                evaluation_hop=args.hop,
                data=args.data,
            )
            if not checks["passed"]:
                raise RuntimeError(f"preflight failed: {checks}")
            require_paid_case_gate(args.out, identity)
            checkpoint = Checkpoint(
                checkpoint_path(
                    args.out,
                    "paid-smoke",
                    args.episode_id,
                    identity_task_type(identity),
                ),
                str(identity["run_config_hash"]),
                "paid-smoke",
            )
            recover_paid_execution_checkpoint(
                args.out,
                identity,
                args.episode_id,
            )
            if checkpoint.claim():
                stop = asyncio.Event()
                heartbeat = asyncio.create_task(_heartbeat_checkpoint(checkpoint, stop))
                attempt_before = _token_snap(system)
                attempt_retry_before = _retry_snap(system)
                try:
                    checkpoint_row = json.loads(
                        checkpoint.path.read_text(encoding="utf-8")
                    )
                    artifact = await run_one_paid_case(
                        system,
                        corpus=corpus,
                        episode_id=args.episode_id,
                        out=args.out,
                        prices=prices,
                        identity=identity,
                        artifact_namespace="paid-smoke",
                        evaluation_hop=args.hop,
                        data=args.data,
                        prior_attempts=checkpoint_row.get("attempts") or [],
                        attempt_token=str(checkpoint.attempt_token),
                    )
                    verify_run_identity(identity, corpus=corpus, data=args.data)
                    paid_artifact = json.loads(artifact.read_text())
                    checkpoint.finish(
                        "execution_complete",
                        artifact_sha256=sha256_file(artifact),
                        artifact_path=str(artifact),
                        usage=paid_artifact["cost"]["paid_execution"]["layers"],
                        cost=paid_artifact["cost"]["paid_execution"],
                        call_ledger_sha256=paid_artifact["cost"]["call_ledger_sha256"],
                    )
                except Exception as exc:
                    attempt_usage = _paid_usage_delta(
                        system, attempt_before, attempt_retry_before
                    )
                    checkpoint.finish(
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                        usage=attempt_usage,
                        cost=_priced_cost(system, attempt_usage, prices),
                    )
                    raise
                finally:
                    stop.set()
                    await heartbeat
            finalize_paid_case(args.out, identity, args.episode_id)
        else:
            price_bytes = args.price_table.read_bytes()
            prices = json.loads(price_bytes)
            corpus = FrozenV3()
            models = {
                name: getattr(args, name)
                for name in (
                    "chat_model",
                    "judge_model",
                    "p2_cheap_model",
                    "p2_strong_model",
                    "extract_model",
                    "embedding_model",
                )
            }
            identity = build_run_identity(
                corpus,
                evaluation_hop=args.hop,
                task_type=args.task_type,
                with_stale_notices=args.with_stale_notices,
                models=models,
                prices=prices,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                data=args.data,
                price_table_path=args.price_table,
                price_table_bytes=price_bytes,
                provider_config_path=args.providers_path,
                provider_config_bytes=provider_bytes,
            )
            checks = await preflight(
                system,
                corpus,
                args.out,
                models,
                prices=prices,
                provider_config={
                    "provider": args.provider,
                    "embedding_provider": args.embedding_provider,
                },
                identity=identity,
                command_mode="full-run",
                evaluation_hop=args.hop,
                data=args.data,
            )
            if not checks["passed"]:
                raise RuntimeError(f"preflight failed: {checks}")
            require_full_run_gate(args.out, identity)
            config_hash = str(identity["run_config_hash"])
            expected_episode_ids = evaluation_episode_ids(
                corpus,
                evaluation_hop=args.hop,
                task_type=identity_task_type(identity),
                data=args.data,
            )
            for episode_id in expected_episode_ids:
                checkpoint = Checkpoint(
                    checkpoint_path(
                        args.out,
                        "full-run",
                        episode_id,
                        identity_task_type(identity),
                    ),
                    config_hash,
                    "full-run",
                )
                if not checkpoint.claim():
                    continue
                attempt_before = _token_snap(system)
                attempt_retry_before = _retry_snap(system)
                stop = asyncio.Event()
                heartbeat = asyncio.create_task(_heartbeat_checkpoint(checkpoint, stop))
                try:
                    checkpoint_row = json.loads(
                        checkpoint.path.read_text(encoding="utf-8")
                    )
                    artifact = await run_one_paid_case(
                        system,
                        corpus=corpus,
                        episode_id=episode_id,
                        out=args.out,
                        prices=prices,
                        identity=identity,
                        artifact_namespace="full-run",
                        evaluation_hop=args.hop,
                        data=args.data,
                        prior_attempts=checkpoint_row.get("attempts") or [],
                        attempt_token=str(checkpoint.attempt_token),
                    )
                    verify_run_identity(identity, corpus=corpus, data=args.data)
                    paid_artifact = json.loads(artifact.read_text())
                    checkpoint.finish(
                        "success",
                        artifact_sha256=sha256_file(artifact),
                        artifact_path=str(artifact),
                        usage=paid_artifact["cost"]["paid_execution"]["layers"],
                        cost=paid_artifact["cost"]["paid_execution"],
                        call_ledger_sha256=paid_artifact["cost"]["call_ledger_sha256"],
                    )
                except Exception as exc:
                    attempt_usage = _paid_usage_delta(
                        system, attempt_before, attempt_retry_before
                    )
                    checkpoint.finish(
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                        usage=attempt_usage,
                        cost=_priced_cost(system, attempt_usage, prices),
                    )
                    raise
                finally:
                    stop.set()
                    await heartbeat
            completed_rows = validate_full_run_completion(
                args.out,
                expected_episode_ids=expected_episode_ids,
                run_config_hash=config_hash,
                evaluation_hop=args.hop,
                task_type=identity_task_type(identity),
                data=args.data,
            )
            successful_costs = [row["known_usd"] for row in completed_rows]
            successful_costs.sort()

            def percentile(q: float) -> float:
                index = min(
                    len(successful_costs) - 1,
                    int(round((len(successful_costs) - 1) * q)),
                )
                return successful_costs[index]

            atomic_json(
                args.out / "run_cost_summary.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "schema_generation": SCHEMA_GENERATION,
                    "artifact_mode": "full-run-summary",
                    "evaluation_hop": args.hop,
                    "run_config_hash": identity["run_config_hash"],
                    "input_hashes": identity["input_hashes"],
                    "run_config": identity["config"],
                    "checkpoint_namespace": "full-run",
                    "completion_closed": True,
                    "episode_count": len(successful_costs),
                    "episode_artifacts": completed_rows,
                    "mean_usd": sum(successful_costs) / len(successful_costs),
                    "p50_usd": percentile(0.50),
                    "p95_usd": percentile(0.95),
                    "total_usd": sum(successful_costs),
                },
            )
    finally:
        close = getattr(system, "close", None)
        if close is not None:
            await close()
        else:
            await system.pool.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main_async(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
