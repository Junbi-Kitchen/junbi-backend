import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from config import settings

_pool: psycopg_pool.AsyncConnectionPool | None = None


async def open_pool() -> None:
    global _pool
    _pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=2,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await _pool.open()


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_db():
    """FastAPI dependency — yields a psycopg3 async connection (dict_row).
    Usage: db = Depends(get_db)
    Commits on success, rolls back on exception, returns to pool when done.
    """
    async with _pool.connection() as conn:
        yield conn
