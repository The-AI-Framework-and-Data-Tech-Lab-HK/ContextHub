"""FastAPI dependency providers."""

from __future__ import annotations

from fastapi import Request

from app.orchestrators.commit_orchestrator import CommitOrchestrator
from app.orchestrators.retrieve_orchestrator import RetrieveOrchestrator


def get_commit_orchestrator(request: Request) -> CommitOrchestrator:
    # Wired once in app factory and reused per request.
    return request.app.state.commit_orchestrator


def get_retrieve_orchestrator(request: Request) -> RetrieveOrchestrator:
    return request.app.state.retrieve_orchestrator
