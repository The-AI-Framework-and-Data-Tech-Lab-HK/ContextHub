"""Application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from contexthub.api.middleware import AuthMiddleware
from contexthub.api.routers.contexts import router as contexts_router
from contexthub.config import Settings
from contexthub.db.pool import create_pool
from contexthub.db.repository import PgRepository
from contexthub.services.acl_service import ACLService
from contexthub.services.context_service import ContextService
from contexthub.store.context_store import ContextStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    pool = await create_pool(settings)
    repo = PgRepository(pool)

    acl_service = ACLService()
    context_store = ContextStore(acl_service)
    context_service = ContextService(context_store, acl_service)

    app.state.settings = settings
    app.state.repo = repo
    app.state.context_store = context_store
    app.state.context_service = context_service

    yield

    await pool.close()


app = FastAPI(title="ContextHub", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(contexts_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
