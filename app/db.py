import threading
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager

from config import settings

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


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


@contextmanager
def _get_conn():
    key = threading.get_ident()
    conn = get_pool().getconn(key=key)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        get_pool().putconn(conn, key=key)


def get_db():
    """FastAPI dependency — yields a psycopg2 connection (RealDictCursor).
    Usage: db = Depends(get_db)
    """
    with _get_conn() as conn:
        yield conn
