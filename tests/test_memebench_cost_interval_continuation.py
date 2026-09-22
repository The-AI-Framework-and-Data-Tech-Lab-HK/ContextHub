from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from integrations.memebench.cost_interval import (
    aggregate_intervals,
    bounded_unknown_completion,
    exact_record,
    interval_record,
)
import integrations.memebench.full100_continuation as continuation
from integrations.memebench.full100_continuation import (
    build_completion_summary,
    continuation_pending_episode_ids,
    create_continuation,
    resume_with_executor,
    validate_continuation_directory,
    validate_continuation_source,
)
from integrations.memebench.run_full100_v3_p2 import FrozenV3, canonical_json


REAL_STOPPED_RUN = Path(
    "integrations/memebench/runs/"
    "full100_v3_p2_e2e_v3_development_20260827"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_cost_interval_degenerates_to_point():
    row = exact_record(0.125)
    assert row["lower_usd"] == row["upper_usd"] == row["known_usd"] == 0.125
    assert row["cost_exact"] is True
    assert row["cost_bounded"] is row["cost_unbounded"] is False


def test_unknown_completion_uses_only_frozen_request_and_price_upper_bound():
    row = bounded_unknown_completion(
        known_usd=0.00004395,
        max_completion_tokens=100,
        output_per_million=0.6,
        attempt={"attempt": 1, "completion_tokens": None},
        request_snapshot={"max_completion_tokens": 100, "prompt_sha256": "a" * 64},
        price_snapshot={"input_per_million": 0.15, "output_per_million": 0.6},
    )
    assert row["lower_usd"] == 0.00004395
    assert row["upper_usd"] == pytest.approx(0.00010395)
    assert row["cost_bounded"] is True
    assert row["missing_token_types"] == ["completion_tokens"]


def test_unknown_usage_without_finite_evidence_is_unbounded():
    row = bounded_unknown_completion(
        known_usd=1.0,
        max_completion_tokens=None,
        output_per_million=0.6,
        attempt={"attempt": 1},
        request_snapshot=None,
        price_snapshot={"output_per_million": 0.6},
    )
    assert row["upper_usd"] is None
    assert row["cost_unbounded"] is True


def test_call_stage_case_global_interval_closure():
    calls = [
        exact_record(1.0),
        bounded_unknown_completion(
            known_usd=2.0,
            max_completion_tokens=10,
            output_per_million=1_000_000,
            attempt={"attempt": 1},
            request_snapshot={"max_completion_tokens": 10},
            price_snapshot={"output_per_million": 1_000_000},
        ),
    ]
    stage = aggregate_intervals(calls)
    case = aggregate_intervals([stage, exact_record(3.0)])
    global_cost = aggregate_intervals([case])
    assert stage["lower_usd"] == 3.0
    assert stage["upper_usd"] == 13.0
    assert case["lower_usd"] == global_cost["lower_usd"] == 6.0
    assert case["upper_usd"] == global_cost["upper_usd"] == 16.0


def test_live_unknown_retry_is_bounded_per_attempt_and_closes_to_stage():
    zero = {
        "known_usd": 0.0,
        "retry_usage_unknown": False,
    }
    artifact = {
        "cost": {
            "paid_execution": {
                "layers": {
                    "inference_llm": {
                        "known_usd": 0.0002,
                        "retry_usage_unknown": True,
                    },
                    "judge_llm": zero,
                    "p2_cheap_llm": zero,
                    "oracle_llm": zero,
                }
            },
            "failed_attempts": [],
            "calls": [
                {
                    "call_id": "a" * 64,
                    "kind": "answer",
                    "stage": "before.answer",
                    "usage_bucket": "inference_llm",
                    "usd": 0.0002,
                    "prompt_tokens": 100,
                    "retry_attempt": 1,
                    "retry_usage_unknown": True,
                    "request_sha256": "b" * 64,
                    "request_bytes": 400,
                    "price_snapshot": {
                        "input_per_million": 1.0,
                        "output_per_million": 2.0,
                    },
                }
            ],
        }
    }
    continuation._attach_native_cost_views(artifact, frozen=exact_record(0.0))
    call = artifact["cost"]["calls"][0]
    assert call["known_usd"] == pytest.approx(0.0003)
    assert call["upper_usd"] == pytest.approx(0.0004)
    assert call["unknown_usage_attempt_count"] == 1
    assert (
        artifact["cost"]["interval_stage_summary"]["before.answer"]["upper_usd"]
        == call["upper_usd"]
    )


def test_bounded_case_stays_in_accuracy_denominator_and_summary():
    bounded = bounded_unknown_completion(
        known_usd=1.0,
        max_completion_tokens=1,
        output_per_million=1_000_000,
        attempt={"attempt": 1},
        request_snapshot={"max_completion_tokens": 1},
        price_snapshot={"output_per_million": 1_000_000},
    )
    artifact = {
        "execution_complete": True,
        "runtime_status": "executed",
        "judge": {"calls": [{"parsed_verdict": "correct"}]},
    }
    summary = build_completion_summary(
        meme_comparable=[bounded],
        p2_incremental=[exact_record(0.0)],
        audit_all_in=[bounded],
        artifacts=[artifact],
    )
    assert summary["bounded_case_count"] == 1
    assert summary["accuracy_denominator"] == 1


def test_provider_failure_finalizes_and_batch_continues(tmp_path: Path, monkeypatch):
    marker = {"continuation_identity": "identity"}
    monkeypatch.setattr(
        continuation,
        "validate_continuation_directory",
        lambda _out: {"marker": marker},
    )
    calls = {"pending": 0}

    def pending(_out):
        calls["pending"] += 1
        return ["a", "b"] if calls["pending"] == 1 else []

    monkeypatch.setattr(continuation, "continuation_pending_episode_ids", pending)
    statuses = iter(("cost-bounded", "finalized"))
    result = resume_with_executor(
        tmp_path,
        lambda episode: {
            "status": next(statuses),
            "runtime_status": (
                "provider-call-failure" if episode == "a" else "executed"
            ),
        },
    )
    assert result["executed"] == ["a", "b"]
    assert json.loads(
        (tmp_path / "checkpoints/full-run/a-Cas.json").read_text()
    )["runtime_status"] == "provider-call-failure"


@pytest.mark.skipif(not REAL_STOPPED_RUN.is_dir(), reason="local frozen run unavailable")
def test_real_stopped_run_imports_58_without_external_calls(tmp_path: Path):
    before = continuation._tree_snapshot(REAL_STOPPED_RUN)
    manifest = create_continuation(REAL_STOPPED_RUN, tmp_path / "continued")
    after = continuation._tree_snapshot(REAL_STOPPED_RUN)
    assert manifest["imported_case_count"] == 58
    assert manifest["external_call_count"] == 0
    assert before == after
    pending = continuation_pending_episode_ids(tmp_path / "continued")
    assert pending[0] == "sw_009"
    assert len(pending) == 42


@pytest.mark.skipif(not REAL_STOPPED_RUN.is_dir(), reason="local frozen run unavailable")
def test_real_failed_sw009_is_not_successfully_imported():
    result = validate_continuation_source(REAL_STOPPED_RUN)
    assert [row["episode_id"] for row in result["failed_not_imported"]] == ["sw_009"]
    assert "sw_009" not in {
        row["episode_id"] for row in result["importable"]
    }


@pytest.mark.skipif(not REAL_STOPPED_RUN.is_dir(), reason="local frozen run unavailable")
def test_frozen_unknown_attempt_is_generically_bounded_from_request_metadata():
    row = continuation.frozen_cost_for_corpus(
        FrozenV3(),
        "sw_009",
        price_table={
            "gpt-4o-mini": {
                "input_per_million": 0.15,
                "output_per_million": 0.6,
            }
        },
    )
    assert row["known_usd"] == row["lower_usd"] == 0.00409015
    assert row["upper_usd"] == 0.00415015
    assert row["unknown_usage_attempt_count"] == 1
    source = row["upper_bound_sources"][0]
    assert source["max_completion_tokens"] == 100
    assert source["request_snapshot"]["prompt_sha256"]


def _fake_source(tmp_path: Path, monkeypatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    config = {
        "schema_version": "meme-full100-v3-p2-e2e-v3",
        "schema_generation": "v3",
        "mode_family": "full100-v3-p2-e2e-v3",
        "input_bundle": [],
    }
    identity = hashlib.sha256(canonical_json(config).encode()).hexdigest()
    marker = {"run_config_hash": identity}
    preflight = {
        "success": True,
        "run_config_hash": identity,
        "input_hashes": {},
        "run_config": config,
    }
    smoke = {**preflight, "success": True}
    for name, value in (
        ("run_identity.json", marker),
        ("preflight.json", preflight),
        ("smoke_result.json", smoke),
        ("paid_smoke_result.json", {"success": True}),
    ):
        (source / name).write_text(canonical_json(value) + "\n")
    artifact_root = source / "artifacts/full-run/a-paid"
    artifact_root.mkdir(parents=True)
    artifact = {
        "episode_id": "a",
        "artifact_mode": "full-run",
        "synthetic_stub": False,
        "authorization": {"run_config_hash": identity},
        "cost": {
            "known_usd": 1.0,
            "call_ledger_sha256": "c" * 64,
            "frozen_v3": {"known_usd": 0.25},
            "paid_execution": {
                "known_usd": 0.75,
                "layers": {
                    "inference_llm": {"known_usd": 0.25},
                    "judge_llm": {"known_usd": 0.5},
                    "p2_cheap_llm": {"known_usd": 0.0},
                    "oracle_llm": {"known_usd": 0.0},
                },
            },
        },
    }
    artifact_path = artifact_root / "artifact.json"
    artifact_path.write_text(canonical_json(artifact) + "\n")
    manifest = {
        "episode_id": "a",
        "run_config_hash": identity,
        "artifact_sha256": _sha(artifact_path),
        "artifact_bytes": artifact_path.stat().st_size,
    }
    (artifact_root / "manifest.json").write_text(canonical_json(manifest) + "\n")
    checkpoint_root = source / "checkpoints/full-run"
    checkpoint_root.mkdir(parents=True)
    checkpoint = {
        "status": "success",
        "checkpoint_namespace": "full-run",
        "run_config_hash": identity,
        "artifact_path": str(artifact_path.resolve()),
        "artifact_sha256": _sha(artifact_path),
        "attempt_token": "attempt",
        "attempts": [
            {
                "status": "success",
                "attempt_token": "attempt",
                "call_ledger_sha256": "c" * 64,
                "cost": artifact["cost"]["paid_execution"],
            }
        ],
    }
    (checkpoint_root / "a.json").write_text(canonical_json(checkpoint) + "\n")
    monkeypatch.setattr(continuation, "canonical_full100_episode_ids", lambda: ("a", "b"))
    monkeypatch.setattr(continuation, "verify_run_identity", lambda _identity: None)
    monkeypatch.setattr(
        continuation,
        "validate_paid_smoke_evidence",
        lambda *_args: {"artifact_sha256": "p" * 64},
    )
    monkeypatch.setattr(continuation, "validate_case_artifact", lambda _artifact: None)
    monkeypatch.setattr(continuation, "validate_paid_cost", lambda *_a, **_k: 1.0)
    return source


@pytest.mark.parametrize(
    "tamper",
    ("artifact", "manifest", "checkpoint", "identity", "extra", "missing"),
)
def test_continuation_rejects_tampered_missing_extra_source_evidence(
    tmp_path: Path, monkeypatch, tamper: str
):
    source = _fake_source(tmp_path, monkeypatch)
    artifact = source / "artifacts/full-run/a-paid/artifact.json"
    manifest = artifact.with_name("manifest.json")
    checkpoint = source / "checkpoints/full-run/a.json"
    if tamper == "artifact":
        artifact.write_text("{}")
    elif tamper == "manifest":
        manifest.write_text("{}")
    elif tamper == "checkpoint":
        checkpoint.write_text("{}")
    elif tamper == "identity":
        marker = json.loads((source / "run_identity.json").read_text())
        marker["run_config_hash"] = "changed"
        (source / "run_identity.json").write_text(canonical_json(marker))
    elif tamper == "extra":
        extra = source / "artifacts/full-run/extra"
        extra.mkdir()
        (extra / "artifact.json").write_text("{}")
        (extra / "manifest.json").write_text("{}")
    else:
        manifest.unlink()
    with pytest.raises(RuntimeError):
        validate_continuation_source(source)


def test_only_accounting_identity_upgrade_is_allowed(tmp_path: Path, monkeypatch):
    source = _fake_source(tmp_path, monkeypatch)
    out = tmp_path / "out"
    create_continuation(source, out)
    validate_continuation_directory(out)
    marker_path = out / "run_identity.json"
    marker = json.loads(marker_path.read_text())
    marker["behavior_config"]["mode_family"] = "changed-behavior"
    marker_path.write_text(canonical_json(marker))
    with pytest.raises(RuntimeError):
        validate_continuation_directory(out)


def test_completion_summary_reports_three_cost_states_and_interval_statistics():
    exact = exact_record(1.0)
    bounded = bounded_unknown_completion(
        known_usd=2.0,
        max_completion_tokens=1,
        output_per_million=1_000_000,
        attempt={"attempt": 1},
        request_snapshot={"max_completion_tokens": 1},
        price_snapshot={"output_per_million": 1_000_000},
    )
    unbounded = interval_record(
        known_usd=3.0,
        upper_usd=None,
        unknown_usage_attempts=[{"attempt": 2}],
        missing_token_types=["prompt_tokens", "completion_tokens"],
    )
    artifacts = [
        {
            "execution_complete": True,
            "runtime_status": "executed",
            "judge": {"calls": [{"parsed_verdict": "correct"}]},
        }
        for _ in range(3)
    ]
    summary = build_completion_summary(
        meme_comparable=[exact, bounded, unbounded],
        p2_incremental=[exact, exact, exact],
        audit_all_in=[exact, bounded, unbounded],
        artifacts=artifacts,
    )
    assert (
        summary["exact_case_count"],
        summary["bounded_case_count"],
        summary["unbounded_case_count"],
        summary["unknown_usage_attempts"],
    ) == (1, 1, 1, 2)
    assert summary["cost_views"]["audit_all_in"]["sum"] == {
        "lower_usd": 6.0,
        "upper_usd": None,
    }
