import asyncpg

from contexthub.config import Settings


class _OpenGaussConnection(asyncpg.Connection):
    """Avoid UNLISTEN in pool reset queries for openGauss compatibility."""

    def get_reset_query(self):
        return "SELECT pg_advisory_unlock_all(); CLOSE ALL; RESET ALL;"


async def create_pool(settings: Settings) -> asyncpg.Pool:
    kwargs = {
        "dsn": settings.asyncpg_database_url,
        "min_size": 2,
        "max_size": 10,
    }
    if settings.is_opengauss:
        kwargs["connection_class"] = _OpenGaussConnection
    return await asyncpg.create_pool(**kwargs)
