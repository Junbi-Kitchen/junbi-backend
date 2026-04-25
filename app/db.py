
import psycopg_pool
from psycopg.rows import dict_row




from config import settings

# Async pool — used by all FastAPI route handlers via get_db()
_async_pool: psycopg_pool.AsyncConnectionPool | None = None



async def open_pool() -> None:
    global _async_pool
    _async_pool = psycopg_pool.AsyncConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=2,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await _async_pool.open()



async def close_pool() -> None:
    global _async_pool
    if _async_pool:
        await _async_pool.close()
        _async_pool = None


def get_async_pool() -> psycopg_pool.AsyncConnectionPool:
      return _async_pool  

async def get_db():
    """FastAPI dependency — yields a psycopg3 async connection (dict_row).
    Commits on success, rolls back on exception, returns to pool when done.
    """
    async with _async_pool.connection() as conn:
        yield conn


