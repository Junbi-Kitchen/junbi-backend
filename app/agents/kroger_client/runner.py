"""
kroger_agent/runner.py
──────────────────────
Public entry point for Kroger product search.

Calls the Kroger REST API directly via the `kroger-api` Python library —
no LLM, no MCP subprocess, no token limits. The agent pattern was replaced
because ADK accumulates all tool-call results in the conversation history
and re-sends the full context on every LLM call, blowing past the 50k
input-tokens-per-minute Tier 1 rate limit after just 5 product searches.

Direct API flow (this file):
  1. Authenticate with Client Credentials (product.compact scope)
  2. Search locations near the zip code → pick the closest store
  3. For each item, search products at that store → pick the best match
  4. Return KrogerResult with store info, all results, and a cart preview

Selection logic (no LLM needed):
  - Store: first result from Kroger's location API (already sorted by distance)
  - Product: prefer Kroger-brand items, otherwise lowest price
  - If no price available: take the first result

Callers
───────
  - app/agents/smart_grocery_agent/tools.py  (search_kroger_products tool)
  - scripts/kroger/kroger_agent_smoke_test.py
"""

import asyncio
import logging
import os
from typing import TypedDict

from config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result types  (same shape as before — callers are unaffected)
# ─────────────────────────────────────────────────────────────────────────────

class KrogerStoreInfo(TypedDict):
    """Identifies the Kroger store selected for this search run."""
    location_id: str
    name: str
    address: str


class KrogerNutrition(TypedDict, total=False):
    """Parsed nutrition facts per serving."""
    serving_size: str | None          # e.g. "2 large eggs (100g)"
    calories: float | None
    protein_g: float | None
    fat_g: float | None
    carbs_g: float | None
    fiber_g: float | None
    sugar_g: float | None
    sodium_mg: float | None


class KrogerProduct(TypedDict):
    """One product match returned by the Kroger product search API."""
    product_id: str
    name: str
    brand: str | None
    price: float | None
    sale_price: float | None          # promo price if on sale
    unit: str
    image_url: str | None
    aisle: str | None

    # Dietary / health attributes
    organic: bool                     # organicClaimName == "YES"
    snap_eligible: bool
    manufacturer_declarations: list[str]  # ["Dairy Free", "Gluten Free", ...]
    allergens: list[str]              # ["Contains Eggs", "Free From Gluten", ...]

    # Inventory / fulfillment
    stock_level: str | None           # "HIGH" | "LOW" | "TEMPORARILY_OUT_OF_STOCK"
    fulfillment_pickup: bool
    fulfillment_delivery: bool
    fulfillment_in_store: bool

    # Temperature / storage
    temperature: str | None           # "Refrigerated" | "Frozen" | "Dry"

    # Ratings
    rating: float | None              # averageOverallRating (0–5)
    review_count: int | None

    # Metadata
    categories: list[str]
    country_origin: str | None

    # Nutrition (may be None if not returned by API)
    nutrition: KrogerNutrition | None


class KrogerCartItem(TypedDict):
    """Best product pick for one shopping-list item."""
    name: str
    product_id: str | None
    product_name: str
    price: float | None
    quantity: float
    unit: str
    estimated_price: float | None
    aisle: str | None


class KrogerResult(TypedDict):
    """Full result returned by search_kroger()."""
    store: KrogerStoreInfo | None
    results: dict[str, list[KrogerProduct]]
    unfound: list[str]
    cart_preview: list[KrogerCartItem]
    estimated_total: float
    error: str | None


# ─────────────────────────────────────────────────────────────────────────────
# Stub fallback
# ─────────────────────────────────────────────────────────────────────────────

def _stub_result(items: list[str], zip_code: str) -> KrogerResult:
    """Deterministic fake data when credentials are missing or the API fails."""
    cart_preview: list[KrogerCartItem] = []
    results: dict[str, list[KrogerProduct]] = {}
    total = 0.0

    for item in items:
        base_price = round(2.5 + len(item) % 4, 2)
        product: KrogerProduct = {
            "product_id": f"stub-kroger-{item.lower().replace(' ', '-')}",
            "name": f"{item.title()} (Kroger Brand)",
            "brand": "Kroger",
            "price": base_price,
            "sale_price": None,
            "unit": "1 ea",
            "image_url": None,
            "aisle": "Pantry",
            "organic": False,
            "snap_eligible": False,
            "manufacturer_declarations": [],
            "allergens": [],
            "stock_level": "HIGH",
            "fulfillment_pickup": True,
            "fulfillment_delivery": False,
            "fulfillment_in_store": True,
            "temperature": "Dry",
            "rating": None,
            "review_count": None,
            "categories": [],
            "country_origin": None,
            "nutrition": None,
        }
        results[item] = [product]
        cart_preview.append({
            "name": item,
            "product_id": product["product_id"],
            "product_name": product["name"],
            "price": base_price,
            "quantity": 1,
            "unit": "1 ea",
            "estimated_price": base_price,
            "aisle": "Pantry",
        })
        total += base_price

    return {
        "store": {"location_id": "stub-store", "name": "Kroger (stub)", "address": zip_code},
        "results": results,
        "unfound": [],
        "cart_preview": cart_preview,
        "estimated_total": round(total, 2),
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_price(product: dict) -> float | None:
    """Pull the regular shelf price out of a Kroger product API response."""
    for entry in product.get("items", []):
        regular = entry.get("price", {}).get("regular")
        if regular is not None:
            return float(regular)
    return None


def _extract_sale_price(product: dict) -> float | None:
    """Pull the promotional/sale price if one exists."""
    for entry in product.get("items", []):
        promo = entry.get("price", {}).get("promo")
        if promo is not None:
            return float(promo)
    return None


def _extract_unit(product: dict) -> str:
    """Pull the size/unit string from a Kroger product item."""
    for entry in product.get("items", []):
        size = entry.get("size", "")
        if size:
            return size
    return "1 ea"


def _extract_image(product: dict) -> str | None:
    """Pull the front-of-package image URL (medium size preferred)."""
    for img in product.get("images", []):
        if img.get("perspective") == "front":
            sizes = {s.get("size"): s.get("url") for s in img.get("sizes", [])}
            return sizes.get("medium") or sizes.get("large") or next(iter(sizes.values()), None)
    return None


def _extract_aisle(product: dict) -> str | None:
    """Pull the aisle description from aisleLocations."""
    for loc in product.get("aisleLocations", []):
        desc = loc.get("description", "")
        if desc:
            return desc
    return None


def _extract_stock_level(product: dict) -> str | None:
    """Pull inventory stock level from the first item entry."""
    for entry in product.get("items", []):
        level = entry.get("inventory", {}).get("stockLevel")
        if level:
            return level
    return None


def _extract_fulfillment(product: dict) -> tuple[bool, bool, bool]:
    """Return (pickup, delivery, in_store) booleans from fulfillment flags."""
    for entry in product.get("items", []):
        f = entry.get("fulfillment", {})
        return (
            bool(f.get("curbside") or f.get("pickup")),
            bool(f.get("delivery")),
            bool(f.get("inStore")),
        )
    return False, False, True  # default: assume in-store


def _extract_allergens(product: dict) -> list[str]:
    """
    Build a plain-English allergen list from the allergens array.
    e.g. [{"levelOfContainmentName": "Contains", "name": "Eggs"}]
    → ["Contains Eggs"]
    """
    allergens = []
    for a in product.get("allergens", []):
        level = a.get("levelOfContainmentName", "")
        name = a.get("name", "")
        if name:
            allergens.append(f"{level} {name}".strip())
    return allergens


def _extract_nutrition(product: dict) -> "KrogerNutrition | None":
    """
    Parse nutritionInformation into a compact dict.

    The Kroger API can return nutritionInformation in two shapes:
      1. A dict:  {"labelNutrients": [...], "servingSizeDescription": "2 eggs"}
      2. A list:  [{"name": "Calories", "value": "140", ...}, ...]

    We handle both and fall back gracefully if neither yields data.
    """
    raw = product.get("nutritionInformation")
    if not raw:
        return None

    serving: str | None = None
    nutrients_list: list = []

    if isinstance(raw, dict):
        serving = raw.get("servingSizeDescription") or raw.get("servingSize")
        nutrients_list = raw.get("labelNutrients") or []
    elif isinstance(raw, list):
        # The list IS the nutrients array
        nutrients_list = raw
    else:
        return None

    # Build lookup {lower_name: numeric_value}
    nutrient_map: dict[str, float] = {}
    for n in nutrients_list:
        if not isinstance(n, dict):
            continue
        raw_name = (n.get("name") or "").lower().strip()
        try:
            val = float(n.get("value") or 0)
        except (ValueError, TypeError):
            val = 0.0
        nutrient_map[raw_name] = val

    if not nutrient_map and not serving:
        return None

    return {
        "serving_size": serving,
        "calories": nutrient_map.get("calories"),
        "protein_g": nutrient_map.get("protein"),
        "fat_g": nutrient_map.get("total fat") or nutrient_map.get("fat"),
        "carbs_g": nutrient_map.get("total carbohydrate") or nutrient_map.get("carbohydrates"),
        "fiber_g": nutrient_map.get("dietary fiber") or nutrient_map.get("fiber"),
        "sugar_g": nutrient_map.get("total sugars") or nutrient_map.get("sugars"),
        "sodium_mg": nutrient_map.get("sodium"),
    }


def _pick_best(products: list[KrogerProduct]) -> KrogerProduct | None:
    """
    Pick the best product from a list of matches.
    Preference order:
      1. Kroger-brand items (cheapest among them)
      2. Any item with a price (cheapest)
      3. First item (no price data available)
    """
    if not products:
        return None

    kroger_brand = [p for p in products if "kroger" in p["name"].lower() or "simple truth" in p["name"].lower()]
    with_price = [p for p in products if p["price"] is not None]

    candidates = kroger_brand or with_price or products
    return min(candidates, key=lambda p: p["price"] if p["price"] is not None else 999)


# ─────────────────────────────────────────────────────────────────────────────
# Core sync implementation (runs in a thread — kroger-api is sync/requests)
# ─────────────────────────────────────────────────────────────────────────────

def _run_kroger_sync(
    items: list[str],
    zip_code: str,
    quantities: dict[str, float],
    delivery_preference: str,
) -> KrogerResult:
    """
    Direct Kroger API calls — no LLM, no MCP, no token limits.

    Uses Client Credentials auth (product.compact scope) which only requires
    KROGER_CLIENT_ID + KROGER_CLIENT_SECRET — no user OAuth needed.
    """
    from kroger_api import KrogerAPI

    api = KrogerAPI(
        client_id=settings.KROGER_CLIENT_ID,
        client_secret=settings.KROGER_CLIENT_SECRET,
    )

    # Authenticate with Client Credentials (read-only: locations + products)
    api.authorization.get_token_with_client_credentials("product.compact")

    # ── 1. Find nearest store ──────────────────────────────────────────────
    loc_resp = api.location.search_locations(zip_code=zip_code, limit=1)
    locations = loc_resp.get("data", [])

    if not locations:
        return {**_stub_result(items, zip_code), "error": f"No Kroger store found near {zip_code}"}

    store_raw = locations[0]
    location_id = store_raw.get("locationId", "")
    store_name = store_raw.get("name", "Kroger")
    addr = store_raw.get("address", {})
    address_str = f"{addr.get('addressLine1', '')}, {addr.get('city', '')}, {addr.get('state', '')} {addr.get('zipCode', '')}".strip(", ")

    store: KrogerStoreInfo = {
        "location_id": location_id,
        "name": store_name,
        "address": address_str,
    }

    # ── 2. Search products for each item ───────────────────────────────────
    results: dict[str, list[KrogerProduct]] = {}
    unfound: list[str] = []
    cart_preview: list[KrogerCartItem] = []
    total = 0.0

    fulfillment = "ais" if delivery_preference == "pickup" else "dug"

    for item in items:
        try:
            prod_resp = api.product.search_products(
                term=item,
                location_id=location_id,
                fulfillment=fulfillment,
                limit=3,
            )
            raw_products = prod_resp.get("data", [])
        except Exception as e:
            logger.warning("kroger product search failed for '%s': %s", item, e)
            raw_products = []

        products: list[KrogerProduct] = []
        for p in raw_products:
            pickup, delivery, in_store = _extract_fulfillment(p)
            products.append({
                "product_id": p.get("productId", ""),
                "name": p.get("description", item),
                "brand": p.get("brand"),
                "price": _extract_price(p),
                "sale_price": _extract_sale_price(p),
                "unit": _extract_unit(p),
                "image_url": _extract_image(p),
                "aisle": _extract_aisle(p),
                "organic": (p.get("organicClaimName") or "").upper() == "YES",
                "snap_eligible": bool(p.get("snapEligible")),
                "manufacturer_declarations": p.get("manufacturerDeclarations") or [],
                "allergens": _extract_allergens(p),
                "stock_level": _extract_stock_level(p),
                "fulfillment_pickup": pickup,
                "fulfillment_delivery": delivery,
                "fulfillment_in_store": in_store,
                "temperature": (p.get("temperature") or {}).get("indicator"),
                "rating": (p.get("ratingsAndReviews") or {}).get("averageOverallRating"),
                "review_count": (p.get("ratingsAndReviews") or {}).get("totalReviewCount"),
                "categories": p.get("categories") or [],
                "country_origin": p.get("countryOrigin"),
                "nutrition": _extract_nutrition(p),
            })

        results[item] = products

        if not products:
            unfound.append(item)
            continue

        best = _pick_best(products)
        if not best:
            unfound.append(item)
            continue

        qty = quantities.get(item, 1)
        est = round(best["price"] * qty, 2) if best["price"] is not None else None
        if est:
            total += est

        cart_preview.append({
            "name": item,
            "product_id": best["product_id"],
            "product_name": best["name"],
            "price": best["price"],
            "quantity": qty,
            "unit": best["unit"],
            "estimated_price": est,
            "aisle": best["aisle"],
        })

    return {
        "store": store,
        "results": results,
        "unfound": unfound,
        "cart_preview": cart_preview,
        "estimated_total": round(total, 2),
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public async entry point
# ─────────────────────────────────────────────────────────────────────────────

async def search_kroger(
    items: list[str],
    zip_code: str,
    quantities: dict[str, float] | None = None,
    delivery_preference: str = "pickup",
) -> KrogerResult:
    """
    Find Kroger products for a list of grocery items near a zip code.

    Calls the Kroger REST API directly (no LLM). Runs the sync kroger-api
    client in a thread pool so it doesn't block the FastAPI event loop.

    Args:
        items: Grocery item names, e.g. ["eggs", "whole milk"].
        zip_code: User's zip code for store lookup.
        quantities: Optional item → quantity mapping (defaults to 1 each).
        delivery_preference: "pickup" or "delivery".

    Returns:
        KrogerResult with store, product matches, and cart preview.
        Falls back to stub data with error field set on any failure.
    """
    if not settings.KROGER_CLIENT_ID or not settings.KROGER_CLIENT_SECRET:
        logger.warning("Kroger credentials not set — returning stub result")
        return _stub_result(items, zip_code)

    if not items:
        return {
            "store": None,
            "results": {},
            "unfound": [],
            "cart_preview": [],
            "estimated_total": 0.0,
            "error": None,
        }

    qty = quantities or {}

    try:
        # kroger-api uses requests (sync) — run in thread to avoid blocking
        return await asyncio.to_thread(
            _run_kroger_sync, items, zip_code, qty, delivery_preference
        )
    except Exception as e:
        logger.error("kroger_client: run failed — %s", e)
        return {**_stub_result(items, zip_code), "error": str(e)}
