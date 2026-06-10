"""
smart_grocery_agent/tools.py
─────────────────────────────
ADK tool functions called by the Smart Grocery orchestrator agent.

Why ToolContext.state?
──────────────────────
In a normal function-calling flow, the LLM would pass data from one tool to
the next as arguments — e.g. pass the pantry list from load_user_context into
analyze_missing_items. That works for small data but breaks down for grocery
planning where pantry + recipe dumps can be thousands of tokens.

Instead, all tools share an ADK session-scoped state dict (tool_context.state).
Each tool reads what it needs and writes its output back. The LLM only receives
a short summary string from each call, keeping Gemini token usage minimal.

Tool overview
─────────────
  load_user_context(user_id)
      DB: reads pantry_items, saved_recipes, grocery_items, user_preferences,
          user_addresses in parallel. Writes full data to state; returns counts.

  resolve_nearby_stores()
      Scores Kroger-family stores by how well they match user budget, dietary
      tags, and household size. Writes store list to state; returns top 3.

  analyze_missing_items(budget)
      Calls Claude (claude-sonnet-4-6) with the pantry + recipe data from state.
      Claude identifies recipe gaps, expiring items, and missing staples.
      Writes missing_items list to state; returns that list as JSON.

  search_kroger_products(delivery_preference)
      Calls search_kroger() from kroger_agent/ with the missing item names.
      The Kroger agent uses kroger-mcp to search the live Kroger API.
      Writes kroger_result to state; returns a short found/unfound summary.

  build_grocery_cart(store)
      Computes price comparison across store tiers (kroger/walmart/whole_foods/etc).
      Optionally calls Claude to refine product selection if Kroger results are
      available. Writes cart_items, cart_total, price_comparison to state.

Internal helpers (not called by the LLM)
─────────────────────────────────────────
  _score_store()           — computes a preference-fit score for a store slug
  _compute_price_comparison() — estimates cart totals across store tiers
  _cart_from_kroger_preview() — maps Kroger cart_preview into CartItem shape
  _claude_refine_cart()    — Claude prompt to pick best products + assign aisles
  _stub_analysis()         — fallback missing items when ANTHROPIC_API_KEY absent
"""

import asyncio
import json
import logging

import anthropic
from google.adk.tools import ToolContext

from app.db import get_async_pool
from app.config import settings
from app.agents.kroger_client import search_kroger

logger = logging.getLogger(__name__)

# Common pantry staples checked during analyze_missing_items.
# Claude will flag any of these that are completely absent from the user's pantry.
_STAPLES = [
    "olive oil", "garlic", "eggs", "butter", "salt", "black pepper",
    "onion", "milk", "flour", "sugar", "chicken broth", "canned tomatoes",
]

# Maps store display names (from Instacart / kroger-mcp) → internal slug.
# Used to normalize store names before scoring with _STORE_TRAITS.
_NAME_TO_SLUG: dict[str, str] = {
    "walmart": "walmart", "costco": "costco", "aldi": "aldi",
    "kroger": "kroger", "publix": "publix",
    "whole foods": "whole_foods", "whole foods market": "whole_foods",
    "trader joe's": "trader_joes", "trader joes": "trader_joes",
    "sprouts": "sprouts", "sprouts farmers market": "sprouts",
    "target": "target", "safeway": "safeway",
    "albertsons": "albertsons", "heb": "heb", "meijer": "meijer",
}

# Static store traits used by _score_store() to rank stores by preference fit.
# price_rank: 1=cheapest, 5=most expensive (relative to national average)
# quality_rank: 1=lowest, 5=highest (freshness, organic selection, variety)
# bulk: True if the store is bulk/warehouse format (rewarded for large households)
_STORE_TRAITS: dict[str, dict] = {
    "walmart":     {"price_rank": 1, "quality_rank": 2, "bulk": False},
    "aldi":        {"price_rank": 2, "quality_rank": 2, "bulk": False},
    "costco":      {"price_rank": 1, "quality_rank": 3, "bulk": True},
    "kroger":      {"price_rank": 3, "quality_rank": 3, "bulk": False},
    "target":      {"price_rank": 3, "quality_rank": 3, "bulk": False},
    "safeway":     {"price_rank": 3, "quality_rank": 3, "bulk": False},
    "albertsons":  {"price_rank": 3, "quality_rank": 3, "bulk": False},
    "heb":         {"price_rank": 2, "quality_rank": 4, "bulk": False},
    "meijer":      {"price_rank": 2, "quality_rank": 3, "bulk": False},
    "publix":      {"price_rank": 4, "quality_rank": 4, "bulk": False},
    "trader_joes": {"price_rank": 3, "quality_rank": 4, "bulk": False},
    "sprouts":     {"price_rank": 4, "quality_rank": 5, "bulk": False},
    "whole_foods": {"price_rank": 5, "quality_rank": 5, "bulk": False},
}

# Dietary tags that signal the user prefers higher-quality/specialty stores.
# Used in _score_store() to boost quality_rank weight for these users.
_QUALITY_TAGS = {"organic", "non-gmo", "gluten-free", "vegan", "vegetarian", "keto", "paleo"}


# ---------------------------------------------------------------------------
# Tool 1 — Load user context from DB
# ---------------------------------------------------------------------------

async def load_user_context(user_id: str, tool_context: ToolContext) -> str:
    """
    Load the user's pantry, saved recipes, active grocery list, preferences,
    and default address from the database.

    Returns a short summary. Full data is stored in session state for other tools.
    """
    pool = get_async_pool()

    async def _pantry(conn):
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT p.id, p.name, p.quantity, p.unit, p.expiry_date,
                       p.freshness_status, p.location,
                       COALESCE(ic.slug, 'pantry') AS category
                FROM pantry_items_with_freshness p
                LEFT JOIN ingredients i ON i.id = p.ingredient_id
                LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
                WHERE p.user_id = %s AND p.is_active = true
                ORDER BY p.expiry_date ASC NULLS LAST
            """, (user_id,))
            return [
                {
                    "id": str(r["id"]),
                    "name": r["name"] or "",
                    "quantity": float(r["quantity"] or 0),
                    "unit": r["unit"] or "",
                    "expiryDate": r["expiry_date"].isoformat() if r["expiry_date"] else None,
                    "freshnessStatus": r["freshness_status"],
                    "category": r["category"],
                }
                for r in await cur.fetchall()
            ]

    async def _recipes(conn):
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT r.id, r.title, r.cook_time_mins,
                       json_agg(json_build_object(
                           'name', COALESCE(ri.display_text, i.name),
                           'quantity', ri.quantity,
                           'unit', ri.unit
                       )) AS ingredients
                FROM recipes r
                JOIN user_recipe_interactions uri ON uri.recipe_id = r.id
                JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                LEFT JOIN ingredients i ON i.id = ri.ingredient_id
                WHERE uri.user_id = %s AND uri.action = 'saved'
                GROUP BY r.id
            """, (user_id,))
            return [
                {
                    "id": str(r["id"]),
                    "title": r["title"],
                    "cookTimeMinutes": r["cook_time_mins"],
                    "ingredients": r["ingredients"] or [],
                }
                for r in await cur.fetchall()
            ]

    async def _grocery(conn):
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT gi.name, gi.quantity, gi.unit, gi.is_checked
                FROM grocery_items gi
                JOIN grocery_lists gl ON gl.id = gi.list_id
                WHERE gl.user_id = %s AND gl.status = 'active'
            """, (user_id,))
            return [
                {"name": r["name"], "quantity": float(r["quantity"] or 0),
                 "unit": r["unit"] or "", "checked": r["is_checked"]}
                for r in await cur.fetchall()
            ]

    async def _prefs(conn):
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT dietary_tags, allergies, cuisines, household_size, weekly_budget
                FROM user_preferences WHERE user_id = %s
            """, (user_id,))
            row = await cur.fetchone()
            return {
                "dietaryTags": list(row["dietary_tags"] or []) if row else [],
                "allergies": list(row["allergies"] or []) if row else [],
                "cuisines": list(row["cuisines"] or []) if row else [],
                "householdSize": row["household_size"] if row else 1,
                "weeklyBudget": float(row["weekly_budget"] or 0) if row else 0,
            }

    async def _address(conn):
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT zip FROM user_addresses WHERE user_id = %s AND is_default = true LIMIT 1",
                (user_id,)
            )
            row = await cur.fetchone()
            if not row:
                await cur.execute(
                    "SELECT zip FROM user_addresses WHERE user_id = %s ORDER BY created_at LIMIT 1",
                    (user_id,)
                )
                row = await cur.fetchone()
            return row["zip"] if row else None

    async with (
        pool.connection() as c1,
        pool.connection() as c2,
        pool.connection() as c3,
        pool.connection() as c4,
        pool.connection() as c5,
    ):
        pantry_items, saved_recipes, existing_items, user_preferences, user_address = (
            await asyncio.gather(_pantry(c1), _recipes(c2), _grocery(c3), _prefs(c4), _address(c5))
        )

    tool_context.state["user_id"] = user_id
    tool_context.state["pantry_items"] = pantry_items
    tool_context.state["saved_recipes"] = saved_recipes
    tool_context.state["existing_grocery_items"] = existing_items
    tool_context.state["user_preferences"] = user_preferences
    tool_context.state["user_address"] = user_address

    expiring_count = sum(
        1 for i in pantry_items
        if i.get("freshnessStatus") in ("expiring", "use_soon", "expired")
    )

    return json.dumps({
        "pantry_items": len(pantry_items),
        "expiring_soon": expiring_count,
        "saved_recipes": len(saved_recipes),
        "existing_grocery_items": len(existing_items),
        "zip_code": user_address,
        "dietary_tags": user_preferences.get("dietaryTags", []),
        "weekly_budget": user_preferences.get("weeklyBudget", 0),
        "household_size": user_preferences.get("householdSize", 1),
    })


# ---------------------------------------------------------------------------
# Tool 2 — Resolve nearby stores
# ---------------------------------------------------------------------------

def _score_store(slug: str, prefs: dict) -> tuple[float, str, str]:
    """
    Score a store by how well it fits the user's preferences.

    Scoring logic:
      - Price-sensitive users (budget ≤ $150/week): weight low price_rank heavily
      - Quality-sensitive users (organic/vegan/etc dietary tags): weight quality_rank
      - Large households (4+): bonus for bulk/warehouse stores
      - Neutral users: reward stores in the middle of both spectrums

    Returns (score, insight_text, insight_type) where insight_type is one of:
      "price" | "quality" | "bulk" | "balanced"
    """
    traits = _STORE_TRAITS.get(slug, {"price_rank": 3, "quality_rank": 3, "bulk": False})
    price_rank = traits["price_rank"]
    quality_rank = traits["quality_rank"]
    is_bulk = traits["bulk"]
    budget = prefs.get("weeklyBudget", 0)
    dietary_tags = {t.lower() for t in prefs.get("dietaryTags", [])}
    household_size = prefs.get("householdSize", 1)
    is_price_sensitive = 0 < budget <= 150
    is_quality_sensitive = bool(dietary_tags & _QUALITY_TAGS)
    wants_bulk = household_size >= 4
    score = 0.0
    if is_price_sensitive:
        score += (6 - price_rank) * 2.5
    if is_quality_sensitive:
        score += quality_rank * 2.5
    if wants_bulk and is_bulk:
        score += 4.0
    if not is_price_sensitive and not is_quality_sensitive:
        score += (4 - abs(price_rank - 2.5)) + (4 - abs(quality_rank - 3.5))
    if is_bulk and wants_bulk:
        insight, insight_type = f"Best bulk value — great for household of {household_size}", "bulk"
    elif price_rank <= 2 and is_price_sensitive:
        insight, insight_type = "Lowest prices nearby — best fit for your budget", "price"
    elif quality_rank >= 4 and is_quality_sensitive:
        insight, insight_type = "Best produce quality — matches your dietary preferences", "quality"
    elif price_rank <= 2:
        insight, insight_type = "Cheapest option nearby", "price"
    elif quality_rank >= 4:
        insight, insight_type = "Better produce quality and longer shelf life", "quality"
    else:
        insight, insight_type = "Good balance of price and quality", "balanced"
    return score, insight, insight_type


async def resolve_nearby_stores(tool_context: ToolContext) -> str:
    """
    Find nearby stores based on the user's zip code loaded in context.
    Scores and ranks them by fit with the user's preferences.

    Returns the top 3 ranked stores. Full list stored in session state.
    """
    zip_code = tool_context.state.get("user_address")
    prefs = tool_context.state.get("user_preferences", {})

    if not zip_code:
        tool_context.state["nearby_stores"] = []
        return json.dumps({"error": "No zip code found for this user"})

    # Kroger-family stores available via the kroger-mcp location search
    # For now use static nearby store list as a placeholder — the kroger_agent
    # will resolve the actual location_id when searching products.
    # TODO: call Instacart getStores() or kroger search_locations here for a richer list.
    kroger_slug = "kroger"
    score, insight, insight_type = _score_store(kroger_slug, prefs)

    nearby_stores = [{
        "store_id": None,  # resolved by kroger_agent at search time
        "name": "Kroger",
        "slug": kroger_slug,
        "distance_miles": None,
        "insight": insight,
        "insight_type": insight_type,
        "recommended": True,
        "supports_delivery": True,
        "supports_pickup": True,
    }]

    tool_context.state["nearby_stores"] = nearby_stores
    tool_context.state["store_preference"] = kroger_slug

    return json.dumps(nearby_stores[:3])


# ---------------------------------------------------------------------------
# Tool 3 — Analyze pantry and identify missing items
# ---------------------------------------------------------------------------

async def analyze_missing_items(budget: float | None, tool_context: ToolContext) -> str:
    """
    Identify what the user needs to buy by sending their pantry and recipe data to Claude.

    Three categories of missing items:
      - recipe_gap (priority 2): ingredients needed for saved recipes not in pantry
      - expiring_soon (priority 1): items expiring within 3 days that need replacing
      - staple (priority 3): common pantry staples completely absent

    Reads from state: pantry_items, saved_recipes, existing_grocery_items, user_preferences
    Writes to state:  missing_items (list), budget (float)
    Returns: JSON array of missing items — the LLM sees the full list for context.

    Falls back to _stub_analysis() if ANTHROPIC_API_KEY is not configured.
    """
    pantry_items = tool_context.state.get("pantry_items", [])
    saved_recipes = tool_context.state.get("saved_recipes", [])
    existing_items = tool_context.state.get("existing_grocery_items", [])
    prefs = tool_context.state.get("user_preferences", {})
    household_size = prefs.get("householdSize", 1)
    dietary_tags = prefs.get("dietaryTags", [])
    effective_budget = budget or prefs.get("weeklyBudget") or 0

    if not settings.ANTHROPIC_API_KEY:
        missing = _stub_analysis(pantry_items, saved_recipes)
        tool_context.state["missing_items"] = missing
        tool_context.state["budget"] = effective_budget
        return json.dumps(missing)

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    prompt = f"""You are a smart grocery assistant for a household of {household_size}.

PANTRY (current items):
{json.dumps(pantry_items, indent=2)}

SAVED RECIPES (recipes this user wants to cook):
{json.dumps([{"title": r["title"], "ingredients": r["ingredients"]} for r in saved_recipes], indent=2)}

ALREADY ON GROCERY LIST (don't duplicate):
{json.dumps([i["name"] for i in existing_items])}

DIETARY RESTRICTIONS: {", ".join(dietary_tags) or "none"}
WEEKLY BUDGET: ${effective_budget or "not set"}
COMMON STAPLES TO CHECK: {", ".join(_STAPLES)}

Identify what this user should buy. Return a JSON array. Each item:
  - name: string (simple grocery name, e.g. "eggs")
  - quantity: number
  - unit: string (e.g. "dozen", "lbs", "count")
  - reason: "recipe_gap" | "expiring_soon" | "staple"
  - priority: 1 (expiring), 2 (recipe gap), 3 (staple)

Rules: prioritize expiring items, only flag genuinely missing recipe ingredients,
only flag fully absent staples, ignore already-listed items, respect dietary restrictions.
Aim for 10-20 items max. Respond with ONLY a valid JSON array."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        missing = json.loads(raw)
    except Exception as e:
        logger.error("analyze_missing_items Claude call failed: %s", e)
        missing = _stub_analysis(pantry_items, saved_recipes)

    tool_context.state["missing_items"] = missing
    tool_context.state["budget"] = effective_budget

    return json.dumps(missing)


# ---------------------------------------------------------------------------
# Tool 4 — Search Kroger products for all missing items
# ---------------------------------------------------------------------------

async def search_kroger_products(
    delivery_preference: str,
    tool_context: ToolContext,
) -> str:
    """
    Search the Kroger API for every item in the missing_items list.

    Delegates to search_kroger() in kroger_agent/, which:
      1. Starts the kroger-mcp subprocess
      2. Calls search_locations to find the nearest Kroger store by zip code
      3. Calls search_products for each item (up to 5 results per item)
      4. Picks the best match per item and returns a cart_preview

    Reads from state: missing_items, user_address
    Writes to state:  kroger_result (full KrogerResult), delivery_preference,
                      nearby_stores[0].store_id (backfilled from actual location)
    Returns: compact JSON with store info, found/unfound counts, estimated total.

    Falls back gracefully: if credentials are missing, search_kroger() returns
    stub data — the cart can still be built and reviewed without live API calls.
    """
    missing_items = tool_context.state.get("missing_items", [])
    zip_code = tool_context.state.get("user_address")

    if not missing_items:
        tool_context.state["kroger_result"] = {}
        return json.dumps({"error": "No missing items to search for"})

    if not zip_code:
        tool_context.state["kroger_result"] = {}
        return json.dumps({"error": "No zip code available for store lookup"})

    item_names = [i["name"] for i in missing_items]
    quantities = {i["name"]: i.get("quantity", 1) for i in missing_items}

    kroger_result = await search_kroger(
        items=item_names,
        zip_code=zip_code,
        quantities=quantities,
        delivery_preference=delivery_preference,
    )

    tool_context.state["kroger_result"] = kroger_result
    tool_context.state["delivery_preference"] = delivery_preference

    if kroger_result.get("store"):
        tool_context.state["nearby_stores"][0]["store_id"] = kroger_result["store"].get("location_id")

    found_count = len(item_names) - len(kroger_result.get("unfound", []))
    return json.dumps({
        "store": kroger_result.get("store"),
        "items_found": found_count,
        "items_not_found": kroger_result.get("unfound", []),
        "estimated_total": kroger_result.get("estimated_total", 0),
    })


# ---------------------------------------------------------------------------
# Tool 5 — Build the final grocery cart
# ---------------------------------------------------------------------------

async def build_grocery_cart(
    store: str,
    tool_context: ToolContext,
) -> str:
    """
    Assemble the final grocery cart and compute cross-store price comparison.

    Two steps:
      1. Price comparison — estimate cart totals across store tiers (kroger/walmart/
         whole_foods/etc) using the actual Kroger prices as a baseline and applying
         per-chain price index multipliers.
      2. Cart building — if Kroger returned product matches and Claude is configured,
         calls _claude_refine_cart() to re-rank products and assign aisle labels.
         Otherwise uses the Kroger agent's cart_preview directly.

    Reads from state: missing_items, kroger_result, budget, delivery_preference,
                      user_preferences
    Writes to state:  price_comparison, savings_summary, cart_items, cart_total
    Returns: JSON with cart_items, cart_total, item_count, savings_summary.
    """
    missing_items = tool_context.state.get("missing_items", [])
    kroger_result = tool_context.state.get("kroger_result", {})
    budget = tool_context.state.get("budget")
    delivery_preference = tool_context.state.get("delivery_preference", "delivery")
    prefs = tool_context.state.get("user_preferences", {})

    search_results = kroger_result.get("results", {})
    cart_preview = kroger_result.get("cart_preview", [])

    # Price comparison
    price_comparison, savings_summary = _compute_price_comparison(
        missing_items, search_results, store
    )
    tool_context.state["price_comparison"] = price_comparison
    tool_context.state["savings_summary"] = savings_summary

    # Use Kroger's cart_preview if available; otherwise call Claude to build cart
    if cart_preview and settings.ANTHROPIC_API_KEY:
        cart_items, cart_total = await _claude_refine_cart(
            missing_items, search_results, cart_preview, store, budget,
            delivery_preference, prefs
        )
    else:
        cart_items = _cart_from_kroger_preview(cart_preview, store)
        cart_total = kroger_result.get("estimated_total", 0.0)

    tool_context.state["cart_items"] = cart_items
    tool_context.state["cart_total"] = cart_total

    within_budget = (cart_total <= budget) if budget else None

    return json.dumps({
        "cart_items": cart_items,
        "cart_total": cart_total,
        "item_count": len(cart_items),
        "budget": budget,
        "within_budget": within_budget,
        "over_by": round(cart_total - budget, 2) if budget and cart_total > budget else 0,
        "savings_summary": savings_summary,
        "store": kroger_result.get("store", {}),
    })


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_price_comparison(
    missing_items: list,
    search_results: dict,
    preferred_store: str,
) -> tuple[dict, dict]:
    """
    Estimate what the same cart would cost at several store chains.

    Uses the first Kroger product price per item as a baseline, then applies
    chain-level multipliers derived from general market positioning. Returns
    both the raw price_comparison dict and a savings_summary that highlights
    the best/worst/preferred store.

    Multipliers are heuristics — replace with real per-retailer search calls
    once Instacart getStores() / per-chain product search is implemented.
    """
    baseline_total = 0.0
    item_count = 0
    for item in missing_items:
        products = search_results.get(item["name"], [])
        if products and products[0].get("price"):
            baseline_total += products[0]["price"] * max(item.get("quantity", 1), 1)
            item_count += 1
    if item_count == 0:
        baseline_total = len(missing_items) * 4.0

    store_multipliers = {
        "walmart": 0.88, "costco": 0.82, "aldi": 0.85,
        "kroger": 1.0, "publix": 1.05, "whole_foods": 1.28,
    }
    price_comparison = {
        s: round(baseline_total * m, 2)
        for s, m in store_multipliers.items()
    }
    sorted_stores = sorted(price_comparison.items(), key=lambda x: x[1])
    best_store, best_price = sorted_stores[0]
    worst_store, worst_price = sorted_stores[-1]
    preferred_price = price_comparison.get(preferred_store, baseline_total)
    savings_summary = {
        "best_store": best_store,
        "best_price": best_price,
        "worst_store": worst_store,
        "preferred_store": preferred_store,
        "preferred_price": preferred_price,
        "savings_vs_worst": round(worst_price - preferred_price, 2),
        "savings_vs_worst_pct": round(
            (worst_price - preferred_price) / worst_price * 100, 1
        ) if worst_price > 0 else 0,
        "all_stores": price_comparison,
    }
    return price_comparison, savings_summary


def _cart_from_kroger_preview(cart_preview: list, store: str) -> list:
    """
    Convert the Kroger agent's cart_preview directly into CartItem dicts.
    Used as a fast path when ANTHROPIC_API_KEY is not set or Claude refinement fails.
    """
    return [
        {
            "name": item.get("name", ""),
            "quantity": item.get("quantity", 1),
            "unit": item.get("unit", ""),
            "product_id": item.get("product_id"),
            "estimated_price": item.get("estimated_price"),
            "store": store,
            "aisle": item.get("aisle"),
        }
        for item in cart_preview
    ]


async def _claude_refine_cart(
    missing_items, search_results, cart_preview, store, budget, delivery_preference, prefs
) -> tuple[list, float]:
    """
    Ask Claude to make budget-aware product selections from real Kroger prices.

    The Kroger API returns real shelf prices. Claude uses those to:
      - Check if the cart total fits the user's weekly budget
      - Prefer Kroger-brand items when money is tight
      - Flag expensive items and suggest cheaper options when over budget
      - Apply dietary tags (e.g. organic preference, gluten-free)
      - Assign aisle labels for in-store navigation

    Falls back to _cart_from_kroger_preview() if the Claude call fails.
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    kroger_total = sum((i.get("estimated_price") or 0) for i in cart_preview)
    over_budget = budget and kroger_total > budget
    budget_gap = round(kroger_total - budget, 2) if over_budget else 0

    dietary_tags = {t.lower() for t in (prefs.get("dietaryTags") or [])}
    allergies = {a.lower() for a in (prefs.get("allergies") or [])}

    def _option_summary(p: dict) -> dict:
        """Compact product summary with all personalization-relevant fields."""
        out: dict = {
            "product_id": p.get("product_id"),
            "name": p.get("name"),
            "brand": p.get("brand"),
            "price": p.get("price"),
            "sale_price": p.get("sale_price"),
            "unit": p.get("unit"),
            "organic": p.get("organic", False),
            "stock_level": p.get("stock_level"),
            "temperature": p.get("temperature"),
        }
        # Only include allergens/declarations if present (keeps prompt leaner)
        if p.get("allergens"):
            out["allergens"] = p["allergens"]
        if p.get("manufacturer_declarations"):
            out["claims"] = p["manufacturer_declarations"]
        if p.get("rating") is not None:
            out["rating"] = f"{p['rating']:.1f} ({p.get('review_count', 0)} reviews)"
        if p.get("nutrition"):
            n = p["nutrition"]
            out["nutrition_per_serving"] = {
                k: v for k, v in {
                    "serving": n.get("serving_size"),
                    "calories": n.get("calories"),
                    "protein_g": n.get("protein_g"),
                    "fat_g": n.get("fat_g"),
                    "carbs_g": n.get("carbs_g"),
                    "fiber_g": n.get("fiber_g"),
                    "sodium_mg": n.get("sodium_mg"),
                }.items() if v is not None
            }
        return out

    items_with_options = [
        {
            "item": item["name"],
            "quantity": item.get("quantity", 1),
            "unit": item.get("unit", ""),
            "reason": item.get("reason", ""),
            "options": [
                _option_summary(p)
                for p in search_results.get(item["name"], [])[:3]
            ],
            "kroger_pick": next(
                (c for c in cart_preview if c.get("name") == item["name"]), None
            ),
        }
        for item in missing_items
    ]

    budget_context = (
        f"Budget: ${budget}/week. Cart total so far: ${kroger_total:.2f}. "
        f"OVER BUDGET by ${budget_gap} — prioritize cheaper options where possible."
        if over_budget
        else f"Budget: ${budget}/week. Cart total: ${kroger_total:.2f} — within budget."
        if budget
        else "No budget set — optimize for best value."
    )

    # Build dietary guidance so Claude knows what to flag
    dietary_notes = []
    if "organic" in dietary_tags:
        dietary_notes.append("Prefer organic products (organic: true) when available.")
    if "gluten-free" in dietary_tags or "gluten free" in dietary_tags:
        dietary_notes.append("Only pick products that are gluten-free (check allergens/claims).")
    if "dairy-free" in dietary_tags or "dairy free" in dietary_tags or "vegan" in dietary_tags:
        dietary_notes.append("Avoid products containing dairy. Check allergens carefully.")
    if "keto" in dietary_tags:
        dietary_notes.append("Prefer high-protein, low-carb options (check nutrition_per_serving).")
    if allergies:
        dietary_notes.append(f"User is allergic to: {', '.join(allergies)}. NEVER pick products that 'Contains' these.")
    dietary_guidance = "\n".join(dietary_notes) if dietary_notes else "No special dietary restrictions."

    prompt = f"""You are finalizing a grocery cart at {store} using REAL prices and product data from the Kroger API.

{budget_context}
Delivery preference: {delivery_preference}

DIETARY GUIDANCE (apply strictly):
{dietary_guidance}

For each item you have up to 3 real product options with:
  - Price (use sale_price if lower than price)
  - Organic flag, allergens, manufacturer claims (Dairy Free, Gluten Free, etc.)
  - Nutrition info per serving (calories, protein, fat, carbs, sodium)
  - Stock level — skip TEMPORARILY_OUT_OF_STOCK items if alternatives exist
  - Customer rating

Pick the best product per item balancing: budget fit → dietary safety → value → quality.

Items:
{json.dumps(items_with_options, indent=2)}

Return a JSON array of cart items, each with:
  - name (original item name)
  - quantity (number)
  - unit (string)
  - product_id (from the chosen option)
  - product_name (full product name)
  - price (unit price float or null — use sale_price if on sale)
  - estimated_price (price × quantity)
  - store (use "{store}")
  - aisle (e.g. "Dairy", "Produce", "Meat" — use temperature/category to infer)
  - note (optional: why you chose this, e.g. "Organic, on sale $1.20 off" or "Cheapest option")

End with one object: {{"__total__": {{"cart_total": <sum of estimated_prices>, "item_count": <n>}}}}

Respond with ONLY a valid JSON array, no prose."""

    try:
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        summary = next((i for i in parsed if isinstance(i, dict) and "__total__" in i), {})
        cart_items = [i for i in parsed if "__total__" not in i]
        cart_total = (summary.get("__total__") or {}).get("cart_total") or sum(
            (i.get("estimated_price") or 0) for i in cart_items
        )
        return cart_items, round(float(cart_total), 2)
    except Exception as e:
        logger.error("_claude_refine_cart failed: %s", e)
        return _cart_from_kroger_preview(cart_preview, store), kroger_total


def _stub_analysis(pantry_items: list, saved_recipes: list) -> list:
    """
    Return a minimal realistic missing-items list without calling Claude.

    Used when ANTHROPIC_API_KEY is not set. Picks the first expiring pantry
    item (if any) and up to 5 staples not currently in the pantry.
    """
    pantry_names = {i["name"].lower() for i in pantry_items}
    missing = []
    expiring = [i for i in pantry_items if i.get("freshnessStatus") in ("expiring", "use_soon")]
    if expiring:
        missing.append({
            "name": expiring[0]["name"], "quantity": 1,
            "unit": expiring[0].get("unit", "count"),
            "reason": "expiring_soon", "priority": 1,
        })
    for staple in _STAPLES[:5]:
        if staple not in pantry_names:
            missing.append({"name": staple, "quantity": 1, "unit": "count", "reason": "staple", "priority": 3})
    return missing[:8]
