"""
smart_grocery_agent/agent.py
─────────────────────────────
Defines the Smart Grocery ADK orchestrator agent.

Role of this agent
──────────────────
The Smart Grocery orchestrator is the top-level AI agent for the grocery
planning feature. It receives a user's grocery planning request, coordinates
a sequence of tool calls to build a personalized cart, and returns a compact
JSON summary for the FastAPI layer to surface to the frontend.

It does NOT place the order — that happens in runner.py after the user
confirms the cart (Phase 2). This agent only handles Phase 1: planning.

How it works (tool call sequence)
──────────────────────────────────
  1. load_user_context    — pulls pantry, recipes, preferences, zip code from DB
  2. resolve_nearby_stores — scores Kroger-family stores by user preference fit
  3. analyze_missing_items — calls Claude to identify what to buy (recipe gaps,
                              expiring items, missing staples)
  4. search_kroger_products — delegates to the Kroger ADK agent (kroger_agent/)
                               which searches the live Kroger API via kroger-mcp
  5. build_grocery_cart   — calls Claude to pick the best product per item,
                             computes price comparison across store tiers

Data sharing between tools
──────────────────────────
Tools share data via ADK's ToolContext.state — a dict that persists across all
tool calls within a session. This means:
  - The LLM never sees or passes large payloads (pantry dumps, recipe lists)
  - Each tool reads what it needs from state and writes its output back
  - The LLM only receives compact summary strings from each tool call

This keeps Gemini token usage low and avoids context-window pressure.

Why Gemini?
───────────
Google ADK has native support for Gemini models and the ToolContext.state
mechanism. Gemini 2.0 Flash is used for speed and cost — the orchestrator's
reasoning task (sequencing tool calls, handling errors) doesn't require
the largest model.

Two-phase design
────────────────
This agent handles Phase 1 only (planning). Phase 2 (order submission) is
handled directly in runner.py without an LLM — it just takes the pre-built
cart and calls the Kroger cart API + writes to the DB.

This split means:
  - The user can review, edit, or cancel before anything is ordered
  - Phase 2 is fast and deterministic (no LLM latency on the confirm step)
  - Mirrors the old LangGraph human_checkpoint interrupt pattern
"""

from google.adk.agents import LlmAgent

from .tools import (
    load_user_context,
    resolve_nearby_stores,
    analyze_missing_items,
    search_kroger_products,
    build_grocery_cart,
)

# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator system prompt
# ─────────────────────────────────────────────────────────────────────────────

# Being explicit about step order prevents the model from skipping steps or
# calling tools in the wrong sequence. The final JSON contract is also spelled
# out so the FastAPI layer can parse the response without fragile string matching.
ORCHESTRATOR_INSTRUCTION = """You are the Smart Grocery planning agent for Junbi, an AI kitchen assistant.

Your job is to build a personalized grocery cart for the user by following these steps IN ORDER:

**Step 1 — Load context**
Call load_user_context with the user_id provided. This loads their pantry, saved recipes,
preferences, and zip code. Note the zip code — you'll need it later.

**Step 2 — Resolve nearby stores**
Call resolve_nearby_stores (no arguments needed — uses the zip code from Step 1).
Review the store rankings and note the recommended store slug.

**Step 3 — Analyze missing items**
Call analyze_missing_items with:
  - budget: the user's weekly budget (from Step 1 preferences, or null if not set)
This returns a list of items to buy based on pantry gaps, expiring items, and missing staples.

**Step 4 — Search Kroger**
Call search_kroger_products with:
  - delivery_preference: "pickup" or "delivery" (from the user's original request)
This searches for every missing item at the nearest Kroger store.

**Step 5 — Build cart**
Call build_grocery_cart with:
  - store: the recommended store slug from Step 2 (e.g. "kroger")
This selects the best product per item, computes price comparisons, and assembles the final cart.

**Step 6 — Return results**
After build_grocery_cart completes, respond with ONLY a valid JSON object (no prose, no markdown):
{
  "status": "ready_for_review",
  "store": "<store slug>",
  "cart_total": <float>,
  "item_count": <int>,
  "summary": "<1-sentence summary for the user, e.g. 'Found 12 of 14 items at Kroger for an estimated $47.20'>"
}

**Rules:**
- Follow the steps in order — do not skip any step
- Do not ask the user questions — proceed with the information available
- If a tool returns an error, note it in the summary but continue
- Never expose raw pantry data or recipe lists to the user
- Respond only with the final JSON after Step 5 completes"""


# ─────────────────────────────────────────────────────────────────────────────
# Agent instance
# ─────────────────────────────────────────────────────────────────────────────

# Instantiated once at module load and shared across all requests via runner.py.
# Tools are stateless functions — ToolContext.state is per-session, not per-agent,
# so sharing this agent instance across concurrent requests is safe.
smart_grocery_agent = LlmAgent(
    name="smart_grocery_orchestrator",
    model="gemini-2.0-flash",
    instruction=ORCHESTRATOR_INSTRUCTION,
    tools=[
        load_user_context,       # Step 1: DB context pull
        resolve_nearby_stores,   # Step 2: store ranking
        analyze_missing_items,   # Step 3: Claude pantry analysis
        search_kroger_products,  # Step 4: Kroger product search (via kroger_agent)
        build_grocery_cart,      # Step 5: Claude cart building + price comparison
    ],
)
