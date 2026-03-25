"""Typed async client for ContextHub Server API."""

from __future__ import annotations

from typing import Any, Union

import httpx

from .exceptions import ContextHubError, raise_for_status
from .models import (
    ContextLevel,
    ContextReadResult,
    ContextRecord,
    ContextStat,
    ContextStatus,
    ContextType,
    DependencyRecord,
    MemoryRecord,
    ResolvedSkillReadResult,
    Scope,
    SearchResponse,
    SkillSubscriptionRecord,
    SkillVersionRecord,
)

# Re-export enums for convenience
__all__ = ["ContextHubClient"]


# ── Internal helpers ────────────────────────────────────────────────────


def _extract_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body.get("detail", resp.text)
    except Exception:
        pass
    return resp.text


# ── Namespace classes ───────────────────────────────────────────────────


class _ContextNamespace:
    """client.context.* operations."""

    def __init__(self, client: ContextHubClient) -> None:
        self._c = client

    async def create(
        self,
        *,
        uri: str,
        context_type: ContextType,
        scope: Scope,
        owner_space: str | None = None,
        l0_content: str | None = None,
        l1_content: str | None = None,
        l2_content: str | None = None,
        file_path: str | None = None,
        tags: list[str] | None = None,
    ) -> ContextRecord:
        body: dict[str, Any] = {
            "uri": uri,
            "context_type": context_type.value,
            "scope": scope.value,
        }
        if owner_space is not None:
            body["owner_space"] = owner_space
        if l0_content is not None:
            body["l0_content"] = l0_content
        if l1_content is not None:
            body["l1_content"] = l1_content
        if l2_content is not None:
            body["l2_content"] = l2_content
        if file_path is not None:
            body["file_path"] = file_path
        if tags is not None:
            body["tags"] = tags
        data = await self._c._post("/api/v1/contexts", json=body, expected_status=201)
        return ContextRecord.model_validate(data)

    async def read(
        self,
        uri: str,
        *,
        level: ContextLevel = ContextLevel.L1,
        version: int | None = None,
    ) -> Union[ContextReadResult, ResolvedSkillReadResult]:
        params: dict[str, Any] = {"level": level.value}
        if version is not None:
            params["version"] = version
        data = await self._c._get(f"/api/v1/contexts/{uri}", params=params)
        if "version" in data and "status" in data:
            return ResolvedSkillReadResult.model_validate(data)
        return ContextReadResult.model_validate(data)

    async def update(
        self,
        uri: str,
        *,
        expected_version: int,
        l0_content: str | None = None,
        l1_content: str | None = None,
        l2_content: str | None = None,
        file_path: str | None = None,
        status: ContextStatus | None = None,
        tags: list[str] | None = None,
    ) -> ContextRecord:
        body: dict[str, Any] = {}
        if l0_content is not None:
            body["l0_content"] = l0_content
        if l1_content is not None:
            body["l1_content"] = l1_content
        if l2_content is not None:
            body["l2_content"] = l2_content
        if file_path is not None:
            body["file_path"] = file_path
        if status is not None:
            body["status"] = status.value
        if tags is not None:
            body["tags"] = tags
        data = await self._c._patch(
            f"/api/v1/contexts/{uri}",
            json=body,
            expected_version=expected_version,
        )
        return ContextRecord.model_validate(data)

    async def delete(self, uri: str, *, expected_version: int) -> None:
        await self._c._delete(
            f"/api/v1/contexts/{uri}",
            expected_version=expected_version,
        )

    async def stat(self, uri: str) -> ContextStat:
        data = await self._c._get(f"/api/v1/contexts/{uri}/stat")
        return ContextStat.model_validate(data)

    async def children(self, uri: str) -> list[str]:
        return await self._c._get(f"/api/v1/contexts/{uri}/children")

    async def deps(self, uri: str) -> list[DependencyRecord]:
        data = await self._c._get(f"/api/v1/contexts/{uri}/deps")
        return [DependencyRecord.model_validate(d) for d in data]


class _MemoryNamespace:
    """client.memory.* operations."""

    def __init__(self, client: ContextHubClient) -> None:
        self._c = client

    async def add(self, *, content: str, tags: list[str] | None = None) -> ContextRecord:
        body: dict[str, Any] = {"content": content}
        if tags is not None:
            body["tags"] = tags
        data = await self._c._post("/api/v1/memories", json=body, expected_status=201)
        return ContextRecord.model_validate(data)

    async def list(self) -> list[MemoryRecord]:
        data = await self._c._get("/api/v1/memories")
        return [MemoryRecord.model_validate(d) for d in data]

    async def promote(self, *, uri: str, target_team: str) -> ContextRecord:
        body = {"uri": uri, "target_team": target_team}
        data = await self._c._post("/api/v1/memories/promote", json=body, expected_status=201)
        return ContextRecord.model_validate(data)


class _SkillNamespace:
    """client.skill.* operations."""

    def __init__(self, client: ContextHubClient) -> None:
        self._c = client

    async def publish(
        self,
        *,
        skill_uri: str,
        content: str,
        changelog: str | None = None,
        is_breaking: bool = False,
    ) -> SkillVersionRecord:
        body: dict[str, Any] = {
            "skill_uri": skill_uri,
            "content": content,
            "is_breaking": is_breaking,
        }
        if changelog is not None:
            body["changelog"] = changelog
        data = await self._c._post("/api/v1/skills/versions", json=body, expected_status=201)
        return SkillVersionRecord.model_validate(data)

    async def versions(self, uri: str) -> list[SkillVersionRecord]:
        data = await self._c._get(f"/api/v1/skills/{uri}/versions")
        return [SkillVersionRecord.model_validate(d) for d in data]

    async def subscribe(
        self, *, skill_uri: str, pinned_version: int | None = None
    ) -> SkillSubscriptionRecord:
        body: dict[str, Any] = {"skill_uri": skill_uri}
        if pinned_version is not None:
            body["pinned_version"] = pinned_version
        data = await self._c._post("/api/v1/skills/subscribe", json=body)
        return SkillSubscriptionRecord.model_validate(data)


class ContextHubClient:
    """Typed async client for the ContextHub server API.

    Usage::

        async with ContextHubClient(url="...", api_key="...",
                                     account_id="...", agent_id="...") as client:
            resp = await client.search(query="table schema")
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        account_id: str,
        agent_id: str,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-API-Key": api_key,
                "X-Account-Id": account_id,
                "X-Agent-Id": agent_id,
            },
            timeout=timeout,
        )
        self.context = _ContextNamespace(self)
        self.memory = _MemoryNamespace(self)
        self.skill = _SkillNamespace(self)

    # ── async context manager ───────────────────────────────────────────

    async def __aenter__(self) -> ContextHubClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── internal HTTP helpers ───────────────────────────────────────────

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._http.get(path, params=params)
        raise_for_status(resp.status_code, _extract_detail(resp))
        return resp.json()

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        expected_status: int = 200,
    ) -> Any:
        resp = await self._http.post(path, json=json)
        raise_for_status(resp.status_code, _extract_detail(resp))
        if resp.status_code != expected_status:
            raise ContextHubError(
                f"Unexpected status code: expected {expected_status}, got {resp.status_code}",
                status_code=resp.status_code,
            )
        return resp.json()

    async def _patch(
        self,
        path: str,
        *,
        json: dict[str, Any],
        expected_version: int,
    ) -> Any:
        resp = await self._http.patch(
            path,
            json=json,
            headers={"If-Match": str(expected_version)},
        )
        raise_for_status(resp.status_code, _extract_detail(resp))
        return resp.json()

    async def _delete(
        self,
        path: str,
        *,
        expected_version: int,
    ) -> None:
        resp = await self._http.delete(
            path,
            headers={"If-Match": str(expected_version)},
        )
        raise_for_status(resp.status_code, _extract_detail(resp))

    # ── top-level convenience methods ───────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        scope: list[Scope] | None = None,
        context_type: list[ContextType] | None = None,
        top_k: int = 10,
        level: ContextLevel = ContextLevel.L1,
        include_stale: bool = True,
    ) -> SearchResponse:
        body: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "level": level.value,
            "include_stale": include_stale,
        }
        if scope is not None:
            body["scope"] = [s.value for s in scope]
        if context_type is not None:
            body["context_type"] = [ct.value for ct in context_type]
        data = await self._post("/api/v1/search", json=body)
        return SearchResponse.model_validate(data)

    async def ls(self, path: str) -> list[str]:
        return await self._post("/api/v1/tools/ls", json={"path": path})

    async def read(
        self,
        uri: str,
        *,
        level: ContextLevel = ContextLevel.L1,
        version: int | None = None,
    ) -> Union[ContextReadResult, ResolvedSkillReadResult]:
        body: dict[str, Any] = {"uri": uri, "level": level.value}
        if version is not None:
            body["version"] = version
        data = await self._post("/api/v1/tools/read", json=body)
        if "version" in data and "status" in data:
            return ResolvedSkillReadResult.model_validate(data)
        return ContextReadResult.model_validate(data)

    async def grep(
        self,
        query: str,
        *,
        scope: list[Scope] | None = None,
        context_type: list[ContextType] | None = None,
        top_k: int = 5,
    ) -> SearchResponse:
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if scope is not None:
            body["scope"] = [s.value for s in scope]
        if context_type is not None:
            body["context_type"] = [ct.value for ct in context_type]
        data = await self._post("/api/v1/tools/grep", json=body)
        return SearchResponse.model_validate(data)

    async def stat(self, uri: str) -> ContextStat:
        data = await self._post("/api/v1/tools/stat", json={"uri": uri})
        return ContextStat.model_validate(data)

    async def health(self) -> dict[str, Any]:
        """Call /health (no auth required)."""
        resp = await self._http.get("/health")
        raise_for_status(resp.status_code, _extract_detail(resp))
        return resp.json()
