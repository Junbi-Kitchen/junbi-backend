"""
LangGraph nodes for the Smart Grocery agent.

Node execution order:
  load_context → resolve_stores → analyze_pantry → search_store → compare_prices
      → build_cart → [INTERRUPT: user confirms] → place_order → finalize
"""

import asyncio
import json
import logging

import anthropic
from langgraph.types import interrupt

from app.db import get_async_pool
from config import settings

from .state import SmartGroceryState
from .tools.instacart import call_instacart_service, execute_checkout, get_stores

logger = logging.getLogger(__name__)

_STAPLES = [
    "olive oil", "garlic", "eggs", "butter", "salt", "black pepper",
    "onion", "milk", "flour", "sugar", "chicken broth", "canned tomatoes",
]


# ---------------------------------------------------------------------------
# Node 1 — Load context from DB
# ---------------------------------------------------------------------------

async def load_context(state: SmartGroceryState) -> dict:
    """Pull pantry, saved recipes, grocery list, and preferences from DB in parallel."""
    uid = state["user_id"]
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
            """, (uid,))
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
            """, (uid,))
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
            """, (uid,))
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
            """, (uid,))
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
                (uid,)
            )
            row = await cur.fetchone()
            if not row:
                await cur.execute(
                    "SELECT zip FROM user_addresses WHERE user_id = %s ORDER BY created_at LIMIT 1",
                    (uid,)
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
        pantry_items, saved_recipes, existing_grocery_items, user_preferences, user_address = (
            await asyncio.gather(_pantry(c1), _recipes(c2), _grocery(c3), _prefs(c4), _address(c5))
        )

    return {
        "pantry_items": pantry_items,
        "saved_recipes": saved_recipes,
        "existing_grocery_items": existing_grocery_items,
        "user_preferences": user_preferences,
        "user_address": user_address,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Node 2 — Resolve nearby stores and rank by user preference fit
# ---------------------------------------------------------------------------

# Maps store names returned by Instacart → internal slugs used in price_comparison
_NAME_TO_SLUG: dict[str, str] = {
    "walmart": "walmart",
    "costco": "costco",
    "aldi": "aldi",
    "kroger": "kroger",
    "publix": "publix",
    "whole foods": "whole_foods",
    "whole foods market": "whole_foods",
    "trader joe's": "trader_joes",
    "trader joes": "trader_joes",
    "sprouts": "sprouts",
    "sprouts farmers market": "sprouts",
    "target": "target",
    "safeway": "safeway",
    "albertsons": "albertsons",
    "heb": "heb",
    "meijer": "meijer",
}

# price_rank: 1=cheapest, 5=most expensive
# quality_rank: 1=lowest, 5=highest (freshness, organic selection, variety)
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

_QUALITY_TAGS = {"organic", "non-gmo", "gluten-free", "vegan", "vegetarian", "keto", "paleo"}


def _score_store(slug: str, prefs: dict) -> tuple[float, str, str]:
    """
    Returns (score, insight_text, insight_type) for a store given user preferences.
    Higher score = better fit.
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
        # balanced: reward stores that aren't extreme in either direction
        score += (4 - abs(price_rank - 2.5)) + (4 - abs(quality_rank - 3.5))

    # Build insight text
    if is_bulk and wants_bulk:
        insight = f"Best bulk value — great for your household of {household_size}"
        insight_type = "bulk"
    elif price_rank <= 2 and is_price_sensitive:
        insight = "Lowest prices nearby — best fit for your budget"
        insight_type = "price"
    elif quality_rank >= 4 and is_quality_sensitive:
        insight = "Best produce quality and freshness — matches your dietary preferences"
        insight_type = "quality"
    elif price_rank <= 2:
        insight = "Cheapest option nearby"
        insight_type = "price"
    elif quality_rank >= 4:
        insight = "Better produce quality and longer shelf life"
        insight_type = "quality"
    else:
        insight = "Good balance of price and quality"
        insight_type = "balanced"

    return score, insight, insight_type


async def resolve_stores(state: SmartGroceryState) -> dict:
    """
    Fetch stores near the user's zip code, score them by preference fit,
    and set the recommended store as the active store_preference.
    The full ranked list is passed to human_checkpoint so the user can change it.
    """
    zip_code = state.get("user_address")
    if not zip_code:
        logger.info("resolve_stores: no zip code available, skipping store resolution")
        return {"nearby_stores": [], "error": None}

    raw_stores = await get_stores(zip_code)
    prefs = state.get("user_preferences", {})

    scored: list[tuple[float, dict]] = []
    for store in raw_stores:
        name = store.get("name", "").lower().strip()
        slug = _NAME_TO_SLUG.get(name, name.replace(" ", "_"))
        score, insight, insight_type = _score_store(slug, prefs)
        scored.append((score, {
            "store_id": store["store_id"],
            "name": store["name"],
            "slug": slug,
            "distance_miles": store.get("distance_miles"),
            "insight": insight,
            "insight_type": insight_type,
            "recommended": False,
            "supports_delivery": store.get("supports_delivery", True),
            "supports_pickup": store.get("supports_pickup", False),
        }))

    # Sort: highest score first, ties broken by distance
    scored.sort(key=lambda x: (-x[0], x[1].get("distance_miles") or 999))
    nearby_stores = [s for _, s in scored]

    if nearby_stores:
        nearby_stores[0]["recommended"] = True

    updates: dict = {"nearby_stores": nearby_stores, "error": None}

    # Only override store_preference if the user hasn't already picked one
    current_pref = state.get("store_preference", "instacart")
    if nearby_stores and current_pref in ("instacart", "", None):
        best = nearby_stores[0]
        updates["store_preference"] = best["slug"]
        updates["store_id"] = best["store_id"]
        logger.info(
            "resolve_stores: recommended %s (score=%.1f, insight_type=%s)",
            best["name"], scored[0][0], best["insight_type"],
        )

    return updates


# ---------------------------------------------------------------------------
# Node 3 — Claude analyzes pantry and identifies what to buy
# ---------------------------------------------------------------------------

def analyze_pantry(state: SmartGroceryState) -> dict:
    """
    Claude identifies missing items across three categories:
      1. Recipe gaps — ingredients needed for saved recipes not in pantry
      2. Expiring items — items expiring soon that should be used/replaced
      3. Missing staples — common pantry staples not currently stocked
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — using stub analysis")
        return {"missing_items": _stub_analysis(state), "error": None}

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    pantry_summary = json.dumps(state["pantry_items"], indent=2)
    recipes_summary = json.dumps(
        [{"title": r["title"], "ingredients": r["ingredients"]} for r in state["saved_recipes"]],
        indent=2
    )
    existing_summary = json.dumps(
        [i["name"] for i in state["existing_grocery_items"]]
    )
    prefs = state["user_preferences"]
    household_size = prefs.get("householdSize", 1)
    dietary_tags = prefs.get("dietaryTags", [])
    budget = state.get("budget") or prefs.get("weeklyBudget") or 0

    prompt = f"""You are a smart grocery assistant for a household of {household_size}.

PANTRY (current items):
{pantry_summary}

SAVED RECIPES (recipes this user wants to cook):
{recipes_summary}

ALREADY ON GROCERY LIST (don't duplicate):
{existing_summary}

DIETARY RESTRICTIONS: {", ".join(dietary_tags) or "none"}
WEEKLY BUDGET: ${budget or "not set"}
COMMON STAPLES TO CHECK: {", ".join(_STAPLES)}

Identify what this user should buy. Return a JSON array of items to purchase.
Each item must have:
  - name: string (simple grocery name, e.g. "eggs" not "Grade A Large Eggs")
  - quantity: number
  - unit: string (e.g. "dozen", "lbs", "oz", "count")
  - reason: one of "recipe_gap" | "expiring_soon" | "staple"
  - priority: 1 (expiring items), 2 (recipe gaps), 3 (staples)

Rules:
- Prioritize items expiring within 3 days (use them in recipes if possible, or replace)
- Only include recipe ingredients that are genuinely missing or insufficient in the pantry
- Only flag staples that are completely absent from the pantry
- Don't include items already on the grocery list
- Respect dietary restrictions
- Keep list practical — aim for 10-20 items max

Respond with ONLY a valid JSON array, no explanation."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        missing_items = json.loads(raw)
    except Exception as e:
        logger.error("analyze_pantry Claude call failed: %s", e)
        missing_items = _stub_analysis(state)

    return {"missing_items": missing_items, "error": None}


# ---------------------------------------------------------------------------
# Node 3 — Search store for each missing item
# ---------------------------------------------------------------------------

async def search_store(state: SmartGroceryState) -> dict:
    """
    Search the user's preferred store for each missing item.
    Returns product matches keyed by item name.
    """
    missing = state.get("missing_items", [])
    store = state.get("store_preference", "instacart")
    results: dict[str, list] = {}

    for item in missing:
        name = item["name"]
        try:
            products = await call_instacart_service(
                "/api/products/search",
                {"store_id": state.get("store_id", ""), "query": name, "limit": 5},
            )
            # Normalize: service returns {products: [...]} or stub {stub: True}
            if isinstance(products, dict) and products.get("stub"):
                products = _stub_store_search(name, store)
            elif isinstance(products, dict):
                products = products.get("products", _stub_store_search(name, store))
            results[name] = products
        except Exception as e:
            logger.warning("Store search failed for '%s': %s", name, e)
            results[name] = _stub_store_search(name, store)

    return {"store_search_results": results, "error": None}


# ---------------------------------------------------------------------------
# Node 4 — Compare prices across stores
# ---------------------------------------------------------------------------

async def compare_prices(state: SmartGroceryState) -> dict:
    """
    Estimate cart total per retailer available through Instacart to show
    savings comparison. Uses actual prices from search results as a baseline
    and applies per-retailer price index heuristics for the others.

    Instacart surfaces all retailers (Walmart, Costco, etc.) — price
    multipliers reflect typical relative pricing between chains.

    TODO: Replace heuristics with per-retailer search calls once we have
          store_ids for each retailer from getStores().
    """
    missing = state.get("missing_items", [])
    search_results = state.get("store_search_results", {})

    # Build baseline total from whatever search results we have
    baseline_total = 0.0
    item_count = 0
    for item in missing:
        name = item["name"]
        products = search_results.get(name, [])
        if products and products[0].get("price"):
            baseline_total += products[0]["price"] * max(item["quantity"], 1)
            item_count += 1

    if item_count == 0:
        baseline_total = len(missing) * 4.0  # rough fallback

    # Price index per retailer relative to a mid-tier baseline
    # Source: general market positioning — replace with real data when available
    store_multipliers = {
        "walmart": 0.88,        # consistently cheapest for groceries
        "costco": 0.82,         # cheapest per unit, but bulk quantities
        "aldi": 0.85,           # discount grocer
        "kroger": 1.0,          # mid-tier baseline
        "publix": 1.05,
        "whole_foods": 1.28,    # premium
    }

    price_comparison: dict[str, float] = {
        store: round(baseline_total * mult, 2)
        for store, mult in store_multipliers.items()
    }

    # Find best and worst for summary
    sorted_stores = sorted(price_comparison.items(), key=lambda x: x[1])
    best_store, best_price = sorted_stores[0]
    worst_store, worst_price = sorted_stores[-1]
    preferred = state.get("store_preference", "instacart")
    # For preferred store use baseline (actual search prices)
    preferred_price = price_comparison.get(preferred, baseline_total)

    savings_summary = {
        "best_store": best_store,
        "best_price": best_price,
        "worst_store": worst_store,
        "preferred_store": preferred,
        "preferred_price": preferred_price,
        "savings_vs_worst": round(worst_price - preferred_price, 2),
        "savings_vs_worst_pct": round((worst_price - preferred_price) / worst_price * 100, 1) if worst_price > 0 else 0,
        "all_stores": price_comparison,
    }

    return {
        "price_comparison": price_comparison,
        "savings_summary": savings_summary,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Node 5 — Claude builds the final cart
# ---------------------------------------------------------------------------

def build_cart(state: SmartGroceryState) -> dict:
    """
    Claude selects the best product match per item, applies budget awareness,
    and returns the final ordered cart with a total estimate.
    """
    if not settings.ANTHROPIC_API_KEY:
        return _stub_build_cart(state)

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    missing = state.get("missing_items", [])
    search_results = state.get("store_search_results", {})
    store = state.get("store_preference", "instacart")
    budget = state.get("budget") or state["user_preferences"].get("weeklyBudget") or 0

    items_with_options = []
    for item in missing:
        name = item["name"]
        options = search_results.get(name, [])
        items_with_options.append({
            "item": item,
            "store_options": options[:3],  # top 3 matches
        })

    prompt = f"""You are building a grocery cart for {store}.

Budget: ${budget or "flexible"}
Delivery preference: {state.get("delivery_preference", "delivery")}

Items to buy with store product options:
{json.dumps(items_with_options, indent=2)}

For each item, pick the best product option (best value, most relevant match).
Return a JSON array of cart items, each with:
  - name: string (the original item name)
  - quantity: number
  - unit: string
  - product_id: string (from store_options, or null if no match)
  - estimated_price: number (price * quantity, or null)
  - store: "{store}"
  - aisle: string (e.g. "Produce", "Dairy", "Meat & Seafood", "Pantry", "Frozen", "Bakery", "Beverages")

Also include a final object with key "__summary__" containing:
  - cart_total: number (sum of all estimated_price values)
  - item_count: number

Respond with ONLY a valid JSON array."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
    except Exception as e:
        logger.error("build_cart Claude call failed: %s", e)
        return _stub_build_cart(state)

    # Extract summary object
    summary = next((item for item in parsed if isinstance(item, dict) and "__summary__" in item), {})
    cart_items = [item for item in parsed if "__summary__" not in item]
    cart_total = summary.get("__summary__", {}).get("cart_total", 0) if summary else sum(
        (i.get("estimated_price") or 0) for i in cart_items
    )

    return {
        "cart_items": cart_items,
        "cart_total": round(float(cart_total), 2),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Node 6 — Human checkpoint (LangGraph interrupt)
# ---------------------------------------------------------------------------

def human_checkpoint(state: SmartGroceryState) -> dict:
    """
    Pause graph execution and surface the cart to the user for review.

    The frontend receives the current state (cart, price comparison, savings)
    and the user either confirms or cancels. The graph resumes when
    /agents/smart-grocery/confirm is called with confirmed=true/false.
    """
    confirmation = interrupt({
        "cart_items": state.get("cart_items", []),
        "cart_total": state.get("cart_total", 0),
        "price_comparison": state.get("price_comparison", {}),
        "savings_summary": state.get("savings_summary", {}),
        "store": state.get("store_preference"),
        "nearby_stores": state.get("nearby_stores", []),
        "delivery_preference": state.get("delivery_preference"),
        "missing_items_count": len(state.get("missing_items", [])),
    })
    # Frontend may send back a different store_preference if the user changed it
    updates: dict = {"order_confirmed": confirmation.get("confirmed", False)}
    if "store_preference" in confirmation:
        updates["store_preference"] = confirmation["store_preference"]
    if "store_id" in confirmation:
        updates["store_id"] = confirmation["store_id"]
    return updates


# ---------------------------------------------------------------------------
# Node 7 — Place order
# ---------------------------------------------------------------------------

async def place_order(state: SmartGroceryState) -> dict:
    """
    Place the order via the Instacart TypeScript service.
    Returns a checkout_url the frontend opens in an in-app WebView.
    """
    if not state.get("order_confirmed"):
        return {"order_result": {"status": "cancelled"}, "error": None}

    store = state.get("store_preference", "instacart")
    cart_items = state.get("cart_items", [])
    delivery_preference = state.get("delivery_preference", "delivery")
    cart_id = state.get("cart_id")

    if cart_id:
        checkout = await execute_checkout(
            cart_id=cart_id,
            max_budget=state.get("budget") or 0,
            fulfillment_type=delivery_preference,
            bypass_budget=False,
        )
        if checkout.get("success"):
            result_data = checkout.get("result", {})
            order_result = {
                "status": "pending_checkout",
                "method": result_data.get("method", "webview"),
                "checkout_url": result_data.get("checkout_url"),
                "store": store,
                "item_count": len(cart_items),
                "cart_total": state.get("cart_total", 0),
                "delivery_preference": delivery_preference,
            }
        else:
            # Budget exceeded or API error — surface to frontend
            order_result = {
                "status": checkout.get("reason", "error"),
                "estimated_total": checkout.get("estimated_total"),
                "max_budget": checkout.get("max_budget"),
                "store": store,
            }
    else:
        # No cart_id yet — return a WebView fallback URL
        order_result = {
            "status": "pending_checkout",
            "method": "webview",
            "checkout_url": "https://www.instacart.com/store",
            "store": store,
            "item_count": len(cart_items),
            "cart_total": state.get("cart_total", 0),
            "delivery_preference": delivery_preference,
        }

    return {"order_result": order_result, "error": None}


# ---------------------------------------------------------------------------
# Node 8 — Write grocery items back to DB
# ---------------------------------------------------------------------------

async def finalize(state: SmartGroceryState) -> dict:
    """
    After a confirmed order, write the cart items to the active grocery list
    and create an order record in the DB.
    """
    if not state.get("order_confirmed"):
        return {"grocery_list_id": None}

    uid = state["user_id"]
    cart_items = state.get("cart_items", [])
    pool = get_async_pool()

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Get or create active grocery list
                await cur.execute(
                    "SELECT id FROM grocery_lists WHERE user_id = %s AND status = 'active' ORDER BY created_at LIMIT 1",
                    (uid,)
                )
                row = await cur.fetchone()
                if row:
                    list_id = str(row["id"])
                else:
                    await cur.execute(
                        "INSERT INTO grocery_lists (user_id, name, status) VALUES (%s, 'My List', 'active') RETURNING id",
                        (uid,)
                    )
                    list_id = str((await cur.fetchone())["id"])

                # Upsert cart items into grocery list
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

                # Update grocery list estimated total
                await cur.execute(
                    "UPDATE grocery_lists SET estimated_total = %s WHERE id = %s",
                    (state.get("cart_total"), list_id)
                )
    except Exception as e:
        logger.error("finalize DB write failed: %s", e)
        return {"grocery_list_id": None, "error": str(e)}

    return {"grocery_list_id": list_id, "error": None}


# ---------------------------------------------------------------------------
# Stubs (used when API keys are not configured)
# ---------------------------------------------------------------------------

def _stub_analysis(state: SmartGroceryState) -> list[dict]:
    """Return a small realistic missing items list for dev without Claude."""
    pantry_names = {i["name"].lower() for i in state.get("pantry_items", [])}
    missing = []
    for staple in _STAPLES[:5]:
        if staple not in pantry_names:
            missing.append({"name": staple, "quantity": 1, "unit": "count", "reason": "staple", "priority": 3})

    # Add one expiring item if any
    expiring = [i for i in state.get("pantry_items", []) if i.get("freshnessStatus") in ("expiring", "use_soon")]
    if expiring:
        missing.insert(0, {
            "name": expiring[0]["name"],
            "quantity": 1,
            "unit": expiring[0].get("unit", "count"),
            "reason": "expiring_soon",
            "priority": 1,
        })

    return missing[:8]


def _stub_store_search(item_name: str, store: str) -> list[dict]:
    base_price = round(2.5 + len(item_name) % 4, 2)
    return [{
        "store": store,
        "product_id": f"stub-{store}-{item_name.lower().replace(' ', '-')}",
        "name": f"{item_name.title()}",
        "price": base_price,
        "unit": "1 ea",
        "image_url": None,
    }]


def _stub_build_cart(state: SmartGroceryState) -> dict:
    store = state.get("store_preference", "instacart")
    missing = state.get("missing_items", [])
    search_results = state.get("store_search_results", {})

    cart_items = []
    total = 0.0
    for item in missing:
        name = item["name"]
        products = search_results.get(name, [])
        product = products[0] if products else {}
        price = float(product.get("price") or 3.5)
        cart_items.append({
            "name": name,
            "quantity": item["quantity"],
            "unit": item["unit"],
            "product_id": product.get("product_id"),
            "estimated_price": round(price * max(item["quantity"], 1), 2),
            "store": store,
            "aisle": "Pantry",
        })
        total += price * max(item["quantity"], 1)

    return {"cart_items": cart_items, "cart_total": round(total, 2), "error": None}
