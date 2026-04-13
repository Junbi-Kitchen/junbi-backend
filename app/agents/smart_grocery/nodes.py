"""
LangGraph nodes for the Smart Grocery agent.

Node execution order:
  load_context → analyze_pantry → search_store → compare_prices
      → build_cart → [INTERRUPT: user confirms] → place_order → finalize
"""

import json
import logging

import anthropic
from langgraph.types import interrupt

from app.db import get_pool
from config import settings

from .state import SmartGroceryState
from .tools.instacart import call_instacart_service, execute_checkout

logger = logging.getLogger(__name__)

_STAPLES = [
    "olive oil", "garlic", "eggs", "butter", "salt", "black pepper",
    "onion", "milk", "flour", "sugar", "chicken broth", "canned tomatoes",
]


# ---------------------------------------------------------------------------
# Node 1 — Load context from DB
# ---------------------------------------------------------------------------

def load_context(state: SmartGroceryState) -> dict:
    """Pull pantry, saved recipes, grocery list, and preferences from DB."""
    uid = state["user_id"]
    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()

        # Pantry with freshness
        cur.execute("""
            SELECT p.id, p.name, p.quantity, p.unit, p.expiry_date,
                   p.freshness_status, p.location,
                   COALESCE(ic.slug, 'pantry') AS category
            FROM pantry_items_with_freshness p
            LEFT JOIN ingredients i ON i.id = p.ingredient_id
            LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
            WHERE p.user_id = %s AND p.is_active = true
            ORDER BY p.expiry_date ASC NULLS LAST
        """, (uid,))
        pantry_items = [
            {
                "id": str(r["id"]),
                "name": r["name"] or "",
                "quantity": float(r["quantity"] or 0),
                "unit": r["unit"] or "",
                "expiryDate": r["expiry_date"].isoformat() if r["expiry_date"] else None,
                "freshnessStatus": r["freshness_status"],
                "category": r["category"],
            }
            for r in cur.fetchall()
        ]

        # Saved recipes with their ingredients
        cur.execute("""
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
        saved_recipes = [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "cookTimeMinutes": r["cook_time_mins"],
                "ingredients": r["ingredients"] or [],
            }
            for r in cur.fetchall()
        ]

        # Active grocery list items
        cur.execute("""
            SELECT gi.name, gi.quantity, gi.unit, gi.is_checked
            FROM grocery_items gi
            JOIN grocery_lists gl ON gl.id = gi.list_id
            WHERE gl.user_id = %s AND gl.status = 'active'
        """, (uid,))
        existing_grocery_items = [
            {"name": r["name"], "quantity": float(r["quantity"] or 0),
             "unit": r["unit"] or "", "checked": r["is_checked"]}
            for r in cur.fetchall()
        ]

        # User preferences
        cur.execute("""
            SELECT dietary_tags, allergies, cuisines, household_size, weekly_budget
            FROM user_preferences WHERE user_id = %s
        """, (uid,))
        pref_row = cur.fetchone()
        user_preferences = {
            "dietaryTags": list(pref_row["dietary_tags"] or []) if pref_row else [],
            "allergies": list(pref_row["allergies"] or []) if pref_row else [],
            "cuisines": list(pref_row["cuisines"] or []) if pref_row else [],
            "householdSize": pref_row["household_size"] if pref_row else 1,
            "weeklyBudget": float(pref_row["weekly_budget"] or 0) if pref_row else 0,
        }

        conn.commit()
    finally:
        pool.putconn(conn)

    return {
        "pantry_items": pantry_items,
        "saved_recipes": saved_recipes,
        "existing_grocery_items": existing_grocery_items,
        "user_preferences": user_preferences,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Node 2 — Claude analyzes pantry and identifies what to buy
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
        "delivery_preference": state.get("delivery_preference"),
        "missing_items_count": len(state.get("missing_items", [])),
    })
    return {"order_confirmed": confirmation.get("confirmed", False)}


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

def finalize(state: SmartGroceryState) -> dict:
    """
    After a confirmed order, write the cart items to the active grocery list
    and create an order record in the DB.
    """
    if not state.get("order_confirmed"):
        return {"grocery_list_id": None}

    uid = state["user_id"]
    cart_items = state.get("cart_items", [])
    order_result = state.get("order_result", {})

    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()

        # Get or create active grocery list
        cur.execute(
            "SELECT id FROM grocery_lists WHERE user_id = %s AND status = 'active' ORDER BY created_at LIMIT 1",
            (uid,)
        )
        row = cur.fetchone()
        if row:
            list_id = str(row["id"])
        else:
            cur.execute(
                "INSERT INTO grocery_lists (user_id, name, status) VALUES (%s, 'My List', 'active') RETURNING id",
                (uid,)
            )
            list_id = str(cur.fetchone()["id"])

        # Upsert cart items into grocery list
        for item in cart_items:
            name = item.get("name", "")
            if not name:
                continue

            # Resolve ingredient
            cur.execute(
                "INSERT INTO ingredients (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (name,)
            )
            cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
            ingredient_id = str(cur.fetchone()["id"])

            cur.execute("""
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
        cur.execute(
            "UPDATE grocery_lists SET estimated_total = %s WHERE id = %s",
            (state.get("cart_total"), list_id)
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("finalize DB write failed: %s", e)
        pool.putconn(conn)
        return {"grocery_list_id": None, "error": str(e)}
    finally:
        pool.putconn(conn)

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
