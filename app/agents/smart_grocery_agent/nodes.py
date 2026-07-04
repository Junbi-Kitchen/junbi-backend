"""
smart_grocery_agent/nodes.py
─────────────────────────────
LangGraph node functions for the Smart Grocery graph (see graph.py).

Each node is `async def node(state: SmartGroceryState) -> dict` — it reads
whatever it needs from state and returns a partial dict that LangGraph merges
back in before running the next node. Nothing here talks to an LLM to decide
*what* to call next; the graph edges in graph.py fix the order, so Claude is
only invoked where actual reasoning is needed (analyze_pantry, build_cart).

Node overview
─────────────
  load_context(state)      DB: reads pantry_items, saved_recipes, grocery_items,
                            user_preferences, user_addresses in parallel.

  resolve_stores(state)    Scores Kroger-family stores by budget, dietary tags,
                            and household size fit.

  analyze_pantry(state)    Calls Claude (claude-sonnet-4-6) with the pantry +
                            recipe data to identify recipe gaps, expiring items,
                            and missing staples. Falls back to _stub_analysis()
                            if ANTHROPIC_API_KEY is unset.

  search_store(state)      Calls search_kroger() from kroger_client/ with the
                            missing item names — real live Kroger prices.
                            Falls back to stub data if Kroger creds are unset.

  compare_prices(state)    Estimates cart totals across store tiers (kroger/
                            walmart/whole_foods/etc) from the Kroger baseline.

  build_cart(state)        Picks the best product per item (Claude-refined when
                            available, otherwise the Kroger cart preview as-is)
                            and writes a deterministic agent_summary string.

  human_checkpoint(state)  Pauses the graph via interrupt() and waits for the
                            frontend to confirm/cancel via runner.confirm_grocery_order().

  place_order(state)       Adds confirmed items to the user's real Kroger cart.
  finalize(state)          Writes the confirmed cart to grocery_lists / grocery_items.
  cancelled(state)         Terminal no-op when the user rejects the cart.

Internal helpers (not graph nodes)
───────────────────────────────────
  _score_store()              — preference-fit score for a store slug
  _compute_price_comparison() — estimates cart totals across store tiers
  _cart_from_kroger_preview() — maps Kroger cart_preview into CartItem shape
  _claude_refine_cart()       — Claude prompt to pick best products + aisles
  _stub_analysis()            — fallback missing items when Claude is unavailable
"""

import asyncio
import json
import logging

import anthropic
from langgraph.types import interrupt

from app.db import get_async_pool
from app.config import settings
from app.agents.kroger_client import search_kroger

from .state import SmartGroceryState

logger = logging.getLogger(__name__)

# Common pantry staples checked during analyze_pantry.
# Claude will flag any of these that are completely absent from the user's pantry.
_STAPLES = [
    "olive oil", "garlic", "eggs", "butter", "salt", "black pepper",
    "onion", "milk", "flour", "sugar", "chicken broth", "canned tomatoes",
]

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
# Node 1 — Load user context from DB
# ---------------------------------------------------------------------------

async def load_context(state: SmartGroceryState) -> dict:
    """
    Load the user's pantry, saved recipes, active grocery list, preferences,
    and default address from the database.
    """
    user_id = state["user_id"]
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

    return {
        "pantry_items": pantry_items,
        "saved_recipes": saved_recipes,
        "existing_grocery_items": existing_items,
        "user_preferences": user_preferences,
        "user_address": user_address,
    }


# ---------------------------------------------------------------------------
# Node 2 — Resolve nearby stores
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


async def resolve_stores(state: SmartGroceryState) -> dict:
    """
    Find nearby stores based on the user's zip code loaded by load_context.
    Scores and ranks them by fit with the user's preferences.
    """
    zip_code = state.get("user_address")
    prefs = state.get("user_preferences", {})

    if not zip_code:
        return {"nearby_stores": [], "error": "No zip code found for this user"}

    # Kroger-family stores available via the Kroger location search.
    # For now use a static nearby store list as a placeholder — search_store
    # resolves the actual location_id when searching products.
    # TODO: call kroger_client's location search here for a richer list.
    kroger_slug = "kroger"
    score, insight, insight_type = _score_store(kroger_slug, prefs)

    nearby_stores = [{
        "store_id": None,  # resolved by search_store at search time
        "name": "Kroger",
        "slug": kroger_slug,
        "distance_miles": None,
        "insight": insight,
        "insight_type": insight_type,
        "recommended": True,
        "supports_delivery": True,
        "supports_pickup": True,
    }]

    return {"nearby_stores": nearby_stores, "store_preference": kroger_slug}


# ---------------------------------------------------------------------------
# Node 3 — Analyze pantry and identify missing items
# ---------------------------------------------------------------------------

async def analyze_pantry(state: SmartGroceryState) -> dict:
    """
    Identify what the user needs to buy by sending their pantry and recipe data to Claude.

    Three categories of missing items:
      - recipe_gap (priority 2): ingredients needed for saved recipes not in pantry
      - expiring_soon (priority 1): items expiring within 3 days that need replacing
      - staple (priority 3): common pantry staples completely absent

    Falls back to _stub_analysis() if ANTHROPIC_API_KEY is not configured.
    """
    pantry_items = state.get("pantry_items", [])
    saved_recipes = state.get("saved_recipes", [])
    existing_items = state.get("existing_grocery_items", [])
    prefs = state.get("user_preferences", {})
    household_size = prefs.get("householdSize", 1)
    dietary_tags = prefs.get("dietaryTags", [])
    effective_budget = state.get("budget") or prefs.get("weeklyBudget") or 0

    if not settings.ANTHROPIC_API_KEY:
        missing = _stub_analysis(pantry_items, saved_recipes)
        return {"missing_items": missing, "budget": effective_budget}

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
        response = await asyncio.to_thread(
            client.messages.create,
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
        logger.error("analyze_pantry Claude call failed: %s", e)
        missing = _stub_analysis(pantry_items, saved_recipes)

    return {"missing_items": missing, "budget": effective_budget}


# ---------------------------------------------------------------------------
# Node 4 — Search Kroger products for all missing items
# ---------------------------------------------------------------------------

async def search_store(state: SmartGroceryState) -> dict:
    """
    Search the Kroger API for every item in missing_items.

    Delegates to search_kroger() in kroger_client/, which:
      1. Fetches an app-level OAuth token via client credentials
      2. Finds the nearest Kroger store by zip code
      3. Searches products for each item (up to 5 results per item)
      4. Picks the best match per item and returns a cart_preview

    Falls back gracefully: if credentials are missing, search_kroger() returns
    stub data — the cart can still be built and reviewed without live API calls.
    """
    missing_items = state.get("missing_items", [])
    zip_code = state.get("user_address")

    if not missing_items:
        return {"kroger_result": {}, "error": "No missing items to search for"}

    if not zip_code:
        return {"kroger_result": {}, "error": "No zip code available for store lookup"}

    item_names = [i["name"] for i in missing_items]
    quantities = {i["name"]: i.get("quantity", 1) for i in missing_items}
    delivery_preference = state.get("delivery_preference", "delivery")

    kroger_result = await search_kroger(
        items=item_names,
        zip_code=zip_code,
        quantities=quantities,
        delivery_preference=delivery_preference,
    )

    update: dict = {"kroger_result": kroger_result}

    if kroger_result.get("store"):
        nearby_stores = list(state.get("nearby_stores", []))
        if nearby_stores:
            nearby_stores[0] = {**nearby_stores[0], "store_id": kroger_result["store"].get("location_id")}
            update["nearby_stores"] = nearby_stores

    return update


# ---------------------------------------------------------------------------
# Node 5 — Compare prices across store chains
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
    once a per-chain product search is implemented.
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


async def compare_prices(state: SmartGroceryState) -> dict:
    """Estimate cart totals across store tiers using the real Kroger prices as a baseline."""
    missing_items = state.get("missing_items", [])
    kroger_result = state.get("kroger_result", {})
    store = state.get("store_preference", "kroger")

    price_comparison, savings_summary = _compute_price_comparison(
        missing_items, kroger_result.get("results", {}), store
    )
    return {"price_comparison": price_comparison, "savings_summary": savings_summary}


# ---------------------------------------------------------------------------
# Node 6 — Build the final grocery cart
# ---------------------------------------------------------------------------

def _cart_from_kroger_preview(cart_preview: list, store: str) -> list:
    """
    Convert the Kroger cart_preview directly into CartItem dicts.
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


async def build_cart(state: SmartGroceryState) -> dict:
    """
    Assemble the final grocery cart.

    If Kroger returned product matches and Claude is configured, calls
    _claude_refine_cart() to re-rank products and assign aisle labels.
    Otherwise uses the Kroger cart_preview directly. Also synthesizes
    agent_summary — a one-sentence, deterministic summary for the frontend
    (there's no orchestrator LLM anymore to produce free text).
    """
    missing_items = state.get("missing_items", [])
    kroger_result = state.get("kroger_result", {})
    budget = state.get("budget")
    delivery_preference = state.get("delivery_preference", "delivery")
    prefs = state.get("user_preferences", {})
    store = state.get("store_preference", "kroger")

    search_results = kroger_result.get("results", {})
    cart_preview = kroger_result.get("cart_preview", [])

    if cart_preview and settings.ANTHROPIC_API_KEY:
        cart_items, cart_total = await _claude_refine_cart(
            missing_items, search_results, cart_preview, store, budget,
            delivery_preference, prefs
        )
    else:
        cart_items = _cart_from_kroger_preview(cart_preview, store)
        cart_total = kroger_result.get("estimated_total", 0.0)

    store_name = (kroger_result.get("store") or {}).get("name", store)
    if budget:
        if cart_total <= budget:
            agent_summary = (
                f"Found {len(cart_items)} items at {store_name} for "
                f"${cart_total:.2f} — ${budget - cart_total:.2f} under your ${budget:.0f} budget."
            )
        else:
            priciest = max(cart_items, key=lambda i: i.get("estimated_price") or 0, default=None)
            over_note = f" {priciest['name'].title()} is the priciest item." if priciest else ""
            agent_summary = (
                f"Found {len(cart_items)} items at {store_name} for ${cart_total:.2f} — "
                f"${cart_total - budget:.2f} over your ${budget:.0f} budget.{over_note}"
            )
    else:
        agent_summary = f"Found {len(cart_items)} items at {store_name} for ${cart_total:.2f}."

    return {"cart_items": cart_items, "cart_total": cart_total, "agent_summary": agent_summary}


# ---------------------------------------------------------------------------
# Node 7 — Human checkpoint (interrupt)
# ---------------------------------------------------------------------------

async def human_checkpoint(state: SmartGroceryState) -> dict:
    """
    Pause the graph and wait for the user to review the cart.

    interrupt() suspends execution here — ainvoke() on the graph returns to
    the caller with the state as computed so far (runner.start_grocery_agent
    surfaces this as the cart-for-review response). The graph resumes only
    when runner.confirm_grocery_order() calls ainvoke(Command(resume=...))
    with the same thread_id — never skip straight past this node.
    """
    decision = interrupt({
        "cart_items": state.get("cart_items", []),
        "cart_total": state.get("cart_total", 0),
        "store": state.get("store_preference"),
    })
    return {
        "confirmed": bool(decision.get("confirmed")),
        "store_override": decision.get("store_override"),
    }


# ---------------------------------------------------------------------------
# Node 8 — Place order (Kroger cart)
# ---------------------------------------------------------------------------

async def place_order(state: SmartGroceryState) -> dict:
    """
    Add confirmed cart items to the user's real Kroger cart via the Kroger Cart API.

    Flow:
      1. Fetch the user's Kroger OAuth access token from connected_accounts table
         (provider='kroger'). Returns "pending_oauth" if not linked yet.
      2. Filter cart_items to those with real Kroger product_ids (not stub- prefixed).
      3. PUT /v1/cart/add with the item UPCs and quantities.

    The Kroger Cart API is write-only — it cannot read back or remove cart items.
    Returns a status dict (not raises) so a cart failure doesn't block finalize().

    TODO: Build the OAuth linking flow to populate connected_accounts:
      POST /auth/kroger/connect → returns OAuth URL for the frontend to open
      GET  /auth/kroger/callback → exchanges code, stores token in connected_accounts
    """
    if state.get("store_override"):
        state = {**state, "store_preference": state["store_override"]}

    cart_items = state.get("cart_items", [])
    kroger_result = state.get("kroger_result", {})
    user_id = state["user_id"]

    if not settings.KROGER_CLIENT_ID:
        return {"kroger_cart_status": {"status": "stub", "message": "Kroger credentials not configured"}}

    pool = get_async_pool()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT access_token FROM connected_accounts
                       WHERE user_id = %s AND provider = 'kroger'
                       AND expires_at > NOW() LIMIT 1""",
                    (user_id,)
                )
                row = await cur.fetchone()
    except Exception:
        row = None

    if not row:
        return {"kroger_cart_status": {
            "status": "pending_oauth",
            "message": "User has not linked their Kroger account yet",
        }}

    items_payload = []
    store_info = kroger_result.get("store", {})
    location_id = store_info.get("location_id") if store_info else None

    for item in cart_items:
        if item.get("product_id") and not item["product_id"].startswith("stub-"):
            items_payload.append({
                "upc": item["product_id"],
                "quantity": int(max(item.get("quantity", 1), 1)),
                "modality": "PICKUP",
            })

    if not items_payload:
        return {"kroger_cart_status": {"status": "no_valid_products", "message": "No Kroger product IDs available to add"}}

    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                "https://api.kroger.com/v1/cart/add",
                headers={
                    "Authorization": f"Bearer {row['access_token']}",
                    "Content-Type": "application/json",
                },
                json={"items": items_payload},
                timeout=10.0,
            )
            resp.raise_for_status()
        return {"kroger_cart_status": {"status": "added", "item_count": len(items_payload), "location_id": location_id}}
    except Exception as e:
        logger.error("Kroger cart add failed: %s", e)
        return {"kroger_cart_status": {"status": "error", "message": str(e)}}


# ---------------------------------------------------------------------------
# Node 9 — Finalize (write confirmed cart to DB)
# ---------------------------------------------------------------------------

async def finalize(state: SmartGroceryState) -> dict:
    """
    Persist the confirmed cart to the database as a grocery list.

    Finds or creates the user's active grocery_list, then upserts each cart
    item into grocery_items with its ingredient_id, aisle, and estimated_price.
    Updates the list's estimated_total on completion.

    Failure is logged but not raised — a DB write failure doesn't roll back the
    Kroger cart add that already happened in place_order().
    """
    cart_items = state.get("cart_items", [])
    user_id = state["user_id"]

    if not cart_items:
        return {"grocery_list_id": None, "status": "order_placed"}

    pool = get_async_pool()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM grocery_lists WHERE user_id = %s AND status = 'active' ORDER BY created_at LIMIT 1",
                    (user_id,)
                )
                row = await cur.fetchone()
                if row:
                    list_id = str(row["id"])
                else:
                    await cur.execute(
                        "INSERT INTO grocery_lists (user_id, name, status) VALUES (%s, 'My List', 'active') RETURNING id",
                        (user_id,)
                    )
                    list_id = str((await cur.fetchone())["id"])

                for item in cart_items:
                    name = item.get("name", "")
                    if not name:
                        continue
                    await cur.execute(
                        "INSERT INTO ingredients (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                        (name,)
                    )
                    await cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
                    ingredient_id = str((await cur.fetchone())["id"])
                    await cur.execute("""
                        INSERT INTO grocery_items
                            (list_id, name, ingredient_id, quantity, unit, aisle, estimated_price)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        list_id, name, ingredient_id,
                        item.get("quantity", 1),
                        item.get("unit", ""),
                        item.get("aisle", "Pantry"),
                        item.get("estimated_price"),
                    ))
                await cur.execute(
                    "UPDATE grocery_lists SET estimated_total = %s WHERE id = %s",
                    (state.get("cart_total"), list_id)
                )
        return {"grocery_list_id": list_id, "status": "order_placed"}
    except Exception as e:
        logger.error("finalize failed: %s", e)
        return {"grocery_list_id": None, "status": "order_placed"}


# ---------------------------------------------------------------------------
# Node 10 — Cancelled (terminal, when the user rejects the cart)
# ---------------------------------------------------------------------------

async def cancelled(state: SmartGroceryState) -> dict:
    """Terminal node — the cart is discarded, nothing is written or ordered."""
    return {"status": "cancelled"}
