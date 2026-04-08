import pytest

from contexthub.errors import BadRequestError, NotFoundError
from contexthub.models.context import (
    ContextLevel,
    ContextType,
    CreateContextRequest,
    Scope,
    UpdateContextRequest,
)
from contexthub.models.request import RequestContext
from contexthub.services.access_decision import AccessDecision
from contexthub.services.acl_service import ACLService
from contexthub.services.context_service import ContextService
from contexthub.services.masking_service import MaskingService
from contexthub.store.context_store import ContextStore


class FakeRecord:
    def __init__(self, **data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class LsDB:
    async def fetch(self, sql: str, *args):
        if "visible_teams" in sql:
            return [
                FakeRecord(path="engineering/backend"),
                FakeRecord(path="engineering"),
                FakeRecord(path=""),
            ]
        if "SELECT uri, scope, owner_space, status" in sql:
            return [
                FakeRecord(
                    uri="ctx://team/engineering/backend/runbooks/api",
                    scope="team",
                    owner_space="engineering/backend",
                    status="active",
                ),
                FakeRecord(
                    uri="ctx://team/data/warehouse",
                    scope="team",
                    owner_space="data",
                    status="active",
                ),
            ]
        if "access_policies" in sql:
            return []
        if "team_memberships" in sql:
            return [
                FakeRecord(path="engineering/backend"),
                FakeRecord(path="engineering"),
            ]
        raise AssertionError(sql)


class MissingContextDB:
    async def fetchval(self, sql: str, *args):
        return None


class DenyReadACL:
    async def check_read(self, db, uri: str, ctx: RequestContext) -> bool:
        return False

    async def check_read_access(self, db, uri, ctx):
        return AccessDecision(allowed=False, field_masks=None, reason="test deny")


class DenyWriteACL:
    async def check_write(self, db, uri: str, ctx: RequestContext) -> bool:
        return False


@pytest.mark.asyncio
async def test_ls_accepts_record_like_rows_and_filters_visible_children():
    store = ContextStore(ACLService(), MaskingService())

    children = await store.ls(
        LsDB(),
        "ctx://team",
        RequestContext(account_id="acme", agent_id="query-agent"),
    )

    assert children == ["engineering"]


@pytest.mark.asyncio
async def test_store_read_returns_not_found_when_acl_denies_missing_context():
    store = ContextStore(DenyReadACL(), MaskingService())

    with pytest.raises(NotFoundError, match="ctx://datalake/prod/orders"):
        await store.read(
            MissingContextDB(),
            "ctx://datalake/prod/orders",
            ContextLevel.L1,
            RequestContext(account_id="acme", agent_id="query-agent"),
        )


@pytest.mark.asyncio
async def test_store_write_returns_not_found_when_acl_denies_missing_context():
    store = ContextStore(DenyWriteACL(), MaskingService())

    with pytest.raises(NotFoundError, match="ctx://team/engineering/doc"):
        await store.write(
            MissingContextDB(),
            "ctx://team/engineering/doc",
            ContextLevel.L1,
            "updated content",
            RequestContext(account_id="acme", agent_id="query-agent", expected_version=1),
        )


@pytest.mark.asyncio
async def test_service_update_returns_not_found_when_acl_denies_missing_context():
    acl = DenyWriteACL()
    service = ContextService(ContextStore(ACLService(), MaskingService()), acl)

    with pytest.raises(NotFoundError, match="ctx://team/engineering/doc"):
        await service.update(
            MissingContextDB(),
            "ctx://team/engineering/doc",
            UpdateContextRequest(tags=["docs"]),
            RequestContext(account_id="acme", agent_id="query-agent", expected_version=1),
        )


def test_team_scope_requires_owner_space_even_for_root_team():
    body = CreateContextRequest(
        uri="ctx://team/engineering/doc",
        context_type=ContextType.RESOURCE,
        scope=Scope.TEAM,
        owner_space=None,
    )

    with pytest.raises(BadRequestError, match="owner_space"):
        ContextService._validate_uri_scope(body)
