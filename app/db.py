import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from config import settings

# Async pool — used by all FastAPI route handlers via get_db()
_async_pool: psycopg_pool.AsyncConnectionPool | None = None

# Sync pool — used by LangGraph agent nodes (which run as sync functions)
_sync_pool: psycopg_pool.ConnectionPool | None = None


async def open_pool() -> None:
    global _async_pool, _sync_pool
    _async_pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=2,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await _async_pool.open()

    _sync_pool = psycopg_pool.ConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=True,
    )


async def close_pool() -> None:
    global _async_pool, _sync_pool
    if _async_pool:
        await _async_pool.close()
        _async_pool = None
    if _sync_pool:
        _sync_pool.close()
        _sync_pool = None


async def get_db():
    """FastAPI dependency — yields a psycopg3 async connection (dict_row).
    Commits on success, rolls back on exception, returns to pool when done.
    """
    async with _async_pool.connection() as conn:
        yield conn


def get_pool() -> psycopg_pool.ConnectionPool:
    """Sync pool accessor for LangGraph agent nodes."""
    return _sync_pool
