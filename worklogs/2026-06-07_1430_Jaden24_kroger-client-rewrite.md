# 2026-06-07 14:30 — Jaden24 — kroger-client-rewrite

**Branch:** main (e9b369a)
**Repo:** gook-backend

---

## What was done

- Replaced ADK/LiteLLM-based `kroger_agent` with a direct Python client `app/agents/kroger_client/` — no LLM, no MCP subprocess, no token limits
- Expanded `KrogerProduct` TypedDict from 6 → 23 fields: `brand`, `sale_price`, `organic`, `snap_eligible`, `manufacturer_declarations`, `allergens`, `stock_level`, `fulfillment_pickup/delivery/in_store`, `temperature`, `rating`, `review_count`, `categories`, `country_origin`, `nutrition`
- Added extraction helpers: `_extract_sale_price`, `_extract_stock_level`, `_extract_fulfillment`, `_extract_allergens`, `_extract_nutrition` (handles both dict and list shapes from Kroger API)
- Updated `smart_grocery_agent/tools.py` `_claude_refine_cart` to pass rich product data (nutrition, allergens, organic, stock level, sale price) to Claude for personalized cart decisions
- Switched smart_grocery orchestrator model from `gemini-2.0-flash` → `LiteLlm(model="anthropic/claude-haiku-4-5-20251001")` — Google free tier quota exhausted
- Added `.kroger_token*.json` and `kroger_preferences.json` to `.gitignore` — OAuth tokens auto-created by kroger-api library, never commit
- Reorganized test scripts into `scripts/kroger/`: `kroger_smoke_test.py`, `kroger_locations_test.py`, `kroger_rich_fields_test.py`
- Introduced `worklogs/` folder structure + pre-push hook enforcement

## Decisions made

- **No LLM for product search.** ADK re-sends full conversation history on every LLM call — 5 items × 5 products = 50k+ tokens/min, blowing Tier 1 rate limit instantly. Product search is deterministic so no LLM needed. Direct API: 4.9s, zero tokens.
- **`asyncio.to_thread` for kroger-api.** Library uses `requests` (sync) — run in thread pool so FastAPI event loop is never blocked.
- **Rich fields for personalization.** 17 new fields exist so Claude can make allergen-safe, dietary-compliant, budget-optimized picks — not just for display.
- **`nutritionInformation` shape is inconsistent.** Kroger returns it as a dict OR a list depending on the product. `_extract_nutrition()` handles both.
- **Legacy aliases kept.** `run_kroger_agent` / `KrogerAgentResult` re-exported from `kroger_client/__init__.py` so nothing breaks.

## Bottlenecks hit

- **Google ADK 50k TPM rate limit** — hit after 5 product searches. Root cause: ADK accumulates all tool-call results in conversation history. Fix: removed LLM entirely from product search loop.
- **fastmcp `Image` import error** — kroger-mcp 0.2.0 imports `Image` from fastmcp but fastmcp 3.x removed it. Patched uv cache copies manually. Not relevant now since we don't use MCP anymore.
- **Anthropic model 404** — account only has new-generation models (`claude-haiku-4-5-20251001`, `claude-sonnet-4-6`). claude-3 series returns 404. Use new names only.
- **`nutritionInformation` is a list not a dict** — caused `'list' object has no attribute 'get'` crash. Fixed by detecting type before parsing.

## Still mocked / pending

- Kroger user-scoped OAuth (cart add) — only Client Credentials (read-only) implemented
- Nutrition values (calories, protein, etc.) are `None` — `product.compact` scope omits them; need full detail endpoint
- Instacart price comparison still uses multiplier heuristics, not real API calls

## Next up

- Kroger OAuth user flow → POST /auth/kroger/connect → cart add
- Call full product detail endpoint to fill in nutrition values
- Integration test: user with `dietaryTags: ["dairy-free"]` → verify Claude blocks dairy allergen products
