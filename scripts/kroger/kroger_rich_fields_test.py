"""
scripts/kroger/kroger_rich_fields_test.py
──────────────────────────────────────────
Verifies that the expanded KrogerProduct TypedDict fields are populated
correctly from a live Kroger API response.

Tests each new field extracted by runner.py:
  brand, sale_price, organic, snap_eligible, manufacturer_declarations,
  allergens, stock_level, fulfillment_*, temperature, rating, review_count,
  categories, country_origin, nutrition

Usage:
  cd gook-backend
  source venv/bin/activate
  python scripts/kroger/kroger_rich_fields_test.py

Set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in gook-backend/.env first.
"""

import asyncio
import json
import os
import sys

# Three levels up from scripts/kroger/ → project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from app.agents.kroger_client.runner import search_kroger, KrogerProduct

# ─── Config ───────────────────────────────────────────────────────────────────
ZIP_CODE  = "46032"   # Carmel, IN — change if you want a different store
TEST_ITEMS = ["eggs", "whole milk", "chicken breast"]   # small set; covers refrigerated/dry/meat


# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m~\033[0m"


def check(label: str, value, *, required: bool = True, validator=None) -> bool:
    """Print a pass/warn/fail line for a single field check."""
    if value is None or value == [] or value == {}:
        symbol = FAIL if required else WARN
        note   = "MISSING (required)" if required else "not returned by API"
        print(f"  {symbol} {label}: {note}")
        return not required   # warn = still passes; fail = required field missing
    if validator and not validator(value):
        print(f"  {FAIL} {label}: invalid value → {value!r}")
        return False
    print(f"  {PASS} {label}: {value!r}")
    return True


def validate_product_fields(item_name: str, products: list[KrogerProduct]) -> int:
    """
    Run field-level checks on the best product returned for one item.
    Returns number of failures.
    """
    if not products:
        print(f"  {FAIL} No products returned for '{item_name}'")
        return 1

    p = products[0]   # best match
    print(f"\n  Product: {p.get('name')} (product_id={p.get('product_id')})")

    failures = 0

    # ── Core fields (always required) ────────────────────────────────────────
    failures += 0 if check("product_id", p.get("product_id"),
                            validator=lambda v: isinstance(v, str) and len(v) > 0) else 1
    failures += 0 if check("name",       p.get("name"),
                            validator=lambda v: isinstance(v, str) and len(v) > 0) else 1
    failures += 0 if check("unit",       p.get("unit"),
                            validator=lambda v: isinstance(v, str) and len(v) > 0) else 1

    # ── Price (required — every real product should have one) ─────────────
    failures += 0 if check("price",      p.get("price"),
                            validator=lambda v: isinstance(v, (int, float)) and v > 0) else 1
    check("sale_price", p.get("sale_price"), required=False,
          validator=lambda v: isinstance(v, (int, float)) and v > 0)

    # ── Brand / image / aisle (usually present) ───────────────────────────
    check("brand",     p.get("brand"),     required=False)
    check("image_url", p.get("image_url"), required=False)
    check("aisle",     p.get("aisle"),     required=False)

    # ── Boolean flags (always present — default False is fine) ────────────
    failures += 0 if check("organic",      p.get("organic"),
                            required=True, validator=lambda v: isinstance(v, bool)) else 1
    failures += 0 if check("snap_eligible", p.get("snap_eligible"),
                            required=True, validator=lambda v: isinstance(v, bool)) else 1

    # ── Lists (present but may be empty) ─────────────────────────────────
    failures += 0 if check("manufacturer_declarations", p.get("manufacturer_declarations"),
                            required=True, validator=lambda v: isinstance(v, list)) else 1
    failures += 0 if check("allergens", p.get("allergens"),
                            required=True, validator=lambda v: isinstance(v, list)) else 1
    failures += 0 if check("categories", p.get("categories"),
                            required=True, validator=lambda v: isinstance(v, list)) else 1

    # ── Inventory / fulfillment ───────────────────────────────────────────
    check("stock_level", p.get("stock_level"), required=False,
          validator=lambda v: v in ("HIGH", "LOW", "TEMPORARILY_OUT_OF_STOCK"))
    failures += 0 if check("fulfillment_in_store", p.get("fulfillment_in_store"),
                            required=True, validator=lambda v: isinstance(v, bool)) else 1
    failures += 0 if check("fulfillment_pickup",   p.get("fulfillment_pickup"),
                            required=True, validator=lambda v: isinstance(v, bool)) else 1
    failures += 0 if check("fulfillment_delivery",  p.get("fulfillment_delivery"),
                            required=True, validator=lambda v: isinstance(v, bool)) else 1

    # ── Temperature (usually present for food items) ──────────────────────
    check("temperature", p.get("temperature"), required=False,
          validator=lambda v: isinstance(v, str) and len(v) > 0)

    # ── Ratings (optional — many items have no reviews) ───────────────────
    check("rating",       p.get("rating"),       required=False,
          validator=lambda v: isinstance(v, (int, float)) and 0 <= v <= 5)
    check("review_count", p.get("review_count"), required=False,
          validator=lambda v: isinstance(v, int) and v >= 0)

    # ── Country origin ────────────────────────────────────────────────────
    check("country_origin", p.get("country_origin"), required=False)

    # ── Nutrition ─────────────────────────────────────────────────────────
    nutrition = p.get("nutrition")
    if nutrition is None:
        print(f"  {WARN} nutrition: not returned by API (some products omit it)")
    else:
        print(f"  {PASS} nutrition: present")
        for nfield in ("calories", "protein_g", "fat_g", "carbs_g", "sodium_mg"):
            val = nutrition.get(nfield)
            symbol = PASS if val is not None else WARN
            print(f"    {symbol} nutrition.{nfield}: {val}")

    return failures


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Kroger Rich Fields Test")
    print(f"ZIP: {ZIP_CODE}  |  Items: {TEST_ITEMS}")
    print("=" * 60)

    result = await search_kroger(
        items=TEST_ITEMS,
        zip_code=ZIP_CODE,
        delivery_preference="pickup",
    )

    # ── API-level checks ──────────────────────────────────────────────────────
    print("\n── Store resolution ─────────────────────────────────────────────")
    store = result.get("store")
    if store:
        print(f"  {PASS} Store found: {store['name']} ({store['location_id']})")
        print(f"       Address: {store['address']}")
    else:
        print(f"  {FAIL} No store found near {ZIP_CODE}")

    if result.get("error"):
        print(f"\n  {FAIL} API error: {result['error']}")
        return

    print(f"\n── Product field validation ──────────────────────────────────────")
    total_failures = 0
    search_results = result.get("results", {})

    for item in TEST_ITEMS:
        products = search_results.get(item, [])
        print(f"\n▸ Item: '{item}'  ({len(products)} options returned)")
        failures = validate_product_fields(item, products)
        total_failures += failures

    # ── Unfound items ─────────────────────────────────────────────────────────
    unfound = result.get("unfound", [])
    print(f"\n── Summary ──────────────────────────────────────────────────────")
    print(f"  Items searched:    {len(TEST_ITEMS)}")
    print(f"  Items found:       {len(TEST_ITEMS) - len(unfound)}")
    print(f"  Items not found:   {len(unfound)}{' → ' + str(unfound) if unfound else ''}")
    print(f"  Estimated total:   ${result.get('estimated_total', 0):.2f}")
    print(f"  Field failures:    {total_failures}")

    print()
    if total_failures == 0:
        print("\033[92m✓ All required fields present and valid.\033[0m")
    else:
        print(f"\033[91m✗ {total_failures} required field(s) failed. See above.\033[0m")

    # ── Raw dump of first product (for debugging new API fields) ─────────────
    print("\n── Raw first product dump (for debugging) ───────────────────────")
    for item in TEST_ITEMS:
        products = search_results.get(item, [])
        if products:
            print(f"\n  [{item}]")
            print(json.dumps(products[0], indent=4, default=str))
            break


if __name__ == "__main__":
    asyncio.run(main())
