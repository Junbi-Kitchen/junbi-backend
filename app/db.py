import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager

from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

from config import settings

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_async_pool: AsyncConnectionPool | None = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=settings.DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


async def get_async_pool() -> AsyncConnectionPool:
    """Async psycopg v3 pool — used by agent nodes for parallel queries."""
    global _async_pool
    if _async_pool is None:
        _async_pool = AsyncConnectionPool(
            conninfo=settings.DATABASE_URL,
            min_size=2,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await _async_pool.open()
    return _async_pool


async def close_async_pool() -> None:
    global _async_pool
    if _async_pool is not None:
        await _async_pool.close()
        _async_pool = None


@contextmanager
def _get_conn():
    conn = get_pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().putconn(conn)


def get_db():
    """FastAPI dependency — yields a psycopg2 connection (RealDictCursor).
    Usage: db = Depends(get_db)
    """
    with _get_conn() as conn:
        yield conn
