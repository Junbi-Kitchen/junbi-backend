# 2026-06-08 17:23 — ledq — agent-pool-seam-packaging

**Branch:** ingredient_resolver
**Repo:** gook-backend

---

## Context

The `ingredient_resolver` is an ADK agent that maps a raw ingredient name
(e.g. `"2 cups finely chopped organic yellow onion"`) to a canonical row in the
shared `ingredients` table. The service (`app/services/ingredient_resolver.py`)
normalizes the name, runs an exact pre-screen, then hands off to the LLM agent
(`app/agents/ingredient_resolver/`) which uses full-text + vector search tools
to either match an existing ingredient or create a new one. It is called
in-process from the pantry/grocery/recipes/orders routes via
`run_ingredient_resolver`.

While running it standalone via `adk web`, the tools crashed with
`AttributeError: 'NoneType' object has no attribute 'connection'` — the agent
grabbed `app.db`'s pool, which is only opened by the FastAPI lifespan, so it was
`None` outside the API process.

## What was done

### Phase 1 — pool seam (commit 8219416)
- Added `app/agents/ingredient_resolver/resources.py` with `get_pool()` +
  `set_pool_provider()`. The agent now asks `get_pool()` for a connection source
  instead of grabbing `app.db`'s global directly.
- In-process: `main.py` lifespan injects the app's pool via
  `set_pool_provider(get_async_pool)` — agent reuses it, no second pool.
- Out-of-process (`adk web` / future standalone): no provider set, so the pool
  opens lazily. Fixes the crash.
- Pointed the 3 agent tools (`tools.py`) and the service's `_prescreen` /
  `_create_fallback` at `get_pool()`.

### Phase 2 — packaging (commit d52ea3d)
- Added `pyproject.toml`; `app` is now an installable package
  (`pip install -e .`), deps read dynamically from `requirements.txt`.
- Added package markers: `app/__init__.py`, `app/core/__init__.py`,
  `app/services/__init__.py`.
- Moved root `config.py` → `app/config.py` and updated 9 imports.
- Dropped the `PYTHONPATH=.` hack from `make adk` (no longer needed).

## Decisions made

- **Agent stays in-process for now, but designed for low-friction migration.**
  The pool-provider seam means a future split to Agent runtime is a wiring/CMD
  change, not a rewrite. The agent must not depend on a host-owned global.
- **Config moved into the package** to eliminate the second top-level import
  anchor (`config`) and avoid installing a generically-named module globally.
- **Data-access layer will be named `queries/`** (not `repositories/`/`crud/`)
  — raw-SQL codebase, no ORM.

## Bottlenecks hit

- One transient Supabase pool-connect error during verification; a direct
  connection and re-run both succeeded — environmental, not a code issue.

## Still mocked / pending

- Same seam not yet applied to `smart_grocery_agent` (still uses
  `get_async_pool` directly).
- `adk web` end-to-end (LLM tool calls) verified only at the pool level here;
  full UI run still needs `GOOGLE_API_KEY`.

## Next up

- Phase 3 — extract SQL into an `app/queries/` layer that takes a `conn`.
- Phase 4 — tests (rollback DB fixture, convert `test_resolver.py`) + config
  refactor (`get_settings()` + `@lru_cache` + `Depends`, `SecretStr`).
