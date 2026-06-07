# Junbi Backend — Work Log

Newest entries at top. Before every PR, run `/junbi-worklog` — Claude reads your git diff and writes the entry. Review, tweak, commit it alongside your code.

---

## Entry Format

```
## YYYY-MM-DD — feature/branch-name

**What was done:**
- Bullet points of what changed

**Decisions made:**
- Any non-obvious choices and why (these belong here, not in code comments)

**Still mocked / pending:**
- What's stubbed out and why

**Next up:**
- Immediate follow-on work
```

---

<!-- Entries go below this line, newest first -->

## 2026-06-07 — Kroger direct client + rich product fields

**What was done:**
- Replaced the ADK/LiteLLM-based `kroger_agent` with a direct Python client (`app/agents/kroger_client/`) — no LLM, no MCP subprocess, no token limits. Flow: Client Credentials OAuth → nearest store by zip → sequential product search via `kroger-api` library → pick best match.
- Expanded `KrogerProduct` TypedDict from 6 fields to 23: added `brand`, `sale_price`, `organic`, `snap_eligible`, `manufacturer_declarations`, `allergens`, `stock_level`, `fulfillment_pickup/delivery/in_store`, `temperature`, `rating`, `review_count`, `categories`, `country_origin`, `nutrition` (calories, protein, fat, carbs, fiber, sodium).
- Added extraction helpers: `_extract_sale_price`, `_extract_stock_level`, `_extract_fulfillment`, `_extract_allergens`, `_extract_nutrition` (handles both dict and list shapes from Kroger API).
- Updated `smart_grocery_agent/tools.py` `_claude_refine_cart` to pass full rich product data to Claude — nutrition, allergens, organic flag, stock level, sale price, manufacturer claims — so Claude can make personalized picks (keto → prefer high-protein, dairy-free → block dairy allergens, organic preference → favor `organic: true`, out-of-stock → skip).
- Added dietary guidance prompt section that translates user `dietaryTags` + `allergies` into explicit Claude instructions.
- Switched smart_grocery orchestrator model from `gemini-2.0-flash` to `LiteLlm(model="anthropic/claude-haiku-4-5-20251001")` (Google free tier quota exhausted).
- Added `.kroger_token*.json` and `kroger_preferences.json` to `.gitignore` — these are OAuth tokens and local config auto-created by the kroger-api library, never commit them.
- Reorganized test scripts into `scripts/kroger/` folder: `kroger_smoke_test.py`, `kroger_locations_test.py`, `kroger_rich_fields_test.py` (new — validates every field with ✓/~/✗ output).

**Decisions made:**
- **No LLM for product search.** Kroger search is deterministic (auth → location → search → pick). ADK was accumulating full tool-call history in context and re-sending it on every LLM call — 5 items × 5 products each = 50k+ input tokens/min, blowing past Tier 1 rate limits in seconds. Direct API call: 4.9s, zero tokens, no rate limits.
- **`asyncio.to_thread` for kroger-api.** The `kroger-api` library uses `requests` (sync). We run it in a thread pool so FastAPI's event loop is never blocked.
- **Rich fields for personalization, not display.** The 17 new fields exist so the smart_grocery orchestrator's Claude call can make truly informed cart decisions — allergen safety, dietary compliance, budget optimization using sale prices — not just for showing pretty UI data.
- **`nutritionInformation` can be a dict or list.** Kroger's API returns it inconsistently across products. `_extract_nutrition` handles both shapes. Calorie/macro values require the full product detail endpoint (`product.compact` scope omits them) — stored as `None` for now, not a bug.
- **fastmcp/kroger-mcp compatibility patch.** kroger-mcp 0.2.0 imports `Image` from fastmcp but fastmcp 3.x removed that export. Patched all 3 cached copies in uv cache. Not relevant if MCP is not used (we no longer use it).
- **Legacy aliases kept.** `run_kroger_agent` and `KrogerAgentResult` are re-exported from `kroger_client/__init__.py` so any code that imported the old names still works.

**Still mocked / pending:**
- Kroger OAuth user-scoped flow (POST /auth/kroger/connect) for cart add — currently only Client Credentials (read-only) is implemented.
- Nutrition values (calories, protein, etc.) are `None` for all products — requires the full product detail API call, not covered by `product.compact` scope.
- `smart_grocery_agent` still uses ADK + LiteLLM for orchestration — the token accumulation issue is mitigated by the rich `KrogerProduct` reducing round-trips, but a future refactor could replace ADK with direct Claude tool_use calls.
- Instacart catalog search still uses stub data (see `app/agents/smart_grocery_agent/tools.py` `_compute_price_comparison`).

**Next up:**
- Wire Kroger OAuth user flow so the agent can actually add items to a Kroger cart (POST /cart/add).
- Call `product.compact` detail endpoint per item to fill in nutrition values.
- Add `search_kroger_products` filter: skip `TEMPORARILY_OUT_OF_STOCK` at search time, not just at refine time.
- Write integration test for `_claude_refine_cart` with a mock user who has `dietaryTags: ["dairy-free"]` — verify Claude skips dairy-allergen products.
