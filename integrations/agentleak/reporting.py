"""Manifest, registry, and summary helpers for AgentLeak Phase 5 runs."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

REQUIRED_MANIFEST_FIELDS = (
    "run_id",
    "created_at",
    "agentleak_repo_path",
    "agentleak_commit",
    "model",
    "provider",
    "system",
    "channels",
    "scenario_subset",
    "n",
    "seed",
    "detection_mode",
    "paper_eligible",
    "paper_eligibility_reason",
    "raw_result_paths",
    "normalized_trace_path",
    "decision_log_path",
    "metrics_path",
    "online_policy_oracle",
)

PAPER_REQUIRED_FIELDS = (
    "protocol_frozen",
    "scenario_subset_fixed",
    "model_provider_probed",
    "raw_trace_available",
    "normalized_trace_available",
    "decision_log_available",
    "metrics_available",
    "detection_mode_fixed",
    "coverage_separated",
    "structured_semantic_separated",
    "no_manual_trace_edits",
)

NON_PAPER_MODES = {"mock", "dry_run", "dry-run", "debug", "smoke"}
MIN_PAPER_MAIN_CHANNELS = {"C2", "C3", "C5", "C6"}


def build_manifest(
    *,
    run_id: str,
    system: str,
    model: str = "mock-model",
    provider: str = "mock",
    channels: list[str] | tuple[str, ...] = ("C1", "C2", "C3", "C5", "C6"),
    scenario_subset: dict[str, Any] | None = None,
    n: int = 0,
    seed: int | None = None,
    detection_mode: str = "mock_labels",
    agentleak_repo_path: str | None = None,
    agentleak_commit: str | None = None,
    raw_result_paths: list[str] | None = None,
    normalized_trace_path: str | None = None,
    decision_log_path: str | None = None,
    metrics_path: str | None = None,
    mode: str = "mock",
    protocol_snapshot_path: str | None = None,
    paper_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a scrubbed manifest and evaluate paper eligibility."""

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "agentleak_repo_path": agentleak_repo_path,
        "agentleak_commit": agentleak_commit,
        "model": model,
        "provider": provider,
        "system": system,
        "channels": list(channels),
        "scenario_subset": scenario_subset or {"mode": mode, "source": "mock_fixture"},
        "n": int(n),
        "seed": seed,
        "detection_mode": detection_mode,
        "paper_eligible": False,
        "paper_eligibility_reason": "",
        "raw_result_paths": raw_result_paths or [],
        "normalized_trace_path": normalized_trace_path,
        "decision_log_path": decision_log_path,
        "metrics_path": metrics_path,
        "online_policy_oracle": False,
        "run_class": mode,
        "system_protocol": {
            "id": system,
            "uses_online_llm_policy_oracle": False,
        },
        "secrets_policy": {
            "api_keys_logged": False,
            "raw_vault_values_in_summary": False,
        },
        "mode": mode,
        "protocol_snapshot_path": protocol_snapshot_path,
        "trace_source": "mock_normalized_trace",
        "no_real_agentleak_benchmark": True,
        "paper_eligibility_inputs": paper_inputs or {},
    }
    manifest = scrub_manifest(manifest)
    eligibility = evaluate_paper_eligibility(manifest)
    manifest["paper_eligible"] = eligibility["paper_eligible"]
    manifest["paper_eligibility_reason"] = eligibility["reason"]
    return manifest


def evaluate_paper_eligibility(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return eligibility status without mutating the manifest."""

    reasons: list[str] = []
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            reasons.append(f"missing manifest field: {field}")

    mode = str(manifest.get("mode") or "").lower()
    if mode in NON_PAPER_MODES:
        reasons.append(f"mode is non-paper-eligible: {mode}")

    if manifest.get("online_policy_oracle") is not False:
        reasons.append("online_policy_oracle must be false")

    if manifest.get("no_real_agentleak_benchmark") is True:
        reasons.append("mock normalized traces are not real AgentLeak benchmark output")

    channels = {str(channel) for channel in manifest.get("channels") or []}
    missing_channels = sorted(MIN_PAPER_MAIN_CHANNELS - channels)
    if missing_channels:
        reasons.append(f"missing required main channels: {','.join(missing_channels)}")

    if len(channels) <= 1:
        reasons.append("single-channel runs cannot enter the paper table")

    inputs = manifest.get("paper_eligibility_inputs") or {}
    if not isinstance(inputs, dict):
        reasons.append("paper_eligibility_inputs must be an object")
        inputs = {}
    for field in PAPER_REQUIRED_FIELDS:
        if inputs.get(field) is not True:
            reasons.append(f"paper eligibility input is not true: {field}")

    raw_paths = manifest.get("raw_result_paths") or []
    if not raw_paths:
        reasons.append("raw_result_paths missing or empty")

    if reasons:
        return {"paper_eligible": False, "reason": "; ".join(reasons), "failures": reasons}
    return {"paper_eligible": True, "reason": "all paper eligibility checks passed", "failures": []}


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    payload = scrub_manifest(manifest)
    _write_json(Path(path), payload)


def append_registry(registry_path: str | Path, manifest: dict[str, Any]) -> None:
    """Append one manifest summary record without rewriting history."""

    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": manifest.get("run_id"),
        "created_at": manifest.get("created_at"),
        "system": manifest.get("system"),
        "model": manifest.get("model"),
        "provider": manifest.get("provider"),
        "channels": manifest.get("channels"),
        "paper_eligible": manifest.get("paper_eligible"),
        "paper_eligibility_reason": manifest.get("paper_eligibility_reason"),
        "metrics_path": manifest.get("metrics_path"),
        "manifest_path": manifest.get("manifest_path"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(scrub_manifest(record), sort_keys=True) + "\n")


def write_summary(
    path: str | Path,
    *,
    manifest: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    """Write a concise markdown summary for a non-paper mock run."""

    system_metrics = metrics.get("systems") if isinstance(metrics.get("systems"), dict) else None
    core_lines = _summary_metric_lines(metrics)
    if system_metrics:
        core_lines = []
        for system_id, payload in sorted(system_metrics.items()):
            core_lines.extend(
                [
                    f"- {system_id} exact_leakage_rate: `{payload.get('exact_leakage_rate')}`",
                    f"- {system_id} internal_leakage_rate: `{payload.get('internal_leakage_rate')}`",
                    f"- {system_id} audit_gap: `{payload.get('audit_gap')}`",
                ]
            )

    excluded = (
        ((manifest.get("channels_detail") or {}).get("excluded") or [])
        if isinstance(manifest.get("channels_detail"), dict)
        else []
    )
    excluded_lines = [
        f"- {item.get('channel')}: {item.get('reason')}"
        for item in excluded
        if isinstance(item, dict)
    ]

    lines = [
        f"# AgentLeak Phase 5 Summary: {manifest.get('run_id')}",
        "",
        "This summary was generated from fixture/local smoke traces only.",
        "",
        "## Eligibility",
        "",
        f"- paper_eligible: `{str(manifest.get('paper_eligible')).lower()}`",
        f"- reason: {manifest.get('paper_eligibility_reason')}",
        "- real AgentLeak benchmark run: `false`",
        "- online policy oracle: `false`",
        "- API key values logged: `false`",
        "",
        "## Core Metrics",
        "",
        *core_lines,
        "",
        "## Channel Exclusions",
        "",
        *(excluded_lines or ["- none recorded"]),
        "",
        "## Notes",
        "",
        "- AgentLeak detector or LLM judge output, if present in fixtures, is treated as post-hoc evidence only.",
        "- Semantic free-text residuals are diagnostics, not proof of runtime enforcement.",
        "- Fixture/local smoke results are not formal AgentLeak benchmark numbers.",
        "",
    ]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def scrub_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Drop obvious secret-like keys before writing tracked artifacts."""

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(child)
                for key, child in value.items()
                if not _secretish_key(str(key))
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return scrub(dict(manifest))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _rate_value(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("rate")
    return payload


def _summary_metric_lines(metrics: dict[str, Any]) -> list[str]:
    return [
        f"- exact_leakage_rate: `{metrics.get('exact_leakage_rate')}`",
        f"- internal_leakage_rate: `{metrics.get('internal_leakage_rate')}`",
        f"- audit_gap: `{metrics.get('audit_gap')}`",
        "- final_output_safe_but_internal_leaked_rate: "
        f"`{metrics.get('final_output_safe_but_internal_leaked_rate')}`",
        "- structured_mediated_leakage_rate: "
        f"`{_rate_value(metrics.get('structured_mediated_leakage_rate'))}`",
        "- semantic_free_text_residual_rate: "
        f"`{_rate_value(metrics.get('semantic_free_text_residual_rate'))}`",
    ]


def _secretish_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in {"secrets_policy", "api_keys_logged", "raw_vault_values_in_summary"}:
        return False
    return any(marker in lowered for marker in ("api_key", "apikey", "secret", "token", "password"))

