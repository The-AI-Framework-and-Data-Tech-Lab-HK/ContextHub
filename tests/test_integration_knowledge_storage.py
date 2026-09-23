"""S2 DB contracts. ONLY explicit isolated S2_TEST_DATABASE_URL is accepted.

Never requests db_pool/clean_db from the legacy conftest. No TRUNCATE.
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import (
    ContextVersion,
    ContractError,
    GraphSnapshot,
    Proposition,
    PropositionArtifact,
    SealRef,
    canonical_hash,
)
from contexthub.models.maintenance import (
    Candidate,
    CheckItem,
    CommitProof,
    EvidenceBundle,
    HardCheckResult,
    MaintenanceSnapshot,
    ReadEntry,
    VerificationResult,
    WorkState,
)
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.evidence_service import EvidenceService
from contexthub.services.execution_ledger import ExecutionLedger
from knowledge_helpers import HASH, identity, policy


@pytest_asyncio.fixture
async def s2_repo():
    url = os.environ.get("S2_TEST_DATABASE_URL")
    if not url:
        pytest.fail("S2_TEST_DATABASE_URL required; refusing production fallback")
    parsed = urlparse(url)
    if (
        parsed.hostname not in ("localhost", "127.0.0.1")
        or parsed.port in (None, 5432)
        or not parsed.path.startswith("/contexthub_s2")
        or not (parsed.username or "").startswith("s2_")
    ):
        pytest.fail(
            "S2 requires explicit local non-5432 contexthub_s2 database and s2_ role"
        )
    pool = await asyncpg.create_pool(
        url, min_size=1, max_size=5, init=init_pg_connection
    )
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            )
            assert not row["rolsuper"] and not row["rolbypassrls"]
            assert (
                await conn.fetchval("SELECT version_num FROM alembic_version") == "010"
            )
            assert (
                await conn.fetchval("SELECT to_regclass('propagation_recompute_work')")
                is None
            )
        yield PgRepository(pool)
    finally:
        await pool.close()


async def seed(db, account):
    source, target = uuid4(), uuid4()
    for ident, text in [(source, "政策：上限500元。"), (target, "450元可以报销。")]:
        await db.execute(
            """INSERT INTO contexts(id,account_id,uri,context_type,scope,l2_content)
            VALUES($1,$2,$3,'memory','user',$4)""",
            ident,
            account,
            "ctx://" + str(ident),
            text,
        )
    event = uuid4()
    await db.execute(
        """INSERT INTO change_events(event_id,context_id,account_id,change_type,actor)
        VALUES($1,$2,$3,'modified','s2-test')""",
        event,
        source,
        account,
    )
    return (
        ContextVersion(context_id=source, version=1),
        ContextVersion(context_id=target, version=1),
        event,
    )


async def artifact_chain(db, account):
    artifacts = ArtifactService()
    evidence = EvidenceService(artifacts)
    source, target, event = await seed(db, account)
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    src = await evidence.snapshot(
        db,
        account_id=account,
        source=source,
        content_level="l2_content",
        cutoff_at=cutoff,
    )
    ref = await evidence.reference(
        db, src, start=0, end=10, provenance=("user-content",)
    )
    prop = PropositionArtifact(
        account_id=account,
        id=uuid4(),
        sources=(src,),
        extraction_config_id="fixture",
        extraction_config_sha256=HASH,
        status="complete",
        propositions=(Proposition(id=uuid4(), text="上限500元", evidence=(ref,)),),
    )
    prop_ref = await artifacts.seal(db, prop)
    graph = await artifacts.seal(
        db,
        GraphSnapshot(
            account_id=account,
            id=uuid4(),
            graph_version=1,
            nodes=(source, target),
            config_sha256=HASH,
        ),
    )
    maintenance = await artifacts.seal(
        db,
        MaintenanceSnapshot(
            account_id=account,
            id=uuid4(),
            event_id=event,
            target=target,
            necessary_upstreams=(source,),
            cause_ids=(event,),
            graph=graph,
            allowed_sources=(src,),
            cutoff_at=cutoff,
            policy_sha256=HASH,
        ),
    )
    entry = ReadEntry(evidence=ref, ordinal=0, page=0, chunk=0)
    bundle = await artifacts.seal(
        db,
        EvidenceBundle(
            account_id=account,
            id=uuid4(),
            maintenance=maintenance,
            scope_id="local",
            required=(entry,),
            read=(entry,),
            complete=True,
            truncated=False,
            ordering="ordinal",
            page_size=10,
            chunk_size=100,
        ),
    )
    candidate = await artifacts.seal(
        db,
        Candidate(
            account_id=account,
            id=uuid4(),
            maintenance=maintenance,
            bundle=bundle,
            outcome="unchanged",
            proposed_content="450元可以报销。",
            evidence=(ref,),
            generator_call_id=uuid4(),
            generator_config_sha256=HASH,
        ),
    )
    item = CheckItem(code="fixture", verdict="PASS", reason="synthetic")
    hard = await artifacts.seal(
        db,
        HardCheckResult(
            account_id=account,
            id=uuid4(),
            candidate=candidate,
            checker_version="fixture",
            checks=(item,),
        ),
    )
    verification = await artifacts.seal(
        db,
        VerificationResult(
            account_id=account,
            id=uuid4(),
            candidate=candidate,
            bundle=bundle,
            verifier_call_id=uuid4(),
            verifier_config_sha256=HASH,
            applicability=item,
            complete_support=item,
            conflicts_resolved=item,
        ),
    )
    proof = CommitProof(
        account_id=account,
        id=uuid4(),
        candidate=candidate,
        hard_check=hard,
        verification=verification,
        maintenance=maintenance,
        committed_target=target,
        checked_upstreams=(source,),
        checked_sources=(src,),
        cleared_cause_ids=(event,),
        committed_at=cutoff,
        commit_code_version="synthetic-receipt-only",
    )
    return locals()


@pytest.mark.asyncio
async def test_roundtrip_all_artifacts_work_and_receipt(s2_repo):
    account = "s2-" + str(uuid4())
    a = ArtifactService()
    async with s2_repo.session(account) as db:
        chain = await artifact_chain(db, account)
        for key in (
            "src",
            "prop_ref",
            "graph",
            "maintenance",
            "bundle",
            "candidate",
            "hard",
            "verification",
        ):
            ref = chain[key]
            obj = await a.get(db, ref)
            assert obj.reference() == ref
            assert await a.seal(db, obj) == ref
        proof_ref = await a.archive_commit_proof(db, chain["proof"])
        assert (await a.get(db, proof_ref)) == chain["proof"]
        assert await a.archive_commit_proof(db, chain["proof"]) == proof_ref
        with pytest.raises(ContractError, match="commit_proof_requires_commit_path"):
            await a.seal(db, chain["proof"])
        work = uuid4()
        await a.create_work(db, work, chain["maintenance"])
        await a.update_work(
            db, account, work, 1, WorkState(revision=2, status="running")
        )
        await a.update_work(
            db,
            account,
            work,
            2,
            WorkState(
                revision=3, status="unresolved", reason="required_source_missing"
            ),
        )
        assert (await a.get_work(db, account, work)).status == "unresolved"
        assert (
            await db.fetchval(
                "SELECT validity_status FROM contexts WHERE id=$1",
                chain["target"].context_id,
            )
            == "fresh"
        )
        with pytest.raises(ContractError, match="work_revision_conflict"):
            await a.update_work(
                db, account, work, 2, WorkState(revision=3, status="running")
            )


@pytest.mark.asyncio
async def test_source_errors_old_version_and_hash(s2_repo):
    account = "s2-" + str(uuid4())
    a = ArtifactService()
    e = EvidenceService(a)
    async with s2_repo.session(account) as db:
        source, _, _ = await seed(db, account)
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
        ref = await e.snapshot(
            db,
            account_id=account,
            source=source,
            content_level="l2_content",
            cutoff_at=cutoff,
        )
        ev = await e.reference(db, ref, start=0, end=10, provenance=("test",))
        with pytest.raises(ContractError, match="invalid_span"):
            await e.reference(db, ref, start=0, end=999, provenance=("test",))
        with pytest.raises(ContractError, match="source_content_missing"):
            await e.snapshot(
                db,
                account_id=account,
                source=source,
                content_level="l0_content",
                cutoff_at=cutoff,
            )
        with pytest.raises(ContractError, match="source_version_missing"):
            await e.snapshot(
                db,
                account_id=account,
                source=source.model_copy(update={"version": 99}),
                content_level="l2_content",
                cutoff_at=cutoff,
            )
        obj = await a.get(db, ref)
        with pytest.raises(ContractError, match="source_version_mismatch"):
            await a.seal(db, obj.model_copy(update={"content_sha256": "0" * 64}))
        with pytest.raises(ContractError, match="artifact_hash_mismatch"):
            await a.seal(
                db, obj.model_copy(update={"cutoff_at": cutoff + timedelta(seconds=1)})
            )
        with pytest.raises(ValueError, match="source_after_cutoff"):
            await e.snapshot(
                db,
                account_id=account,
                source=source,
                content_level="l2_content",
                cutoff_at=cutoff - timedelta(days=1),
            )
        await db.execute(
            "UPDATE contexts SET l2_content=$2,version=2 WHERE id=$1",
            source.context_id,
            "新上限400元",
        )
        assert await e.resolve(db, ev) == "政策：上限500元。"
        wrong = ev.model_copy(update={"start": 1, "end": 11})
        with pytest.raises(ContractError, match="source_span_mismatch"):
            await e.resolve(db, wrong)


@pytest.mark.asyncio
async def test_tenant_isolation_and_append_only_sql(s2_repo):
    account = "s2-" + str(uuid4())
    other = "s2-" + str(uuid4())
    a = ArtifactService()
    async with s2_repo.session(account) as db:
        chain = await artifact_chain(db, account)
        await a.archive_commit_proof(db, chain["proof"])
        await a.create_work(db, uuid4(), chain["maintenance"])
    async with s2_repo.session(other) as db:
        for table in (
            "knowledge_artifacts",
            "context_refresh_proofs",
            "maintenance_work_items",
        ):
            assert (
                await db.fetchval(
                    f"SELECT count(*) FROM {table} WHERE account_id=$1", account
                )
                == 0
            )
        with pytest.raises(ContractError, match="tenant_mismatch"):
            await a.get(db, chain["src"])
        with pytest.raises(ContractError, match="artifact_missing"):
            await a.get(db, chain["src"].model_copy(update={"account_id": other}))
        with pytest.raises(ContractError, match="source_version_missing"):
            await EvidenceService().snapshot(
                db,
                account_id=other,
                source=chain["source"],
                content_level="l2_content",
                cutoff_at=chain["cutoff"],
            )
    for table in ("knowledge_artifacts", "context_refresh_proofs"):
        for verb in ("UPDATE", "DELETE"):
            with pytest.raises(asyncpg.CheckViolationError, match="append_only"):
                async with s2_repo.session(account) as db:
                    sql = (
                        f"UPDATE {table} SET sha256=sha256 WHERE account_id=$1"
                        if verb == "UPDATE"
                        else f"DELETE FROM {table} WHERE account_id=$1"
                    )
                    await db.execute(sql, account)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with s2_repo.session(other) as db:
            obj = chain["prop"]
            await db.execute(
                "INSERT INTO knowledge_artifacts(account_id,id,kind,schema_version,sha256,payload) VALUES($1,$2,$3,1,$4,$5)",
                account,
                uuid4(),
                "propositions",
                HASH,
                obj.model_dump(mode="json"),
            )


@pytest.mark.asyncio
async def test_reference_mismatch_and_self_verification(s2_repo):
    account = "s2-" + str(uuid4())
    a = ArtifactService()
    async with s2_repo.session(account) as db:
        c = await artifact_chain(db, account)
        candidate = await a.get(db, c["candidate"])
        verification = await a.get(db, c["verification"])
        with pytest.raises(ContractError, match="generator_cannot_verify"):
            await a.seal(
                db,
                verification.model_copy(
                    update={
                        "id": uuid4(),
                        "verifier_call_id": candidate.generator_call_id,
                    }
                ),
            )
        with pytest.raises(ContractError, match="artifact_missing"):
            await a.get(db, c["src"].model_copy(update={"id": uuid4()}))
        with pytest.raises(ContractError, match="proof_version_mismatch"):
            await a.archive_commit_proof(
                db, c["proof"].model_copy(update={"cleared_cause_ids": ()})
            )
        failed = verification.model_copy(
            update={
                "id": uuid4(),
                "applicability": CheckItem(
                    code="applicability", verdict="UNKNOWN", reason="missing"
                ),
            }
        )
        failed_ref = await a.seal(db, failed)
        with pytest.raises(ContractError, match="proof_checks_not_passed"):
            await a.archive_commit_proof(
                db,
                c["proof"].model_copy(
                    update={"id": uuid4(), "verification": failed_ref}
                ),
            )


@pytest.mark.asyncio
async def test_transaction_rollback(s2_repo):
    account = "s2-" + str(uuid4())
    a = ArtifactService()
    ref = None
    with pytest.raises(RuntimeError):
        async with s2_repo.session(account) as db:
            c = await artifact_chain(db, account)
            ref = c["src"]
            raise RuntimeError("rollback")
    async with s2_repo.session(account) as db:
        with pytest.raises(ContractError, match="artifact_missing"):
            await a.get(db, ref)


@pytest.mark.asyncio
async def test_durable_ledger_concurrent_calls_exhaustion_and_rollback(s2_repo):
    ledger = ExecutionLedger(s2_repo)
    a, b = identity(), identity()
    a = a.model_copy(update={"account_id": "s2-" + str(uuid4())})
    b = b.model_copy(update={"account_id": a.account_id})

    async def transport(request):
        import json

        value = json.loads(request.content)["messages"][0]["content"]
        await asyncio.sleep(0.001)
        return httpx.Response(
            503 if value == "fail" else 200,
            request=request,
            json={
                "model": "fixture-snapshot",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 3 if value == "fail" else 19,
                    "completion_tokens": 1,
                },
            },
        )

    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(transport)
    )
    try:
        results = await asyncio.gather(
            client.complete("fail", call=RecordedCall(a, policy(), ledger)),
            client.complete("ok", call=RecordedCall(b, policy(), ledger)),
            return_exceptions=True,
        )
        assert isinstance(results[0], httpx.HTTPStatusError) and results[1] == "ok"
        rows = await ledger.records(a.account_id, a.call_id)
        assert len(rows) == 6 and rows[-1].stop_reason == "retry_exhausted"
        assert [r.usage.input_tokens for r in rows if r.phase == "finished"] == [
            3,
            3,
            3,
        ]
        assert (await ledger.records(b.account_id, b.call_id))[
            -1
        ].usage.input_tokens == 19
        with pytest.raises(ContractError):
            await client.complete("fail", call=RecordedCall(a, policy(), ledger))
        assert len(await ledger.records(a.account_id, a.call_id)) == 6
        assert await ledger.records("s2-other", a.call_id) == ()
        await ledger.append(rows[-1])  # exact final duplicate is idempotent
        with pytest.raises(ContractError, match="attempt_already_recorded"):
            await ledger.append(rows[-1].model_copy(update={"request_id": "different"}))
        with pytest.raises(asyncpg.CheckViolationError):
            async with s2_repo.session(a.account_id) as db:
                await db.execute(
                    "DELETE FROM execution_attempts WHERE call_id=$1", a.call_id
                )
        c = identity().model_copy(update={"account_id": a.account_id})
        with pytest.raises(RuntimeError):
            async with s2_repo.session(a.account_id):
                await client.complete("ok", call=RecordedCall(c, policy(), ledger))
                raise RuntimeError("outer transaction fails")
        assert len(await ledger.records(c.account_id, c.call_id)) == 2
        d = identity().model_copy(update={"account_id": a.account_id})
        call = RecordedCall(d, policy(), ledger)
        same = await asyncio.gather(
            client.complete("ok", call=call),
            client.complete("ok", call=call),
            return_exceptions=True,
        )
        assert sum(isinstance(r, ContractError) for r in same) == 1
        assert len(await ledger.records(d.account_id, d.call_id)) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_seal_identity_conflicts_and_source_drift(s2_repo):
    account = "s2-" + str(uuid4())
    a = ArtifactService()
    async with s2_repo.session(account) as db:
        c = await artifact_chain(db, account)
        original = await a.get(db, c["prop_ref"])
    shared = original.model_copy(update={"id": uuid4()})
    altered = shared.model_copy(update={"extraction_config_id": "other-config"})

    async def seal(obj):
        async with s2_repo.session(account) as db:
            return await a.seal(db, obj)

    results = await asyncio.gather(seal(shared), seal(altered), return_exceptions=True)
    assert sum(isinstance(r, ContractError) for r in results) == 1
    assert sum(isinstance(r, SealRef) for r in results) == 1
    async with s2_repo.session(account) as db:
        # Common program error: overwrite text without advancing version.
        # Existing 008 snapshot trigger reflects the change on validity update.
        await db.execute(
            "UPDATE contexts SET l2_content='unexpected edit',validity_status='stale' WHERE id=$1",
            c["source"].context_id,
        )
        with pytest.raises(ContractError, match="source_version_mismatch"):
            await EvidenceService().resolve(db, c["ref"])


@pytest.mark.asyncio
async def test_missing_event_scope_bundle_and_cross_tenant_references(s2_repo):
    account = "s2-" + str(uuid4())
    a = ArtifactService()
    async with s2_repo.session(account) as db:
        c = await artifact_chain(db, account)
        snapshot = await a.get(db, c["maintenance"])
        with pytest.raises(ContractError, match="event_missing"):
            await a.seal(
                db, snapshot.model_copy(update={"id": uuid4(), "event_id": uuid4()})
            )
        with pytest.raises(ContractError, match="tenant_mismatch"):
            await a.seal(
                db,
                c["prop"].model_copy(
                    update={
                        "id": uuid4(),
                        "sources": (
                            c["src"].model_copy(update={"account_id": "other"}),
                        ),
                        "propositions": (),
                    }
                ),
            )
        narrower = await a.seal(
            db, snapshot.model_copy(update={"id": uuid4(), "allowed_sources": ()})
        )
        bundle = await a.get(db, c["bundle"])
        with pytest.raises(ContractError, match="source_outside_scope"):
            await a.seal(
                db, bundle.model_copy(update={"id": uuid4(), "maintenance": narrower})
            )
        candidate = await a.get(db, c["candidate"])
        with pytest.raises(ContractError, match="maintenance_reference_mismatch"):
            await a.seal(
                db,
                candidate.model_copy(update={"id": uuid4(), "maintenance": narrower}),
            )
        work = uuid4()
        await a.create_work(db, work, c["maintenance"])
        assert (await a.create_work(db, work, c["maintenance"])).revision == 1
        with pytest.raises(ContractError, match="work_identity_conflict"):
            await a.create_work(db, uuid4(), c["maintenance"])
        with pytest.raises(ContractError, match="work_identity_conflict"):
            await a.create_work(db, work, narrower)


@pytest.mark.asyncio
async def test_call_context_and_cause_references_are_tenant_scoped(s2_repo):
    account = "s2-" + str(uuid4())
    other = "s2-" + str(uuid4())
    a = ArtifactService()
    async with s2_repo.session(account) as db:
        c = await artifact_chain(db, account)
        snapshot = await a.get(db, c["maintenance"])
        with pytest.raises(ContractError, match="cause_event_missing"):
            await a.seal(
                db, snapshot.model_copy(update={"id": uuid4(), "cause_ids": (uuid4(),)})
            )
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(
            200, request=request, json={"choices": [{"message": {"content": "ok"}}]}
        )

    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    ledger = ExecutionLedger(s2_repo)
    try:
        foreign = identity().model_copy(
            update={"account_id": other, "event_id": c["event"]}
        )
        with pytest.raises(ContractError, match="event_missing"):
            await client.complete("x", call=RecordedCall(foreign, policy(), ledger))
        missing = identity().model_copy(
            update={"account_id": account, "work_item_id": uuid4()}
        )
        with pytest.raises(ContractError, match="work_missing"):
            await client.complete("x", call=RecordedCall(missing, policy(), ledger))
        assert sent == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_work_cas_concurrency_and_ledger_raw_rls(s2_repo):
    account = "s2-" + str(uuid4())
    other = "s2-" + str(uuid4())
    a = ArtifactService()
    work = uuid4()
    async with s2_repo.session(account) as db:
        c = await artifact_chain(db, account)
        await a.create_work(db, work, c["maintenance"])

    async def update():
        async with s2_repo.session(account) as db:
            return await a.update_work(
                db, account, work, 1, WorkState(revision=2, status="running")
            )

    result = await asyncio.gather(update(), update(), return_exceptions=True)
    assert sum(isinstance(r, ContractError) for r in result) == 1
    assert sum(isinstance(r, WorkState) for r in result) == 1
    ledger = ExecutionLedger(s2_repo)
    call_id = identity().model_copy(
        update={"account_id": account, "work_item_id": work, "event_id": c["event"]}
    )
    client = OpenAIChatClient(
        "test",
        model="fixture",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, request=request, json={"choices": [{"message": {"content": "ok"}}]}
            )
        ),
    )
    try:
        await client.complete("x", call=RecordedCall(call_id, policy(), ledger))
    finally:
        await client.close()
    rows = await ledger.records(account, call_id.call_id)
    async with s2_repo.session(other) as db:
        assert (
            await db.fetchval(
                "SELECT count(*) FROM execution_attempts WHERE account_id=$1", account
            )
            == 0
        )
        assert (
            await db.execute(
                "DELETE FROM execution_attempts WHERE account_id=$1", account
            )
            == "DELETE 0"
        )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with s2_repo.session(other) as db:
            await db.execute(
                """INSERT INTO execution_attempts(account_id,call_id,attempt_no,phase,execution_id,sha256,payload)
                VALUES($1,$2,1,'started',$3,$4,$5)""",
                account,
                uuid4(),
                call_id.execution_id,
                canonical_hash(rows[0]),
                rows[0].model_dump(mode="json"),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["", None, "provider-request-123"])
@pytest.mark.parametrize("status", [200, 503])
async def test_optional_request_id_preserves_durable_usage(s2_repo, request_id, status):
    ledger = ExecutionLedger(s2_repo)
    call_identity = identity().model_copy(update={"account_id": "s2-" + str(uuid4())})
    sent = []

    def handle(request):
        sent.append(request)
        number = len(sent)
        return httpx.Response(
            status,
            request=request,
            headers={} if request_id is None else {"x-request-id": request_id},
            json={
                "id": "completion-is-not-request-id",
                "model": "model-snapshot",
                "usage": {
                    "prompt_tokens": 10 * number,
                    "completion_tokens": 5 * number,
                },
                "choices": [{"message": {"content": "ok"}}],
            },
        )

    client = OpenAIChatClient(
        "test", model="fixture", transport=httpx.MockTransport(handle)
    )
    call = RecordedCall(call_identity, policy(), ledger)
    try:
        if status == 503:
            with pytest.raises(httpx.HTTPStatusError):
                await client.complete("x", call=call)
        else:
            assert await client.complete("x", call=call) == "ok"
    finally:
        await client.close()
    expected = 1 if status == 200 else 3
    assert len(sent) == expected
    # Read back committed rows through a new ledger/session, not an in-memory sink.
    records = await ExecutionLedger(s2_repo).records(
        call_identity.account_id, call_identity.call_id
    )
    assert [row.phase for row in records] == ["started", "finished"] * expected
    for number, row in enumerate(records[1::2], start=1):
        assert row.attempt_no == number and row.status_code == status
        assert row.outcome == ("success" if status == 200 else "error")
        assert row.request_id == (request_id or None)
        assert row.request_id_missing_reason == (
            None if request_id else "provider_not_returned"
        )
        assert row.usage.completeness == "complete"
        assert (row.usage.input_tokens, row.usage.output_tokens) == (
            10 * number,
            5 * number,
        )
        assert row.stop_reason == (
            "success"
            if status == 200
            else "retry_exhausted"
            if number == 3
            else "retry_scheduled"
        )
