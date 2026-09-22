from __future__ import annotations

import json
import hashlib
import multiprocessing
import re
from pathlib import Path
from datetime import timedelta

import pytest

from integrations.memebench.run_full100_v3_p2 import (
    Checkpoint,
    DEFAULT_OUT,
    FrozenV3,
    MODE_FAMILY,
    RootIdentity,
    RootWorkload,
    SCHEMA_GENERATION,
    SCHEMA_VERSION,
    TRACE_SECTIONS,
    _main_async,
    build_run_identity,
    build_parser,
    canonical_evaluation_episode_ids,
    canonical_offline_status_preflight,
    canonical_json,
    canonical_full100_episode_ids,
    checkpoint_path,
    collapse_root_alias_edges,
    create_after_root_fallback,
    PREFLIGHT_CONTEXT_KEYS,
    ensure_output_directory,
    finalize_paid_case,
    load_evaluation_annotations,
    parse_paid_judge_output,
    recover_paid_execution_checkpoint,
    resolve_runtime_episode_input,
    resolve_retrieval_evidence_contract,
    resolve_root_identity,
    resolve_root_workload,
    validate_case_artifact,
    validate_full_run_completion,
    validate_paid_smoke_evidence,
    validate_paid_cost,
    validate_stale_isolation_retrieval_contract,
    verify_run_identity,
    write_case_artifact,
    write_input_invalid_paid_case,
)


def _claim_worker(path: str, ready, start, results):
    checkpoint = Checkpoint(Path(path), "config-a", "test")
    ready.put(True)
    start.wait()
    try:
        results.put(("claimed", checkpoint.claim()))
    except Exception as exc:
        results.put(("error", type(exc).__name__))


def _paid_cost():
    prices = {"input_per_million": 1.0, "output_per_million": 1.0}
    price_table = {"fixture-model": prices}
    calls = []
    for kind, stage, bucket, call_id, prompt, response in (
        (
            "answer",
            "on.answer",
            "inference_llm",
            "a" * 64,
            "real answer prompt",
            "real answer",
        ),
        (
            "judge",
            "on.judge",
            "judge_llm",
            "b" * 64,
            "real judge prompt",
            "correct",
        ),
    ):
        calls.append(
            {
                "call_id": call_id,
                "kind": kind,
                "stage": stage,
                "usage_bucket": bucket,
                "model": "fixture-model",
                "provider": "fixture-provider",
                "calls": 1,
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "tokens_are_real": True,
                "estimated": False,
                "retry_attempt": 0,
                "retry_usage_unknown": False,
                "request_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "request_bytes": len(prompt.encode()),
                "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
                "response_bytes": len(response.encode()),
                "raw_output_present": True,
                "price_version": hashlib.sha256(
                    canonical_json(price_table).encode()
                ).hexdigest(),
                "price_snapshot": prices,
                "usd": 1 / 1_000_000,
            }
        )
    layers = {}
    for bucket in ("inference_llm", "judge_llm", "oracle_llm", "p2_cheap_llm"):
        call_count = 1 if bucket in {"inference_llm", "judge_llm"} else 0
        prompt_tokens = call_count
        known = prompt_tokens / 1_000_000
        layers[bucket] = {
            "model": "fixture-model",
            "calls": call_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
            "tokens_are_real": True,
            "estimated_calls": 0,
            "retry_attempts": 0,
            "retry_usage_unknown": False,
            "retry_spend_usd": 0.0,
            "price_snapshot": prices,
            "known_usd": known,
        }
    execution = {
        "layers": layers,
        "cost_complete": True,
        "known_usd": 2 / 1_000_000,
        "strict_usd_interval": [2 / 1_000_000, 2 / 1_000_000],
    }
    stage_summary = {
        call["stage"]: {
            "usage_bucket": call["usage_bucket"],
            "calls": 1,
            "prompt_tokens": 1,
            "completion_tokens": 0,
            "known_usd": 1 / 1_000_000,
            "call_ids": [call["call_id"]],
        }
        for call in calls
    }
    return {
        "frozen_v3": {
            "cost_complete": True,
            "known_usd": 0.0,
            "strict_usd_interval": [0.0, 0.0],
        },
        "paid_execution": execution,
        "failed_attempts": [],
        "retry_spend_usd": 0.0,
        "cost_complete": True,
        "known_usd": 2 / 1_000_000,
        "strict_usd_interval": [2 / 1_000_000, 2 / 1_000_000],
        "calls": calls,
        "call_ledger_sha256": hashlib.sha256(
            canonical_json(calls).encode()
        ).hexdigest(),
        "stage_summary": stage_summary,
        "episode_summary": {
            "total_usd": 2 / 1_000_000,
            "call_count": 2,
            "call_ids": ["a" * 64, "b" * 64],
            "cost_complete": True,
        },
    }


def _artifact(*, mode="full-run"):
    prices = {"fixture-model": {"input_per_million": 1.0, "output_per_million": 1.0}}
    config = {
        "schema_version": SCHEMA_VERSION,
        "models": {
            "chat_model": "fixture-model",
            "judge_model": "fixture-model",
            "p2_cheap_model": "fixture-model",
            "p2_strong_model": "fixture-model",
        },
        "provider_bindings": {"provider": "fixture-provider"},
        "price_table": prices,
        "safe": True,
    }
    config_hash = hashlib.sha256(canonical_json(config).encode()).hexdigest()
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "artifact_mode": mode,
        "episode_id": "episode",
        "synthetic_stub": mode == "no-api",
        "execution_complete": True,
        "runtime_status": "executed",
        "root_binding_status": "bound",
        "case_success": mode != "no-api",
        "development_reevaluation": True,
        **{
            section: {"body": section}
            for section in TRACE_SECTIONS
            if section not in {"authorization", "answers", "judge"}
        },
        "authorization": {
            "run_config": config,
            "run_config_hash": config_hash,
            "run_config_hash_recomputed": config_hash,
            "secrets_persisted": False,
        },
        "cost": _paid_cost() if mode != "no-api" else {"known_usd": 0.0},
    }
    if mode == "no-api":
        artifact["answers"] = {"body": "answers"}
        artifact["judge"] = {"body": "judge"}
    else:
        artifact["answers"] = {
            "on": {
                "call_id": "a" * 64,
                "prompt": "real answer prompt",
                "raw_answer": "real answer",
                "model": "fixture-model",
                "provider": "fixture-provider",
                "usage": {
                    "calls": 1,
                    "prompt_tokens": 1,
                    "completion_tokens": 0,
                    "tokens_are_real": True,
                    "retry_attempts": 0,
                    "retry_usage_unknown": False,
                },
            }
        }
        artifact["judge"] = {
            "calls": [
                {
                    "call_id": "b" * 64,
                    "stage": "on",
                    "prompt": "real judge prompt",
                    "raw_output": "correct",
                    "parsed_verdict": "correct",
                    "fallback": False,
                    "model": "fixture-model",
                    "provider": "fixture-provider",
                    "usage": {
                        "calls": 1,
                        "prompt_tokens": 1,
                        "completion_tokens": 0,
                        "tokens_are_real": True,
                        "retry_attempts": 0,
                        "retry_usage_unknown": False,
                    },
                }
            ]
        }
    return artifact


def _full_run_config_hash() -> str:
    return _artifact()["authorization"]["run_config_hash"]


def test_frozen_manifest_has_one_graph_and_two_scoring_views():
    corpus = FrozenV3()
    assert len(corpus.episode_ids) == 100
    assert sum(row["hop1_applicable"] for row in corpus.index["episodes"]) == 100
    assert sum(row["hop2_applicable"] for row in corpus.index["episodes"]) == 64
    assert corpus.selected_edges(corpus.episode_ids[0])


def test_v3_default_output_and_directory_generation_guard(tmp_path: Path):
    assert DEFAULT_OUT.name == "full100_v3_p2_e2e_v3"
    identity = _identity()
    out = tmp_path / "fresh-v3"
    ensure_output_directory(out, identity, command_mode="no-api")
    marker = json.loads((out / "run_identity.json").read_text())
    assert marker["schema_generation"] == "v3"
    changed = _identity()
    changed["run_config_hash"] = "different"
    with pytest.raises(RuntimeError, match="identity mismatch"):
        ensure_output_directory(out, changed, command_mode="paid-smoke")
    with pytest.raises(RuntimeError, match="refuses a v1/v2"):
        ensure_output_directory(
            tmp_path / "full100_v3_p2_e2e_v1",
            identity,
            command_mode="no-api",
        )
    for name in ("obvious-v1", "V1-paid", "run.v1.final", "old-v2"):
        with pytest.raises(RuntimeError, match="refuses a v1/v2"):
            ensure_output_directory(
                tmp_path / name,
                identity,
                command_mode="no-api",
            )
    ensure_output_directory(
        tmp_path / "revision1-is-current",
        identity,
        command_mode="no-api",
    )


def test_v3_directory_grammar_rejects_nested_junk_and_symlinks(tmp_path: Path):
    identity = _identity()
    out = tmp_path / "strict-v3"
    ensure_output_directory(out, identity, command_mode="no-api")
    junk = out / "cases" / "pl_001"
    junk.mkdir(parents=True)
    (junk / "junk.bin").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="unknown file"):
        ensure_output_directory(out, identity, command_mode="no-api")
    (junk / "junk.bin").unlink()
    (junk / "artifact.json").write_text("{}")
    (junk / "manifest.json").write_text("{}")
    link = out / "cases" / "pl_002"
    link.symlink_to(junk, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        ensure_output_directory(out, identity, command_mode="no-api")


def test_v3_directory_grammar_accepts_exact_known_layout(tmp_path: Path):
    identity = _identity()
    out = tmp_path / "strict-v3"
    ensure_output_directory(out, identity, command_mode="no-api")
    case = out / "cases" / "pl_001"
    artifact = _artifact(mode="no-api")
    artifact["episode_id"] = "pl_001"
    artifact["authorization"] = {
        "run_config": identity["config"],
        "run_config_hash": identity["run_config_hash"],
        "run_config_hash_recomputed": identity["run_config_hash"],
        "secrets_persisted": False,
    }
    write_case_artifact(case, artifact)
    checkpoint = out / "checkpoints" / "no-api"
    checkpoint.mkdir(parents=True)
    (checkpoint / "pl_001-Cas.json.lock").write_bytes(b"")
    ensure_output_directory(out, identity, command_mode="no-api")


def test_atomic_case_export_has_verified_manifest(tmp_path: Path):
    manifest = write_case_artifact(tmp_path / "case", _artifact())
    assert len(manifest["artifact_sha256"]) == 64
    assert json.loads((tmp_path / "case" / "artifact.json").read_text())[
        "development_reevaluation"
    ]


def test_trace_schema_rejects_empty_sections_and_secret_markers():
    artifact = _artifact()
    artifact["judge"] = {}
    with pytest.raises(ValueError, match="judge"):
        validate_case_artifact(artifact)
    artifact = _artifact()
    artifact["cost"]["api_key"] = "forbidden"
    with pytest.raises(ValueError, match="secret"):
        validate_case_artifact(artifact)


def test_checkpoint_refuses_config_mixing_and_resumes_success(tmp_path: Path):
    path = tmp_path / "checkpoint.json"
    first = Checkpoint(path, "config-a", "test")
    assert first.claim()
    first.finish("success", artifact_sha256="abc")
    assert Checkpoint(path, "config-a", "test").claim() is False
    with pytest.raises(ValueError, match="different"):
        Checkpoint(path, "config-b", "test").claim()


def test_checkpoint_cross_process_claim_and_attempt_cas(tmp_path: Path):
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    path = tmp_path / "checkpoint.json"
    workers = [
        context.Process(
            target=_claim_worker,
            args=(str(path), ready, start, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for _ in workers:
        ready.get(timeout=5)
    start.set()
    rows = [results.get(timeout=5) for _ in workers]
    for worker in workers:
        worker.join(timeout=5)
    assert sorted(rows) == [("claimed", True), ("error", "RuntimeError")]

    old = Checkpoint(path, "config-a", "test", stale_after=timedelta(0))
    assert old.claim()
    old_token = old.attempt_token
    new = Checkpoint(path, "config-a", "test", stale_after=timedelta(0))
    assert new.claim()
    assert new.attempt_token != old_token
    with pytest.raises(RuntimeError, match="lease lost"):
        old.finish("success")
    new.heartbeat()
    new.finish("failed", error="network interrupted")
    resumed = Checkpoint(path, "config-a", "test", stale_after=timedelta(0))
    assert resumed.claim()
    resumed.finish("success", artifact_sha256="new-artifact")


def test_real_root_workload_binds_frozen_identity_before_after():
    corpus = FrozenV3()
    hop2 = resolve_root_workload(corpus, "pl_001")
    hop1 = resolve_root_workload(corpus, "pl_002")
    assert hop2.entity == "health_condition"
    assert hop2.before == "lactose intolerance"
    assert hop2.after == "high blood pressure"
    assert all(
        hop2.before.casefold() in alias.text.casefold()
        for alias in hop2.root_identity.aliases
    )
    assert hop2.after.casefold() in hop2.change_evidence_text.casefold()
    assert all(
        alias.node_id.startswith("node-") and alias.evidence_ids
        for alias in hop2.root_identity.aliases
    )
    assert hop1.entity == "residence_location"
    assert hop1.before == "Cymbeline Shores"
    assert hop1.after == "Orinthal Park"


def test_pl016_root_alias_group_recovers_frontier_without_gold():
    corpus = FrozenV3()
    frozen_edges = corpus.selected_edges("pl_016")
    frozen_snapshot = canonical_json(frozen_edges)
    identity = resolve_root_identity(corpus, "pl_016")
    assert identity.applicability == "applicable"
    assert identity.ambiguity_status == "resolved_alias_group"
    assert {alias.text for alias in identity.aliases} == {
        "The user is engaged.",
        "I am engaged.",
    }
    assert all(alias.evidence_ids for alias in identity.aliases)
    runtime_edges, audit = collapse_root_alias_edges(frozen_edges, identity)
    old_target = "node-11fe7d7a7b3cbebe8200c064fbfe66c05cf7d08b536f4393b5d27399ac1cb42d"
    assert old_target in audit["union_root_targets"]
    assert any(
        edge["dependent_node_id"] == old_target
        and edge["dependency_node_id"] in identity.alias_node_ids
        for edge in runtime_edges
    )
    assert canonical_json(corpus.selected_edges("pl_016")) == frozen_snapshot

    contract = resolve_retrieval_evidence_contract(corpus, "pl_016")
    assert contract.root_group_id == identity.group_id
    assert contract.old_target_node_ids == (old_target,)


def test_pl013_direct_root_rejects_dependent_reason_clause_aliases():
    corpus = FrozenV3()
    identity = resolve_root_identity(corpus, "pl_013")
    assert [alias.text for alias in identity.aliases] == [
        "The user works at Quenlix Group, where they manage engineering "
        "projects and teams."
    ]
    rejected = {
        candidate.text for candidate in identity.rejected_reason_clause_candidates
    }
    assert any("shift work (night)" in text for text in rejected)
    assert any("infrastructure upgrade" in text for text in rejected)
    assert any("office is in Quivira Springs" in text for text in rejected)
    runtime_edges, audit = collapse_root_alias_edges(
        corpus.selected_edges("pl_013"), identity
    )
    old_target = (
        "node-688e45ba2ffd3c756da38dcc37795b4ff54de7a648193875293a2f7250b3ea47"
    )
    assert old_target in audit["union_root_targets"]
    assert any(
        edge["dependent_node_id"] == old_target for edge in runtime_edges
    )


def test_original_polluted_groups_no_longer_swallow_reason_clause_dependents():
    corpus = FrozenV3()
    polluted = (
        "pl_013 pl_030 pl_042 pl_046 pl_048 sw_001 sw_002 sw_003 sw_006 "
        "sw_008 sw_010 sw_015 sw_026 sw_031 sw_032 sw_037 sw_038 sw_050"
    ).split()
    for episode_id in polluted:
        identity = resolve_root_identity(corpus, episode_id)
        assert identity.aliases
        assert identity.rejected_reason_clause_candidates
        assert all(
            "if " not in alias.text.casefold()
            and "depends on" not in alias.text.casefold()
            and "determined by" not in alias.text.casefold()
            for alias in identity.aliases
        )


def test_canonical_status_preflight_closes_all_cases_without_raising():
    result = canonical_offline_status_preflight()
    assert result["case_count"] == 100
    assert result["all_statuses_closed"]
    assert result["statistics"]["input_invalid"] == 0
    assert all(
        row["p1_path_status"] in {"present", "p1-system-miss"}
        for row in result["rows"]
    )
    pl043 = next(row for row in result["rows"] if row["episode_id"] == "pl_043")
    assert pl043["surface_mapping_status"] == "surface-mismatch"
    assert pl043["p1_path_status"] == "present"


def test_hop2_view_is_canonical_and_selects_real_hop2_question():
    corpus = FrozenV3()
    ids = canonical_evaluation_episode_ids(corpus, evaluation_hop=2)
    assert len(ids) == 64
    assert len(set(ids)) == 64
    assert set(ids) == {
        str(row["episode_id"])
        for row in corpus.index["episodes"]
        if row["hop2_applicable"]
    }
    hop1 = resolve_runtime_episode_input(corpus, "pl_001", evaluation_hop=1)
    hop2 = resolve_runtime_episode_input(corpus, "pl_001", evaluation_hop=2)
    assert hop1.before_question != hop2.before_question
    assert hop2.before_question == "Where do I work out?"
    annotations = load_evaluation_annotations(
        corpus, "pl_001", evaluation_hop=2
    )
    assert annotations.annotated_hop == 2
    assert annotations.before_reference == "Crysthene Pool"
    assert annotations.after_reference == "Velthari Studio"


def test_runtime_input_does_not_read_gold_or_dependency_edges():
    corpus = FrozenV3()
    baseline = resolve_runtime_episode_input(
        corpus, "pl_013", evaluation_hop=2
    )
    episode = json.loads(json.dumps(corpus.dataset_episodes()[12]))
    assert episode["episode_id"] == "pl_013"
    for section, answer_key in (
        ("before_questions", "expected_answer"),
        ("after_questions", "gold_answer"),
    ):
        for row in episode[section]["questions"]:
            row[answer_key] = "mutated-gold"
    episode["dependency_edges_used"] = [{"mutated": True}]

    class MutatedGoldCorpus:
        def dataset_episodes(self, _data=None):
            return [episode]

        def episode(self, episode_id):
            return corpus.episode(episode_id)

        def selected_edges(self, episode_id):
            return corpus.selected_edges(episode_id)

    mutated_corpus = MutatedGoldCorpus()
    mutated = resolve_runtime_episode_input(
        mutated_corpus, "pl_013", evaluation_hop=2, data="unused"
    )
    assert mutated == baseline
    baseline_identity = resolve_root_identity(corpus, "pl_013")
    mutated_identity = resolve_root_identity(
        mutated_corpus, "pl_013", data="unused"
    )
    assert mutated_identity == baseline_identity
    baseline_frontier = collapse_root_alias_edges(
        corpus.selected_edges("pl_013"), baseline_identity
    )
    mutated_frontier = collapse_root_alias_edges(
        mutated_corpus.selected_edges("pl_013"), mutated_identity
    )
    assert mutated_frontier == baseline_frontier
    assert {
        "queries": (baseline.before_question, baseline.after_question),
        "answer_stages": ("before.answer", "off.answer", "on.answer"),
        "judge_stages": ("before.judge", "off.judge", "on.judge"),
    } == {
        "queries": (mutated.before_question, mutated.after_question),
        "answer_stages": ("before.answer", "off.answer", "on.answer"),
        "judge_stages": ("before.judge", "off.judge", "on.judge"),
    }


def test_hop2_status_preflight_closes_only_canonical_view():
    result = canonical_offline_status_preflight(evaluation_hop=2)
    assert result["evaluation_hop"] == 2
    assert result["case_count"] == 64
    assert result["all_statuses_closed"]
    assert result["statistics"]["input_invalid"] == 0
    assert all(
        row["p1_path_status"] in {"present", "p1-system-miss"}
        for row in result["rows"]
    )


def test_hop_view_is_part_of_identity_and_parser_defaults_to_hop1():
    corpus = FrozenV3()
    kwargs = {
        "models": {"all": "offline-stub"},
        "prices": {
            "offline-stub": {
                "input_per_million": 0,
                "output_per_million": 0,
            }
        },
        "provider_config": {},
    }
    hop1 = build_run_identity(corpus, evaluation_hop=1, **kwargs)
    hop2 = build_run_identity(corpus, evaluation_hop=2, **kwargs)
    assert hop1["run_config_hash"] != hop2["run_config_hash"]
    assert hop2["config"]["evaluation_hop"] == 2
    assert build_parser().parse_args(["preflight"]).hop == 1
    assert build_parser().parse_args(["preflight", "--hop", "2"]).hop == 2


@pytest.mark.asyncio
async def test_root_binding_miss_fallback_creates_independent_after_root():
    identity = RootIdentity(
        episode_id="synthetic",
        entity="employer",
        normalized_before="old co",
        normalized_after="new co",
        group_id="root-group-synthetic",
        change_id="root-change-synthetic",
        aliases=(),
        applicability="root_binding_miss",
        ambiguity_status="ambiguous",
        ambiguity_reasons=("no-direct-root-proposition",),
    )
    workload = RootWorkload(
        episode_id="synthetic",
        entity="employer",
        before="Old Co",
        after="New Co",
        root_identity=identity,
        change_evidence_text="I now work at New Co.",
    )

    class FakeDB:
        def __init__(self):
            self.calls = []

        async def execute(self, statement, *args):
            self.calls.append((statement, args))

    db = FakeDB()
    trace = await create_after_root_fallback(db, workload, account="fixture")
    assert len(db.calls) == 1
    assert trace[0]["fallback"] is True
    assert trace[0]["old_root_nodes_modified"] is False
    assert trace[0]["propagation_frontier_proven"] is False


def test_unchanged_retrieval_is_system_outcome_not_integrity_error():
    stage = {
        "prompt_sha256": "same",
        "retrieval": {
            "retrieval_id": "retrieval",
            "request": {"query": "Where?"},
            "final_materialized": [],
            "context_hash": "same",
        },
    }
    result = validate_stale_isolation_retrieval_contract(
        stage,
        stage,
        runtime_input=type(
            "RuntimeInput",
            (),
            {"after_question": "Where?"},
        )(),
        state_evidence={"contexts": [], "invalidations": []},
        root_context_ids=(),
    )
    assert result["integrity_complete"] is True
    assert result["service_content_changed"] is False
    assert result["system_outcome_passed"] is False


class _SyntheticAliasCorpus:
    def __init__(self, *, multi_alias: bool = True, reconvergent: bool = True):
        aliases = [
            {
                "node_id": "alias-a",
                "text": "I am ready.",
                "source_origin": "fixture",
            }
        ]
        if multi_alias:
            aliases.append(
                {
                    "node_id": "alias-b",
                    "text": "The user is ready.",
                    "source_origin": "fixture",
                }
            )
        self._nodes = aliases + [
            {"node_id": "left", "text": "left branch"},
            {"node_id": "right", "text": "right branch"},
            {"node_id": "shared", "text": "shared branch"},
        ]
        self._edges = [
            {
                "dependency_node_id": "alias-a",
                "dependent_node_id": "left",
                "source_evidence_id": "ev-a",
                "target_evidence_id": "ev-left",
                "source_origin": "fixture",
            },
            {
                "dependency_node_id": "alias-a",
                "dependent_node_id": "shared",
                "source_evidence_id": "ev-a",
                "target_evidence_id": "ev-shared",
                "source_origin": "fixture",
            },
        ]
        if multi_alias:
            self._edges.append(
                {
                    "dependency_node_id": "alias-b",
                    "dependent_node_id": "right",
                    "source_evidence_id": "ev-b",
                    "target_evidence_id": "ev-right",
                    "source_origin": "fixture",
                }
            )
            if reconvergent:
                self._edges.append(
                    {
                        "dependency_node_id": "alias-b",
                        "dependent_node_id": "shared",
                        "source_evidence_id": "ev-b",
                        "target_evidence_id": "ev-shared",
                        "source_origin": "fixture",
                    }
                )

    def dataset_episodes(self, _data=None):
        return [
            {
                "episode_id": "synthetic",
                "root": "state",
                "root_change": {"before": "ready", "after": "done"},
            }
        ]

    def episode(self, _episode_id):
        return {
            "episode_id": "synthetic",
            "nodes": self._nodes,
            "alignments": [
                {
                    "node_id": node["node_id"],
                    "status": "aligned",
                    "evidence_id": f"align-{node['node_id']}",
                    "original_session_id": "fixture-session",
                }
                for node in self._nodes
            ],
        }

    def selected_edges(self, _episode_id):
        return json.loads(json.dumps(self._edges))


def test_synthetic_multi_alias_frontier_union_and_reconvergence_dedup():
    corpus = _SyntheticAliasCorpus()
    identity = resolve_root_identity(corpus, "synthetic", data="unused")
    assert identity.alias_node_ids == ("alias-a", "alias-b")
    runtime_edges, audit = collapse_root_alias_edges(
        corpus.selected_edges("synthetic"), identity
    )
    assert audit["union_root_targets"] == ["left", "right", "shared"]
    assert len(audit["suppressed_reconvergent_root_edges"]) == 1
    assert [
        (row["dependency_node_id"], row["dependent_node_id"]) for row in runtime_edges
    ] == [
        ("alias-a", "left"),
        ("alias-a", "shared"),
        ("alias-b", "right"),
    ]


def test_single_alias_frontier_is_unchanged():
    corpus = _SyntheticAliasCorpus(multi_alias=False)
    identity = resolve_root_identity(corpus, "synthetic", data="unused")
    original = corpus.selected_edges("synthetic")
    runtime_edges, audit = collapse_root_alias_edges(original, identity)
    assert identity.ambiguity_status == "unambiguous"
    assert runtime_edges == original
    assert audit["suppressed_intra_identity_edges"] == []
    assert audit["suppressed_reconvergent_root_edges"] == []


def _identity():
    config = {
        "schema_version": SCHEMA_VERSION,
        "schema_generation": SCHEMA_GENERATION,
        "mode_family": MODE_FAMILY,
        "input_bundle": [],
        "models": {
            "chat_model": "fixture-model",
            "judge_model": "fixture-model",
            "p2_cheap_model": "fixture-model",
            "p2_strong_model": "fixture-model",
        },
        "provider_bindings": {"provider": "fixture-provider"},
        "price_table": {
            "fixture-model": {
                "input_per_million": 1.0,
                "output_per_million": 1.0,
            }
        },
        "safe": True,
    }
    return {
        "run_config_hash": hashlib.sha256(canonical_json(config).encode()).hexdigest(),
        "input_hashes": {"dataset": "hash"},
        "config": config,
    }


def _write_paid_evidence(out: Path, identity, *, do_finalize: bool = True):
    episode_id = "pl_033"
    artifact = _artifact(mode="paid-smoke")
    artifact["episode_id"] = episode_id
    artifact["authorization"] = {
        "run_config": identity["config"],
        "run_config_hash": identity["run_config_hash"],
        "run_config_hash_recomputed": identity["run_config_hash"],
        "secrets_persisted": False,
    }
    artifact["production_retrieval_change_verified"] = {
        "integrity_complete": True
    }
    artifact["p2_queue"] = {"events": [], "unfinished": []}
    artifact["cleanup"] = {"complete": True}
    artifact["attempt_token"] = "fixture-attempt"
    case_dir = out / "artifacts" / "paid-smoke" / f"{episode_id}-Cas-aaaaaaaaaaaa"
    manifest = write_case_artifact(case_dir, artifact)
    artifact_path = case_dir / "artifact.json"
    checkpoint = checkpoint_path(out, "paid-smoke", episode_id)
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps(
            {
                "status": "execution_complete",
                "checkpoint_namespace": "paid-smoke",
                "run_config_hash": identity["run_config_hash"],
                "artifact_path": str(artifact_path),
                "artifact_sha256": manifest["artifact_sha256"],
                "attempt_token": "fixture-attempt",
                "attempts": [
                    {
                        "attempt_token": "fixture-attempt",
                        "status": "execution_complete",
                        "finished_at": "fixture-finished",
                        "usage": _paid_cost()["paid_execution"]["layers"],
                        "cost": _paid_cost()["paid_execution"],
                        "call_ledger_sha256": _paid_cost()["call_ledger_sha256"],
                    }
                ],
            }
        )
    )
    if do_finalize:
        return finalize_paid_case(out, identity, episode_id)
    return artifact_path


def test_input_invalid_paid_case_closes_without_external_calls(tmp_path: Path):
    class Corpus:
        def episode_cost(self, _episode_id):
            return {
                "new": {
                    "deployment": 0.25,
                    "cost_incomplete_attempts": 0,
                }
            }

    identity = _identity()
    path = write_input_invalid_paid_case(
        corpus=Corpus(),
        episode_id="invalid",
        out=tmp_path,
        identity=identity,
        artifact_namespace="full-run",
        prices=identity["config"]["price_table"],
        error="missing question",
        attempt_token="attempt",
    )
    artifact = json.loads(path.read_text())
    assert artifact["execution_complete"]
    assert artifact["runtime_status"] == "input-invalid"
    assert artifact["cost"]["calls"] == []
    assert artifact["answers"]["status"] == "not-executed"


def test_paid_gate_reconstructs_primary_evidence_and_rejects_forged_summary(
    tmp_path: Path,
):
    identity = _identity()
    summary = _write_paid_evidence(tmp_path, identity)
    assert validate_paid_smoke_evidence(tmp_path, identity)["episode_id"] == "pl_033"
    summary["artifact_sha256"] = "forged"
    (tmp_path / "paid_smoke_result.json").write_text(json.dumps(summary))
    with pytest.raises(RuntimeError, match="summary"):
        validate_paid_smoke_evidence(tmp_path, identity)


@pytest.mark.parametrize(
    "failure",
    (
        "unknown_stage",
        "bucket_swap",
        "duplicate_call_id",
        "missing_call_id",
        "model_mismatch",
        "price_mismatch",
        "placeholder_trace",
        "trace_only",
        "ledger_only",
    ),
)
def test_paid_call_evidence_contract_rejects_anti_examples(failure: str):
    artifact = _artifact(mode="paid-smoke")
    cost = artifact["cost"]
    if failure == "unknown_stage":
        cost["calls"][0]["stage"] = "unknown"
    elif failure == "bucket_swap":
        cost["calls"][0]["usage_bucket"] = "judge_llm"
    elif failure == "duplicate_call_id":
        cost["calls"][1]["call_id"] = cost["calls"][0]["call_id"]
    elif failure == "missing_call_id":
        cost["calls"][0].pop("call_id")
    elif failure == "model_mismatch":
        cost["calls"][0]["model"] = "wrong-model"
    elif failure == "price_mismatch":
        cost["calls"][0]["price_snapshot"]["input_per_million"] = 2
    elif failure == "placeholder_trace":
        artifact["answers"]["on"]["raw_answer"] = "placeholder"
    elif failure == "trace_only":
        artifact["answers"]["off"] = {
            **artifact["answers"]["on"],
            "call_id": "c" * 64,
        }
    elif failure == "ledger_only":
        artifact["answers"] = {}
    cost["call_ledger_sha256"] = hashlib.sha256(
        canonical_json(cost["calls"]).encode()
    ).hexdigest()
    with pytest.raises(RuntimeError):
        validate_paid_cost(
            cost,
            artifact=artifact,
            config=artifact["authorization"]["run_config"],
        )


def test_paid_gate_rejects_checkpoint_zero_vs_artifact_nonzero(tmp_path: Path):
    identity = _identity()
    _write_paid_evidence(tmp_path, identity)
    checkpoint_path_value = checkpoint_path(tmp_path, "paid-smoke", "pl_033")
    checkpoint = json.loads(checkpoint_path_value.read_text())
    attempt = checkpoint["attempts"][-1]
    for layer in attempt["cost"]["layers"].values():
        layer["calls"] = 0
        layer["prompt_tokens"] = 0
        layer["completion_tokens"] = 0
        layer["known_usd"] = 0.0
    attempt["cost"]["known_usd"] = 0.0
    attempt["cost"]["strict_usd_interval"] = [0.0, 0.0]
    checkpoint_path_value.write_text(json.dumps(checkpoint))
    with pytest.raises(RuntimeError, match="attempt mismatch"):
        validate_paid_smoke_evidence(tmp_path, identity)


@pytest.mark.parametrize("crash_after", ("index", "summary"))
def test_paid_finalization_recovers_each_crash_boundary(
    tmp_path: Path,
    crash_after: str,
):
    identity = _identity()
    _write_paid_evidence(tmp_path, identity, do_finalize=False)
    with pytest.raises(RuntimeError, match="injected crash"):
        finalize_paid_case(
            tmp_path,
            identity,
            "pl_033",
            crash_after=crash_after,
        )
    summary = finalize_paid_case(tmp_path, identity, "pl_033")
    assert summary == finalize_paid_case(tmp_path, identity, "pl_033")
    assert len((tmp_path / "paid_smoke_cases.jsonl").read_text().splitlines()) == 1
    assert (
        json.loads(checkpoint_path(tmp_path, "paid-smoke", "pl_033").read_text())[
            "status"
        ]
        == "finalized"
    )
    validate_paid_smoke_evidence(tmp_path, identity)


def test_paid_artifact_before_checkpoint_recovers_without_new_execution(
    tmp_path: Path,
):
    identity = _identity()
    _write_paid_evidence(tmp_path, identity, do_finalize=False)
    checkpoint_file = checkpoint_path(tmp_path, "paid-smoke", "pl_033")
    checkpoint = json.loads(checkpoint_file.read_text())
    checkpoint["status"] = "in_progress"
    checkpoint["attempts"] = []
    checkpoint.pop("artifact_path")
    checkpoint.pop("artifact_sha256")
    checkpoint_file.write_text(json.dumps(checkpoint))
    assert recover_paid_execution_checkpoint(tmp_path, identity, "pl_033") is True
    recovered = json.loads(checkpoint_file.read_text())
    assert recovered["status"] == "execution_complete"
    assert recovered["recovered_without_external_calls"] is True
    finalize_paid_case(tmp_path, identity, "pl_033")
    validate_paid_smoke_evidence(tmp_path, identity)


@pytest.mark.parametrize("crash_after", ("artifact", "index_partial", "summary"))
def test_paid_resume_after_crash_never_reexecutes_external_call(
    tmp_path: Path, crash_after: str
):
    identity = _identity()
    external_calls = 1
    ensure_output_directory(tmp_path, identity, command_mode="paid-smoke")
    _write_paid_evidence(tmp_path, identity, do_finalize=False)
    if crash_after == "artifact":
        checkpoint_file = checkpoint_path(tmp_path, "paid-smoke", "pl_033")
        checkpoint = json.loads(checkpoint_file.read_text())
        checkpoint["status"] = "in_progress"
        checkpoint["attempts"] = []
        checkpoint.pop("artifact_path")
        checkpoint.pop("artifact_sha256")
        checkpoint_file.write_text(json.dumps(checkpoint))
        (tmp_path / ".staging" / "pl_033").mkdir(parents=True)
        (
            tmp_path / ".staging" / "pl_033" / ".artifact.json."
            "0123456789abcdef0123456789abcdef.tmp"
        ).write_text("{}")
        ensure_output_directory(tmp_path, identity, command_mode="paid-smoke")
        assert recover_paid_execution_checkpoint(tmp_path, identity, "pl_033") is True
    elif crash_after == "index_partial":
        (tmp_path / "paid_smoke_cases.jsonl").write_text('{"episode_id":')
    else:
        with pytest.raises(RuntimeError, match="injected crash"):
            finalize_paid_case(tmp_path, identity, "pl_033", crash_after="summary")
    finalize_paid_case(tmp_path, identity, "pl_033")
    validate_paid_smoke_evidence(tmp_path, identity)
    assert external_calls == 1


@pytest.mark.parametrize(
    "failure",
    ("duplicate_index", "stale_summary_cases", "orphan_artifact", "hash_conflict"),
)
def test_paid_gate_rejects_index_summary_and_orphan_anomalies(
    tmp_path: Path, failure: str
):
    identity = _identity()
    summary = _write_paid_evidence(tmp_path, identity)
    index_path = tmp_path / "paid_smoke_cases.jsonl"
    if failure == "duplicate_index":
        index_path.write_text(index_path.read_text() * 2)
    elif failure == "stale_summary_cases":
        summary["cases"] = []
        (tmp_path / "paid_smoke_result.json").write_text(json.dumps(summary))
    elif failure == "orphan_artifact":
        orphan = _artifact(mode="paid-smoke")
        orphan["episode_id"] = "orphan"
        write_case_artifact(
            tmp_path / "artifacts" / "paid-smoke" / "orphan-fixture", orphan
        )
    elif failure == "hash_conflict":
        row = json.loads(index_path.read_text())
        row["artifact_sha256"] = "conflict"
        index_path.write_text(canonical_json(row) + "\n")
    with pytest.raises(RuntimeError):
        validate_paid_smoke_evidence(tmp_path, identity)


def test_frozen_dataset_uses_one_snapshot_and_detects_later_change(tmp_path: Path):
    corpus = FrozenV3()
    dataset = tmp_path / "dataset.json"
    dataset.write_text('[{"episode_id":"original"}]')
    assert corpus.dataset_episodes(dataset)[0]["episode_id"] == "original"
    dataset.write_text('[{"episode_id":"changed"}]')
    assert corpus.dataset_episodes(dataset)[0]["episode_id"] == "original"
    with pytest.raises(RuntimeError, match="dataset changed"):
        corpus.verify_identity(dataset)


def _write_full_run_case(
    out: Path,
    episode_id: str,
    *,
    synthetic: bool = False,
) -> Path:
    artifact = _artifact()
    artifact["episode_id"] = episode_id
    artifact["synthetic_stub"] = synthetic
    case_dir = out / "artifacts" / "full-run" / f"{episode_id}-paid"
    write_case_artifact(case_dir, artifact)
    artifact_path = case_dir / "artifact.json"
    checkpoint = checkpoint_path(out, "full-run", episode_id)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        json.dumps(
            {
                "status": "success",
                "checkpoint_namespace": "full-run",
                "run_config_hash": artifact["authorization"]["run_config_hash"],
                "artifact_path": str(artifact_path),
                "attempt_token": "fixture-attempt",
                "artifact_sha256": hashlib.sha256(
                    artifact_path.read_bytes()
                ).hexdigest(),
                "attempts": [
                    {
                        "status": "success",
                        "attempt_token": "fixture-attempt",
                        "usage": artifact["cost"]["paid_execution"]["layers"],
                        "cost": artifact["cost"]["paid_execution"],
                        "call_ledger_sha256": artifact["cost"]["call_ledger_sha256"],
                    }
                ],
            }
        )
    )
    return artifact_path


def _rewrite_case_and_closure(out: Path, episode_id: str, mutate) -> Path:
    checkpoint_file = checkpoint_path(out, "full-run", episode_id)
    checkpoint = json.loads(checkpoint_file.read_text())
    artifact_path = Path(checkpoint["artifact_path"])
    artifact = json.loads(artifact_path.read_text())
    mutate(artifact)
    manifest = write_case_artifact(artifact_path.parent, artifact)
    checkpoint["artifact_sha256"] = manifest["artifact_sha256"]
    checkpoint_file.write_text(json.dumps(checkpoint))
    return artifact_path


def _write_canonical_full_run(out: Path) -> list[str]:
    expected = list(canonical_full100_episode_ids())
    for episode_id in expected:
        _write_full_run_case(out, episode_id)
    return expected


def test_no_api_success_cannot_skip_full_run_checkpoint(tmp_path: Path):
    no_api = Checkpoint(
        checkpoint_path(tmp_path, "no-api", "pl_001"),
        "config-a",
        "no-api",
    )
    assert no_api.claim()
    no_api.finish("success", artifact_sha256="synthetic")
    full_run = Checkpoint(
        checkpoint_path(tmp_path, "full-run", "pl_001"),
        "config-a",
        "full-run",
    )
    assert full_run.claim() is True


@pytest.mark.parametrize("count", (2, 98))
def test_full_run_completion_rejects_partial_canonical_set(tmp_path: Path, count: int):
    expected = list(canonical_full100_episode_ids())
    for episode_id in expected[:count]:
        _write_full_run_case(tmp_path, episode_id)
    with pytest.raises(RuntimeError):
        validate_full_run_completion(
            tmp_path,
            expected_episode_ids=expected,
            run_config_hash=_full_run_config_hash(),
        )


def test_full_run_completion_accepts_exactly_closed_canonical_100(tmp_path: Path):
    expected = _write_canonical_full_run(tmp_path)
    rows = validate_full_run_completion(
        tmp_path,
        expected_episode_ids=expected,
        run_config_hash=_full_run_config_hash(),
    )
    assert len(rows) == 100


def test_full_run_completion_accepts_mixed_system_outcomes(tmp_path: Path):
    expected = _write_canonical_full_run(tmp_path)
    statuses = {
        expected[0]: ("root-binding-miss", "root-binding-miss"),
        expected[1]: ("executed", "bound"),
        expected[2]: ("input-invalid", "not-attempted"),
    }
    for episode_id, (runtime_status, root_status) in statuses.items():
        def mutate(artifact, runtime_status=runtime_status, root_status=root_status):
            artifact["runtime_status"] = runtime_status
            artifact["root_binding_status"] = root_status
            artifact["execution_complete"] = True
            artifact["case_success"] = False

        _rewrite_case_and_closure(tmp_path, episode_id, mutate)
    rows = validate_full_run_completion(
        tmp_path,
        expected_episode_ids=expected,
        run_config_hash=_full_run_config_hash(),
    )
    assert len(rows) == 100


def test_two_supplied_expected_ids_cannot_impersonate_full100(tmp_path: Path):
    expected = list(canonical_full100_episode_ids())[:2]
    for episode_id in expected:
        _write_full_run_case(tmp_path, episode_id)
    # The message now names the tier it actually expected and the two counts, so
    # a future mismatch says which task type/hop was compared instead of always
    # claiming "canonical 100" (which was wrong for a 90-episode Abs tier).
    with pytest.raises(RuntimeError, match=r"canonical Cas hop1 \(100\): got 2"):
        validate_full_run_completion(
            tmp_path,
            expected_episode_ids=expected,
            run_config_hash=_full_run_config_hash(),
        )


@pytest.mark.parametrize(
    "failure",
    (
        "extra_101",
        "synthetic",
        "duplicate_path",
        "orphan",
        "wrong_namespace",
        "symlink_escape",
        "missing_artifact",
        "missing_manifest",
        "missing_artifact_path",
        "bad_hash",
        "bad_manifest_hash",
        "malformed_artifact",
        "malformed_manifest",
        "malformed_checkpoint",
        "null_cost",
        "nan_cost",
        "inf_cost",
        "negative_cost",
        "incomplete_cost",
        "missing_cost_bucket",
        "stage_cost_mismatch",
    ),
)
def test_full_run_completion_fails_closed_on_all_anti_examples(
    tmp_path: Path, failure: str
):
    expected = _write_canonical_full_run(tmp_path)
    first, second = expected[:2]
    first_checkpoint_path = checkpoint_path(tmp_path, "full-run", first)
    second_checkpoint_path = checkpoint_path(tmp_path, "full-run", second)
    first_checkpoint = json.loads(first_checkpoint_path.read_text())
    second_checkpoint = json.loads(second_checkpoint_path.read_text())
    first_artifact = Path(first_checkpoint["artifact_path"])
    second_artifact = Path(second_checkpoint["artifact_path"])
    if failure == "extra_101":
        _write_full_run_case(tmp_path, "extra-episode")
    elif failure == "synthetic":
        _rewrite_case_and_closure(
            tmp_path,
            first,
            lambda artifact: artifact.__setitem__("synthetic_stub", True),
        )
    elif failure == "duplicate_path":
        second_checkpoint["artifact_path"] = str(first_artifact)
        second_checkpoint["artifact_sha256"] = hashlib.sha256(
            first_artifact.read_bytes()
        ).hexdigest()
        second_checkpoint_path.write_text(json.dumps(second_checkpoint))
    elif failure == "orphan":
        orphan = _artifact()
        orphan["episode_id"] = "orphan"
        write_case_artifact(tmp_path / "artifacts" / "full-run" / "orphan-paid", orphan)
    elif failure == "wrong_namespace":
        wrong = tmp_path / "artifacts" / "paid-smoke" / "wrong"
        write_case_artifact(wrong, json.loads(first_artifact.read_text()))
        first_checkpoint["artifact_path"] = str(wrong / "artifact.json")
        first_checkpoint["artifact_sha256"] = hashlib.sha256(
            (wrong / "artifact.json").read_bytes()
        ).hexdigest()
        first_checkpoint_path.write_text(json.dumps(first_checkpoint))
    elif failure == "symlink_escape":
        outside = tmp_path / "outside"
        write_case_artifact(outside, json.loads(first_artifact.read_text()))
        link = tmp_path / "artifacts" / "full-run" / "linked"
        link.symlink_to(outside, target_is_directory=True)
        first_checkpoint["artifact_path"] = str(link / "artifact.json")
        first_checkpoint["artifact_sha256"] = hashlib.sha256(
            (outside / "artifact.json").read_bytes()
        ).hexdigest()
        first_checkpoint_path.write_text(json.dumps(first_checkpoint))
    elif failure == "missing_artifact":
        second_artifact.unlink()
    elif failure == "missing_manifest":
        second_artifact.with_name("manifest.json").unlink()
    elif failure == "missing_artifact_path":
        second_checkpoint.pop("artifact_path")
        second_checkpoint_path.write_text(json.dumps(second_checkpoint))
    elif failure == "bad_hash":
        second_checkpoint["artifact_sha256"] = "bad"
        second_checkpoint_path.write_text(json.dumps(second_checkpoint))
    elif failure == "bad_manifest_hash":
        manifest_path = second_artifact.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text())
        manifest["artifact_sha256"] = "bad"
        manifest_path.write_text(json.dumps(manifest))
    elif failure == "malformed_artifact":
        second_artifact.write_text("{bad")
    elif failure == "malformed_manifest":
        second_artifact.with_name("manifest.json").write_text("{bad")
    elif failure == "malformed_checkpoint":
        second_checkpoint_path.write_text("{bad")
    else:

        def mutate_cost(artifact):
            if failure == "incomplete_cost":
                artifact["cost"]["cost_complete"] = False
            elif failure == "missing_cost_bucket":
                artifact["cost"]["paid_execution"]["layers"].pop("oracle_llm")
            elif failure == "stage_cost_mismatch":
                artifact["cost"]["paid_execution"]["known_usd"] += 1
            else:
                values = {
                    "null_cost": None,
                    "nan_cost": float("nan"),
                    "inf_cost": float("inf"),
                    "negative_cost": -1,
                }
                artifact["cost"]["known_usd"] = values[failure]

        _rewrite_case_and_closure(tmp_path, first, mutate_cost)
    with pytest.raises((RuntimeError, ValueError, KeyError)):
        validate_full_run_completion(
            tmp_path,
            expected_episode_ids=expected,
            run_config_hash=_full_run_config_hash(),
        )


def test_frozen_shared_and_v3_parse_from_snapshot_bytes(tmp_path: Path):
    shared = tmp_path / "shared"
    v3 = tmp_path / "v3"
    shared.mkdir()
    v3.mkdir()
    manifest = shared / "episode.json"
    manifest_bytes = b'{"episode_id":"ep","nodes":[],"alignments":[]}'
    manifest.write_bytes(manifest_bytes)
    (shared / "shared_manifest_index.json").write_text(
        json.dumps(
            {
                "episodes": [
                    {
                        "episode_id": "ep",
                        "manifest_path": "episode.json",
                        "manifest_file_sha256": hashlib.sha256(
                            manifest_bytes
                        ).hexdigest(),
                    }
                ]
            }
        )
    )
    case_success = v3 / "case_success.jsonl"
    case_success.write_text('{"episode_id":"ep"}\n')
    (v3 / "per_episode_cost.jsonl").write_text('{"episode_id":"ep"}\n')
    (v3 / "output_hashes.json").write_text("{}")
    corpus = FrozenV3(shared, v3)

    manifest.write_text('{"episode_id":"changed"}')
    case_success.write_text('{"episode_id":"changed"}\n')
    assert corpus.episode("ep")["episode_id"] == "ep"
    assert corpus.selections("ep")[0]["episode_id"] == "ep"
    dataset = tmp_path / "dataset.json"
    dataset.write_text("[]")
    with pytest.raises(RuntimeError, match="frozen input changed"):
        corpus.verify_identity(dataset)


def test_run_identity_closes_behavior_migrations_and_cost_inputs(tmp_path: Path):
    corpus = FrozenV3()
    price_path = tmp_path / "prices.json"
    prices = {"fixture": {"input_per_million": 1, "output_per_million": 2}}
    price_path.write_text(json.dumps(prices))
    identity = build_run_identity(
        corpus,
        models={"chat_model": "fixture", "embedding_model": "fixture-embed"},
        prices=prices,
        provider_config={"provider": "fixture", "api_key": "must-not-persist"},
        price_table_path=price_path,
    )
    roles = {entry["role"] for entry in identity["config"]["input_bundle"]}
    assert {
        "dataset",
        "shared_index",
        "shared_episode_manifest",
        "v3_selection",
        "v3_cost",
        "v3_output_hashes",
        "price_table",
        "runtime_behavior_source",
        "database_schema_migration",
    }.issubset(roles)
    serialized = canonical_json(identity["config"]).casefold()
    assert "must-not-persist" not in serialized
    assert all(
        {"path", "sha256", "byte_count", "role"}.issubset(entry)
        for entry in identity["config"]["input_bundle"]
    )
    verify_run_identity(identity, corpus=corpus)
    price_path.write_text('{"changed": true}')
    with pytest.raises(RuntimeError, match="input changed"):
        verify_run_identity(identity, corpus=corpus)


def test_identity_freezes_sanitized_provider_and_import_closure(tmp_path: Path):
    corpus = FrozenV3()
    provider_path = tmp_path / "providers.json"
    provider_doc = {
        "targets": [
            {
                "label": "fixture",
                "base_url": "https://fixture.invalid/v1",
                "api_mode": "openai-compatible",
                "timeout_seconds": 90,
                "retry_schedule_seconds": [60, 90, 120],
                "api_key": "must-never-persist",
            },
            {
                "label": "embedding",
                "base_url": "https://embedding.invalid/v1",
                "embedding_max_batch": 10,
                "api_key": "also-secret",
            },
        ]
    }
    provider_bytes = json.dumps(provider_doc).encode()
    provider_path.write_bytes(provider_bytes)
    prices = {"fixture": {"input_per_million": 1, "output_per_million": 2}}
    identity = build_run_identity(
        corpus,
        models={
            "chat_model": "fixture",
            "judge_model": "fixture",
            "p2_cheap_model": "fixture",
            "p2_strong_model": "fixture",
            "embedding_model": "fixture",
        },
        prices=prices,
        provider_config={"provider": "fixture", "embedding_provider": "embedding"},
        provider_config_path=provider_path,
        provider_config_bytes=provider_bytes,
    )
    bundle_paths = {entry["path"] for entry in identity["config"]["input_bundle"]}
    for required in (
        "integrations/memebench/embedding_retry.py",
        "src/contexthub/llm/chat_client.py",
        "src/contexthub/llm/openai_client.py",
        "src/contexthub/db/codecs.py",
        "integrations/memebench/common.py",
    ):
        assert required in bundle_paths
    serialized = canonical_json(identity["config"])
    assert "must-never-persist" not in serialized
    assert "also-secret" not in serialized
    assert "https://fixture.invalid/v1" in serialized
    assert '"present":true' in serialized
    changed = json.loads(provider_path.read_text())
    changed["targets"][0]["base_url"] = "https://changed.invalid/v1"
    provider_path.write_text(json.dumps(changed))
    with pytest.raises(RuntimeError, match="input changed"):
        verify_run_identity(identity, corpus=corpus)
    provider_path.write_bytes(provider_bytes)

    changed_identity = json.loads(json.dumps(identity))
    policy_entry = next(
        entry
        for entry in changed_identity["config"]["input_bundle"]
        if entry["path"] == "integrations/memebench/common.py"
    )
    changed_policy = tmp_path / "common.py"
    changed_policy.write_bytes(
        Path(policy_entry["resolved_path"]).read_bytes() + b"\n# changed\n"
    )
    policy_entry["resolved_path"] = str(changed_policy)
    changed_identity["run_config_hash"] = hashlib.sha256(
        canonical_json(changed_identity["config"]).encode()
    ).hexdigest()
    with pytest.raises(RuntimeError, match="input changed"):
        verify_run_identity(changed_identity, corpus=corpus)


@pytest.mark.parametrize(
    ("raw", "parsed"),
    (("CORRECT", "incorrect"), ("INCORRECT", "correct")),
)
def test_paid_validator_reparses_raw_judge_output(raw: str, parsed: str):
    artifact = _artifact(mode="paid-smoke")
    judge = artifact["judge"]["calls"][0]
    judge["raw_output"] = raw
    judge["parsed_verdict"] = parsed
    judge["fallback"] = parse_paid_judge_output(raw)[1]
    call = artifact["cost"]["calls"][1]
    call["response_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    call["response_bytes"] = len(raw.encode())
    artifact["cost"]["call_ledger_sha256"] = hashlib.sha256(
        canonical_json(artifact["cost"]["calls"]).encode()
    ).hexdigest()
    with pytest.raises(RuntimeError, match="raw output/parsed verdict mismatch"):
        validate_paid_cost(
            artifact["cost"],
            artifact=artifact,
            config=artifact["authorization"]["run_config"],
        )


@pytest.mark.asyncio
async def test_formal_run_rejects_limit_before_clients_are_built():
    args = build_parser().parse_args(["run", "--limit", "1"])
    with pytest.raises(ValueError, match="100 episodes"):
        await _main_async(args)


def test_preflight_context_keys_cover_every_non_boolean_entry():
    """A non-boolean preflight entry that is not declared as context would make
    `passed` false forever, since `passed` is `all(value is True)`. The Abs keys
    (task_type / evaluation_view_count / abs_excluded_count) hit exactly that.
    Reading a real preflight record keeps this honest as new keys appear."""
    recorded = json.loads(
        (
            Path("integrations/memebench/runs/full100_v3_p2_e2e_v3_development_20260827")
            / "preflight.json"
        ).read_text()
    )
    for key in ("task_type", "evaluation_view_count", "abs_excluded_count"):
        assert key in PREFLIGHT_CONTEXT_KEYS
    undeclared = sorted(
        key
        for key, value in recorded.items()
        if not isinstance(value, bool) and key not in PREFLIGHT_CONTEXT_KEYS
    )
    assert undeclared == []


@pytest.mark.parametrize("command", ("preflight", "smoke", "paid-case", "run"))
def test_task_type_and_notices_reach_every_command(command: str):
    """Every command must thread --task-type and --no-stale-notices into the run
    identity. `smoke` once dropped both, which silently ran a Cas workload under
    a directory marked Abs."""
    source = Path("integrations/memebench/run_full100_v3_p2.py").read_text()
    branch_marker = (
        'if args.command == "preflight":'
        if command == "preflight"
        else f'elif args.command == "{command}":'
        if command != "run"
        else "        else:\n            price_bytes = args.price_table.read_bytes()"
    )
    start = source.index(branch_marker)
    end = len(source)
    for later in ('elif args.command ==', "        else:\n            price_bytes"):
        found = source.find(later, start + len(branch_marker))
        if found != -1:
            end = min(end, found)
    branch = source[start:end]
    assert "task_type=args.task_type" in branch
    assert "with_stale_notices=args.with_stale_notices" in branch


def test_every_task_type_resolver_call_passes_task_type():
    """`resolve_runtime_episode_input` and `load_evaluation_annotations` default
    to task_type="Cas". The paid path once called the first without it, so it
    asked the Cas question while the contract expected the Abs one -- caught only
    downstream as `paid_query_unchanged: False`. A default that silently picks
    the wrong task type must not be reachable from any call site."""
    source = Path("integrations/memebench/run_full100_v3_p2.py").read_text()
    for resolver in (
        "resolve_runtime_episode_input",
        "load_evaluation_annotations",
        # Added 2026-09-02. Same class of defect, different function: the full-run
        # caller omitted task_type, so the end-of-run completeness check compared
        # the Abs tier's 90 episodes against the Cas canonical 100 and failed the
        # tier after all 90 cases had already been paid for and recorded.
        "validate_full_run_completion",
    ):
        for match in re.finditer(rf"(?<!def ){resolver}\(", source):
            depth = 0
            index = match.end() - 1
            for index in range(match.end() - 1, len(source)):
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            call = source[match.start():index + 1]
            assert "task_type=" in call, f"{resolver} call omits task_type: {call}"


def _empty_response_artifact():
    """A paid artifact whose answer call legitimately returned an empty string."""
    artifact = _artifact(mode="paid-smoke")
    cost = artifact["cost"]
    answer_call = cost["calls"][0]
    artifact["answers"]["on"]["raw_answer"] = ""
    answer_call["response_sha256"] = hashlib.sha256(b"").hexdigest()
    answer_call["response_bytes"] = 0
    answer_call["raw_output_present"] = False
    cost["call_ledger_sha256"] = hashlib.sha256(
        canonical_json(cost["calls"]).encode()
    ).hexdigest()
    return artifact


def test_paid_call_evidence_accepts_genuine_empty_completion():
    """A model told to answer only from the supplied notes returns an empty
    completion when the notes do not contain the asked-for fact: one stop token,
    real usage, no content. 2026-09-02 sw_027 hit exactly that -- the target node
    existed and was fresh but lost the top_k=8 draw, so the before/off arms had no
    note to answer from. Rejecting the empty response as "missing evidence" killed
    the whole 90-episode tier, which both discarded a real measurement and hid the
    retrieval miss behind a crash. An empty response is a result, not a gap."""
    artifact = _empty_response_artifact()
    validate_paid_cost(
        artifact["cost"],
        artifact=artifact,
        config=artifact["authorization"]["run_config"],
    )


@pytest.mark.parametrize(
    "failure",
    (
        "claims_output_without_bytes",
        "trace_nonempty_but_flag_false",
        "trace_empty_but_flag_true",
        "forged_zero_over_real_response",
    ),
)
def test_paid_call_evidence_still_rejects_empty_response_abuse(failure: str):
    """Allowing response_bytes == 0 must not become a way to launder a cost row.
    The trace cross-check recomputes hash and bytes from the recorded response, and
    raw_output_present is pinned to whether that response has content."""
    artifact = _empty_response_artifact()
    cost = artifact["cost"]
    call = cost["calls"][0]
    if failure == "claims_output_without_bytes":
        call["raw_output_present"] = True
    elif failure == "trace_nonempty_but_flag_false":
        artifact["answers"]["on"]["raw_answer"] = "real answer"
        call["response_sha256"] = hashlib.sha256(b"real answer").hexdigest()
        call["response_bytes"] = len(b"real answer")
    elif failure == "trace_empty_but_flag_true":
        artifact["answers"]["on"]["raw_answer"] = "   "
        call["response_sha256"] = hashlib.sha256(b"   ").hexdigest()
        call["response_bytes"] = 3
        call["raw_output_present"] = True
    elif failure == "forged_zero_over_real_response":
        # Ledger claims an empty response while the trace recorded real content.
        artifact["answers"]["on"]["raw_answer"] = "real answer"
    cost["call_ledger_sha256"] = hashlib.sha256(
        canonical_json(cost["calls"]).encode()
    ).hexdigest()
    with pytest.raises(RuntimeError):
        validate_paid_cost(
            cost,
            artifact=artifact,
            config=artifact["authorization"]["run_config"],
        )
