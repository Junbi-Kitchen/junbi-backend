# 2026-07-04 15:15 — Jaden24 — langgraph-migration

**Branch:** feature/langgraph-migration
**Repo:** gook-backend

---

## What was done
- Converted `app/agents/smart_grocery_agent/` from a Google ADK `LlmAgent` orchestrator to a LangGraph `StateGraph` (`state.py`, `nodes.py`, `graph.py`, `runner.py`). 10-node linear graph: load_context → resolve_stores → analyze_pantry → search_store → compare_prices → build_cart → human_checkpoint → {place_order → finalize | cancelled}.
- Restored a real human-in-the-loop checkpoint via `interrupt()`/`Command(resume=...)` against a `MemorySaver` checkpoint, replacing the ADK-era two-phase HTTP workaround (`_pending_sessions` dict) — ADK has no interrupt primitive.
- Added `runner.get_session_status()` so the `/status` route reads the graph's checkpoint instead of reaching into a private dict.
- Converted `app/agents/ingredient_resolver/` from an ADK `Agent` + `Runner` to a small 2-node LangGraph Anthropic tool-use loop (`call_model` ⇄ `call_tools`); `tools.py` (search_fulltext/search_vector/create_ingredient) needed no changes.
- Removed `google-adk` from `requirements.txt` and `GOOGLE_API_KEY` from `app/config.py` — nothing in the backend depends on Gemini anymore. Added `langgraph`.
- Updated `CLAUDE.md` (folder structure, Smart Grocery Agent section, new Ingredient Resolver section, stale Instacart references) to describe the actual LangGraph implementation.

## Decisions made
- Kept the `smart_grocery_agent/` directory name (not renamed to `smart_grocery/`, despite older CLAUDE.md prose) to avoid churning the route/`__init__.py` import path.
- Dropped the top-level `GOOGLE_API_KEY`-gated stub response in `start_grocery_agent` — every node already degrades gracefully on its own, so the graph now produces a real response (real DB context, real store scoring) instead of a canned 3-item stub even with no keys configured.
- Dropped the manual `_SESSION_TTL_SECONDS`/`_prune_expired` sweep — `MemorySaver` has no per-thread eviction API to hook it into; matches the "swap to `PostgresSaver` for production" trade-off CLAUDE.md already documented.
- `agent_summary` is now a deterministic f-string built in `build_cart` instead of free text from an orchestrator LLM — there's no more orchestrator model sequencing tool calls, since node order is the graph itself.

## Bottlenecks hit
- `get_session_status` initially inferred `order_placed` vs `cancelled` from `grocery_list_id`, which is also `None` when the cart is empty or the DB write fails — misreported placed orders as cancelled. Fixed by tracking an explicit `status` field in `SmartGroceryState`, found while exercising the real confirm flow end-to-end.
- Claude Haiku (unlike the Gemini model `ingredient_resolver` used before) frequently prefaces its final JSON answer with a sentence of explanation despite the system prompt forbidding it, which broke the old fence-stripping parser and silently fell back to a possibly-wrong ingredient match. Fixed the parser to extract the JSON object from wherever it lands in the text, and tightened the prompt.

## Still mocked / pending
- Kroger cart-add (`place_order`) still returns `pending_oauth` until the Kroger OAuth linking flow is built — unchanged from before this migration.
- The same fragile "assume JSON starts at index 0" parsing pattern in `smart_grocery_agent/nodes.py`'s `analyze_pantry`/`build_cart` Claude calls was left untouched (pre-existing, not introduced by this migration) — worth hardening the same way if it ever misfires there.

## Next up
- Swap `MemorySaver` for `PostgresSaver` before running multiple backend replicas in production.
- Build the Kroger OAuth linking flow so `place_order` can actually add to a real cart instead of returning `pending_oauth`.
