"""Durable attempt events. Each append commits outside the caller's transaction.

A started event without a finished event is explicitly incomplete, never success
or zero-cost. Same call_id cannot be restarted by an outer retry loop.
"""

from typing import Protocol
from uuid import UUID

from contexthub.db.repository import PgRepository
from contexthub.models.execution import AttemptRecord
from contexthub.models.knowledge import ContractError, canonical_hash


class AttemptSink(Protocol):
    async def append(self, record: AttemptRecord) -> None: ...


def validate_append(previous: list[AttemptRecord], record: AttemptRecord):
    same = [
        r
        for r in previous
        if r.attempt_no == record.attempt_no and r.phase == record.phase
    ]
    if same:
        # A start is a reservation, never idempotently reusable for another send.
        if record.phase == "finished" and same[0] == record:
            return False
        raise ContractError("attempt_already_recorded")
    if previous:
        first = previous[0]
        for field in (
            "identity",
            "input_sha256",
            "prompt_sha256",
            "request_config_sha256",
            "retry_policy_sha256",
            "requested_model",
            "price",
        ):
            if getattr(first, field) != getattr(record, field):
                raise ContractError("call_identity_mismatch")
    if record.phase == "started":
        starts = [r for r in previous if r.phase == "started"]
        if record.attempt_no != len(starts) + 1:
            raise ContractError("attempt_sequence_mismatch")
        if starts:
            last = next(
                (
                    r
                    for r in previous
                    if r.attempt_no == starts[-1].attempt_no and r.phase == "finished"
                ),
                None,
            )
            if last is None or last.stop_reason != "retry_scheduled":
                raise ContractError("call_closed_or_incomplete")
    else:
        start = next(
            (
                r
                for r in previous
                if r.attempt_no == record.attempt_no and r.phase == "started"
            ),
            None,
        )
        if start is None or start.started_at != record.started_at:
            raise ContractError("attempt_start_missing")
    return True


class ExecutionLedger:
    def __init__(self, repository: PgRepository):
        self.repository = repository

    async def append(self, record: AttemptRecord) -> None:
        record = AttemptRecord.model_validate(record.model_dump(mode="json"))
        identity = record.identity
        async with self.repository.session(identity.account_id) as db:
            if identity.event_id is not None and not await db.fetchval(
                "SELECT 1 FROM change_events WHERE account_id=$1 AND event_id=$2",
                identity.account_id,
                identity.event_id,
            ):
                raise ContractError("event_missing")
            if identity.work_item_id is not None and not await db.fetchval(
                "SELECT 1 FROM maintenance_work_items WHERE account_id=$1 AND id=$2",
                identity.account_id,
                identity.work_item_id,
            ):
                raise ContractError("work_missing")
            # Per-call DB lock serializes reservations across processes, not a
            # shared usage counter. The account participates in the lock key.
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                identity.account_id + ":" + str(identity.call_id),
            )
            previous = await self._read(db, identity.account_id, identity.call_id)
            if not validate_append(previous, record):
                return
            await db.execute(
                """INSERT INTO execution_attempts
                (account_id,call_id,attempt_no,phase,execution_id,sha256,payload)
                VALUES($1,$2,$3,$4,$5,$6,$7)""",
                identity.account_id,
                identity.call_id,
                record.attempt_no,
                record.phase,
                identity.execution_id,
                canonical_hash(record),
                record.model_dump(mode="json"),
            )

    async def _read(self, db, account_id, call_id):
        rows = await db.fetch(
            """SELECT payload,sha256 FROM execution_attempts
            WHERE account_id=$1 AND call_id=$2 ORDER BY attempt_no, CASE phase WHEN 'started' THEN 0 ELSE 1 END""",
            account_id,
            call_id,
        )
        result = []
        for row in rows:
            if canonical_hash(row["payload"]) != row["sha256"]:
                raise ContractError("attempt_hash_mismatch")
            result.append(AttemptRecord.model_validate(row["payload"]))
        return result

    async def records(
        self, account_id: str, call_id: UUID
    ) -> tuple[AttemptRecord, ...]:
        async with self.repository.session(account_id) as db:
            return tuple(await self._read(db, account_id, call_id))
