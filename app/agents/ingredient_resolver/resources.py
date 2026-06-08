"""Pool-provider seam for the ingredient_resolver agent.

The agent must not depend on whichever host happens to run it. Instead of
grabbing FastAPI's lifespan-owned global pool directly, agent code asks
``get_pool()`` for a connection source:

- In-process (FastAPI): the lifespan injects the app's existing pool via
  ``set_pool_provider``, so the agent reuses it — no second pool.
- Out-of-process (``adk web`` / standalone): no provider is set, so the pool
  is opened lazily on first use.

At standalone-migration time, the lazy fallback below is the single place to
swap for a self-contained env-based pool.
"""

from typing import Callable, Optional

import psycopg_pool

# Callable returning the host-owned pool (e.g. app.db.get_async_pool).
_pool_provider: Optional[Callable[[], Optional[psycopg_pool.AsyncConnectionPool]]] = None


def set_pool_provider(
    provider: Callable[[], Optional[psycopg_pool.AsyncConnectionPool]],
) -> None:
    """Let a host inject its pool, so the agent reuses it instead of opening one."""
    global _pool_provider
    _pool_provider = provider


async def get_pool() -> psycopg_pool.AsyncConnectionPool:
    """Return a connection pool, regardless of which host is running the agent."""
    if _pool_provider is not None:
        pool = _pool_provider()
        if pool is not None:
            return pool

    # No host injected a pool (adk web / standalone). Open one lazily, reusing
    # app.db's machinery so config (dict_row, sizes) stays identical.
    from app.db import get_async_pool, open_pool

    pool = get_async_pool()
    if pool is None:
        await open_pool()
        pool = get_async_pool()
    return pool
