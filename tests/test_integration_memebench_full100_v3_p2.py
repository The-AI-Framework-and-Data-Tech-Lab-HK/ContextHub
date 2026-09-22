import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from contexthub.services.context_service import ContextService
from integrations.memebench.run_full100_v3_p2 import (
    FrozenV3,
    RetrievalEvidenceContract,
    RootIdentity,
    TRACE_SECTIONS,
    apply_root_identity_change,
    import_frozen_graph,
    production_no_api_stage,
    resolve_root_workload,
    run_no_api_smoke,
    run_smoke_case,
    validate_stale_isolation_retrieval_contract,
)


@pytest.mark.asyncio
async def test_two_case_no_api_smoke_exports_before_cleanup(
    repo, db_pool, clean_db, tmp_path
):
    system = SimpleNamespace(pool=db_pool, repo=repo, rule_registry=None)
    result = await run_no_api_smoke(system, tmp_path / "smoke", limit=2)
    assert result["completed"] == ["pl_001", "pl_002"]
    assert result["all_trace_sections_nonempty"]
    for episode_id in result["completed"]:
        artifact_path = tmp_path / "smoke" / "cases" / episode_id / "artifact.json"
        artifact_text = artifact_path.read_text(encoding="utf-8")
        artifact = json.loads(artifact_text)
        assert all(f'"{section}"' in artifact_text for section in TRACE_SECTIONS)
        workload = artifact["workload"]["runtime_safe_fields"]
        root_events = [
            row
            for row in artifact["p2_queue"]["events"]
            if row["event_id"] in workload["event_ids"]
        ]
        assert {row["context_id"] for row in root_events} == set(
            workload["root_context_ids"]
        )
        assert all(row["source_version"] == 2 for row in root_events)
        assert all(row["new_version"] == 2 for row in workload["alias_updates"])
        assert {row["root_group_id"] for row in workload["alias_updates"]} == {
            workload["root_group_id"]
        }
        off = artifact["answers"]["off"]
        on = artifact["answers"]["on"]
        assert off["retrieval"]["retrieval_id"]
        assert on["retrieval"]["retrieval_id"]
        assert (
            off["retrieval"]["final_materialized"]
            != on["retrieval"]["final_materialized"]
        )
        assert off["retrieval"]["context_hash"] != on["retrieval"]["context_hash"]
        assert off["prompt_sha256"] != on["prompt_sha256"]
        assert artifact["production_retrieval_change_verified"][
            "integrity_complete"
        ]
        contract = artifact["retrieval_evidence_contract"]
        assert off["retrieval"]["request"]["query"] == contract["after_question"]
        assert workload["before"] not in off["retrieval"]["request"]["query"]
        assert workload["after"] not in off["retrieval"]["request"]["query"]
        assert artifact["capability_observations"]["durable_invalidation_observed"]
        assert not artifact["capability_observations"]["semantic_recompute_observed"]
        assert not artifact["capability_observations"][
            "receding_horizon_recompute_observed"
        ]
        lowered_artifact = artifact_text.casefold()
        assert '"certified"' not in lowered_artifact
        assert '"certification' not in lowered_artifact
        assert artifact["p2_edges"]["capability"] == "invalidation-only fail-closed"
        assert artifact["state_evidence"]["on"]["invalidations"]
    async with db_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                """
            SELECT COUNT(*) FROM contexts
             WHERE account_id LIKE 'meme-v3-smoke-%'
            """
            )
            == 0
        )


@pytest.mark.asyncio
async def test_unchanged_production_retrieval_is_recorded_as_system_miss(
    repo, db_pool, clean_db
):
    system = SimpleNamespace(pool=db_pool, repo=repo, rule_registry=None)
    account = "meme-retrieval-no-change"
    async with repo.session(account) as db:
        await db.execute(
            """
            INSERT INTO contexts (
              uri, context_type, scope, owner_space, account_id,
              l0_content, l1_content, l2_content
            )
            VALUES (
              'ctx://agent/eval-agent/memories/no-change', 'memory', 'agent',
              'eval-agent', $1, 'new value', 'new value', 'new value'
            )
            """,
            account,
        )
    off = await production_no_api_stage(
        system, stage_name="off", account=account, query="new value"
    )
    on = await production_no_api_stage(
        system, stage_name="on", account=account, query="new value"
    )
    result = validate_stale_isolation_retrieval_contract(
        off,
        on,
        contract=RetrievalEvidenceContract(
            before_question="new value",
            after_question="new value",
            root_group_id="root-group",
            old_identity_id="old-identity",
            old_target_node_ids=("old",),
            replacement_identity_id="replacement-identity",
            replacement_node_ids=("replacement",),
            old_reference="old",
            replacement_reference="new",
        ),
        state_evidence={"contexts": [], "invalidations": []},
        root_context_ids=("root",),
    )
    assert result["integrity_complete"]
    assert not result["system_outcome_passed"]


@pytest.mark.asyncio
async def test_pl033_direct_stale_isolated_without_pass_through_recompute(
    repo, db_pool, clean_db, tmp_path
):
    system = SimpleNamespace(pool=db_pool, repo=repo, rule_registry=None)
    observed_stale = None

    async def stage(stage_name: str, account: str):
        nonlocal observed_stale
        result = await production_no_api_stage(
            system,
            stage_name=stage_name,
            account=account,
            query="What insurance do I have?",
        )
        if stage_name == "on":
            async with db_pool.acquire() as conn:
                observed_stale = await conn.fetchrow(
                    """
                    SELECT status, validity_status, version
                      FROM contexts
                     WHERE account_id = $1
                       AND l2_content LIKE '%dental + vision insurance bundle%'
                    """,
                    account,
                )
        return result

    artifact_path = await run_smoke_case(
        system,
        FrozenV3(),
        "pl_033",
        tmp_path / "pl033",
        stage_callback=stage,
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert observed_stale is not None
    assert observed_stale["status"] == "stale"
    assert observed_stale["validity_status"] in {"stale", "invalid"}
    assert observed_stale["version"] == 1

    on = artifact["answers"]["on"]
    visible = "\n".join(
        row["service_content"] for row in on["retrieval"]["final_materialized"]
    )
    assert "dental + vision insurance bundle" not in visible
    assert "[Upstream dependency update]" not in visible
    assert all(
        row["status"] == "active" and row["validity_status"] == "fresh"
        for row in on["retrieval"]["final_materialized"]
    )
    assert artifact["p2_edges"]["frontier_complete"] is True
    assert artifact["p2_edges"]["invalidation_frontier_complete"] is True
    assert artifact["p2_edges"]["receding_horizon_recompute_observed"] is False
    old_context_id = artifact["retrieval_evidence_contract"]["old_target_context_ids"][
        0
    ]
    old_state = next(
        row
        for row in artifact["state_evidence"]["on"]["contexts"]
        if row["node_id"] == old_context_id
    )
    assert old_state["status"] == "stale"
    assert old_state["validity_status"] in {"stale", "invalid"}
    assert any(
        row["context_id"] == old_context_id and row["resolution_status"] == "unresolved"
        for row in artifact["state_evidence"]["on"]["invalidations"]
    )
    assert artifact["p2_queue"]["unfinished"] == []


@pytest.mark.asyncio
async def test_pl016_multi_alias_offline_mechanism_smoke(
    repo, db_pool, clean_db, tmp_path
):
    system = SimpleNamespace(pool=db_pool, repo=repo, rule_registry=None)
    artifact_path = await run_smoke_case(
        system,
        FrozenV3(),
        "pl_016",
        tmp_path / "pl016",
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    workload = artifact["workload"]["runtime_safe_fields"]
    aliases = workload["root_identity"]["aliases"]
    assert {alias["text"] for alias in aliases} == {
        "The user is engaged.",
        "I am engaged.",
    }
    assert len(workload["alias_updates"]) == len(aliases) == 2
    assert {row["new_version"] for row in workload["alias_updates"]} == {2}
    assert {row["root_group_id"] for row in workload["alias_updates"]} == {
        workload["root_group_id"]
    }
    contract = artifact["retrieval_evidence_contract"]
    assert contract["root_group_id"] == workload["root_group_id"]
    assert contract["old_target_node_ids"] == [
        "node-11fe7d7a7b3cbebe8200c064fbfe66c05cf7d08b536f4393b5d27399ac1cb42d"
    ]
    expected = artifact["p2_edges"]["expected_reachable_frontier"]
    old_context_id = contract["old_target_context_ids"][0]
    assert any(edge.endswith(f"->{old_context_id}") for edge in expected)
    assert artifact["p2_edges"]["frontier_complete"]
    assert artifact["p2_edges"]["root_alias_union"]["union_root_targets"]
    assert artifact["stale_isolation_retrieval_verified"]["integrity_complete"]


@pytest.mark.asyncio
async def test_multi_alias_batch_rolls_back_on_partial_failure_and_isolates_account(
    repo, db_pool, clean_db, monkeypatch
):
    corpus = FrozenV3()
    workload = resolve_root_workload(corpus, "pl_016")
    episode = corpus.episode("pl_016")
    account = "root-alias-atomic"
    other_account = "root-alias-other"
    other_id = uuid4()
    async with repo.session(other_account) as db:
        await db.execute(
            """
            INSERT INTO contexts (
              id, uri, context_type, scope, owner_space, account_id,
              l0_content, l1_content, l2_content
            )
            VALUES (
              $1, 'ctx://agent/eval-agent/memories/other',
              'memory', 'agent', 'eval-agent', $2,
              'other', 'other', 'other'
            )
            """,
            other_id,
            other_account,
        )
    async with repo.session(account) as db:
        await import_frozen_graph(
            db,
            account,
            episode,
            corpus.selected_edges("pl_016"),
            root_identity=workload.root_identity,
        )

    original = ContextService.apply_system_content_change
    call_count = 0

    async def fail_second(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("injected second-alias CAS failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        ContextService,
        "apply_system_content_change",
        staticmethod(fail_second),
    )
    with pytest.raises(RuntimeError, match="second-alias CAS failure"):
        async with repo.session(account) as db:
            await apply_root_identity_change(
                db,
                workload,
                expected_versions={
                    context_id: 1
                    for context_id in workload.root_identity.alias_context_ids
                },
            )

    async with repo.session(account) as db:
        alias_rows = await db.fetch(
            """
            SELECT id::text AS context_id, version, l2_content
              FROM contexts
             WHERE id = ANY($1::uuid[])
             ORDER BY id
            """,
            [alias.context_id for alias in workload.root_identity.aliases],
        )
        event_count = await db.fetchval("SELECT COUNT(*) FROM change_events")
    assert call_count == 2
    assert {int(row["version"]) for row in alias_rows} == {1}
    assert all(
        workload.before.casefold() in str(row["l2_content"]).casefold()
        for row in alias_rows
    )
    assert event_count == 0
    async with repo.session(other_account) as db:
        other = await db.fetchrow(
            "SELECT version, l2_content FROM contexts WHERE id = $1",
            other_id,
        )
    assert int(other["version"]) == 1
    assert other["l2_content"] == "other"


@pytest.mark.asyncio
async def test_root_binding_miss_fallback_runs_off_on_retrieval(
    repo, db_pool, clean_db, tmp_path, monkeypatch
):
    system = SimpleNamespace(pool=db_pool, repo=repo, rule_registry=None)
    original = FrozenV3()
    resolved = __import__(
        "integrations.memebench.run_full100_v3_p2",
        fromlist=["resolve_root_identity"],
    )
    real_identity = resolved.resolve_root_identity(original, "pl_001")
    missing = RootIdentity(
        episode_id=real_identity.episode_id,
        entity=real_identity.entity,
        normalized_before=real_identity.normalized_before,
        normalized_after=real_identity.normalized_after,
        group_id=real_identity.group_id,
        change_id=real_identity.change_id,
        aliases=(),
        applicability="root_binding_miss",
        ambiguity_status="ambiguous",
        ambiguity_reasons=("synthetic-unbound",),
        direct_candidates=(),
        ambiguous_candidates=real_identity.direct_candidates,
        rejected_reason_clause_candidates=(
            real_identity.rejected_reason_clause_candidates
        ),
    )
    monkeypatch.setattr(resolved, "resolve_root_identity", lambda *_a, **_k: missing)
    artifact_path = await run_smoke_case(
        system,
        original,
        "pl_001",
        tmp_path / "root-miss",
    )
    artifact = json.loads(artifact_path.read_text())
    assert artifact["execution_complete"]
    assert artifact["runtime_status"] == "root-binding-miss"
    assert artifact["root_binding_status"] == "root-binding-miss"
    assert artifact["frontier_status"] == "empty-no-binding"
    fallback = artifact["workload"]["runtime_safe_fields"]["fallback"]
    assert fallback and fallback[0]["old_root_nodes_modified"] is False
    assert not artifact["p2_queue"]["root_event_ids"]
    assert artifact["retrieval"]["off"]["retrieval_id"]
    assert artifact["retrieval"]["on"]["retrieval_id"]
