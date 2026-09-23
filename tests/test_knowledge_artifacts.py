from uuid import uuid4

import pytest

from contexthub.models.knowledge import ContractError, SealRef, canonical_hash
from contexthub.services.artifact_service import ArtifactService
from knowledge_helpers import HASH


class ReadDB:
    def __init__(self, row=None, account="a"):
        self.row, self.account = row, account

    async def fetchval(self, *args):
        return self.account

    async def fetchrow(self, *args):
        return self.row


@pytest.mark.asyncio
async def test_missing_hash_and_tenant_are_distinct_errors():
    service = ArtifactService()
    ref = SealRef(account_id="a", id=uuid4(), kind="source", sha256=HASH)
    for db, code in [
        (ReadDB(), "artifact_missing"),
        (ReadDB(account="b"), "tenant_mismatch"),
        (ReadDB({"sha256": "0" * 64, "payload": {}}), "artifact_hash_mismatch"),
    ]:
        with pytest.raises(ContractError, match=code):
            await service.get(db, ref)


@pytest.mark.asyncio
async def test_reference_kind_mismatch():
    service = ArtifactService()
    payload = {"kind": "graph"}
    ref = SealRef(
        account_id="a", id=uuid4(), kind="source", sha256=canonical_hash(payload)
    )
    with pytest.raises(ContractError, match="reference_kind_mismatch"):
        await service.get(ReadDB({"sha256": ref.sha256, "payload": payload}), ref)
