#!/usr/bin/env python3
"""
scripts/test_kroger_agent.py
─────────────────────────────
Manual smoke-test for the Kroger ADK agent.

Run from the project root with the venv active:

    python scripts/test_kroger_agent.py

What it does
────────────
Calls search_kroger() with a realistic mock shopping list and prints
the full result in a readable format. Works in two modes:

  STUB MODE  (default, no credentials needed)
    When KROGER_CLIENT_ID or GOOGLE_API_KEY are absent, the agent returns
    deterministic fake data so you can test the result shape and downstream
    parsing without live API calls.

  LIVE MODE  (requires .env credentials)
    When all three env vars are set (KROGER_CLIENT_ID, KROGER_CLIENT_SECRET,
    GOOGLE_API_KEY), the agent starts the kroger-mcp subprocess, calls the
    live Kroger API, and uses Gemini to build the result.

Usage examples
──────────────
  # Stub mode — always works
  python scripts/test_kroger_agent.py

  # Live mode — requires valid .env
  KROGER_CLIENT_ID=xxx KROGER_CLIENT_SECRET=yyy GOOGLE_API_KEY=zzz \
      python scripts/test_kroger_agent.py

  # Custom zip and items
  python scripts/test_kroger_agent.py --zip 30301 --items "bread,butter,apples"

  # Test pickup vs delivery
  python scripts/test_kroger_agent.py --delivery pickup
"""

import argparse
import asyncio
import json
import os
import sys
import time

# ── Make sure project root is on sys.path so local imports work ──────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Load .env before importing anything that reads settings
from dotenv import load_dotenv
load_dotenv()

from app.agents.kroger_client import search_kroger, KrogerResult


# ─────────────────────────────────────────────────────────────────────────────
# Default test data
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_ZIP = "46032"   # Carmel, IN — default test location

DEFAULT_ITEMS = [
    "eggs",
    "whole milk",
    "chicken breast",
    "pasta",
    "olive oil",
]

DEFAULT_QUANTITIES = {
    "eggs": 1,           # 1 dozen
    "whole milk": 1,     # 1 gallon
    "olive oil": 1,
    "garlic": 3,         # 3 bulbs
    "chicken breast": 2, # 2 lbs
    "spinach": 1,
    "greek yogurt": 2,   # 2 containers
    "cheddar cheese": 1,
    "pasta": 2,          # 2 boxes
    "canned tomatoes": 3,
}


# ─────────────────────────────────────────────────────────────────────────────
# Printing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _header(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def _detect_mode() -> str:
    has_kroger = bool(os.environ.get("KROGER_CLIENT_ID") and os.environ.get("KROGER_CLIENT_SECRET"))
    has_google = bool(os.environ.get("GOOGLE_API_KEY"))
    if has_kroger and has_google:
        return "LIVE"
    missing = []
    if not has_kroger:
        missing.append("KROGER_CLIENT_ID / KROGER_CLIENT_SECRET")
    if not has_google:
        missing.append("GOOGLE_API_KEY")
    return f"STUB (missing: {', '.join(missing)})"


def _print_result(result: KrogerResult, elapsed: float) -> None:
    """Pretty-print the KrogerResult."""

    # ── Store ─────────────────────────────────────────────────────────────────
    _header("Store Selected")
    store = result.get("store") or {}
    print(f"  Name:        {store.get('name', 'n/a')}")
    print(f"  Location ID: {store.get('location_id', 'n/a')}")
    print(f"  Address:     {store.get('address', 'n/a')}")

    # ── Cart preview (best match per item) ───────────────────────────────────
    _header("Cart Preview  (best pick per item)")
    cart = result.get("cart_preview", [])
    if cart:
        fmt = "  {:<22} {:>6}  {:<30}  {:>8}"
        print(fmt.format("Item", "Qty", "Product", "Price"))
        print("  " + "─" * 72)
        for item in cart:
            price_str = f"${item['estimated_price']:.2f}" if item.get("estimated_price") else "N/A"
            prod = (item.get("product_name") or "")[:30]
            print(fmt.format(
                item.get("name", "")[:22],
                item.get("quantity", 1),
                prod,
                price_str,
            ))
    else:
        print("  (empty)")

    # ── Unfound items ─────────────────────────────────────────────────────────
    unfound = result.get("unfound", [])
    if unfound:
        _header(f"Items Not Found at Kroger  ({len(unfound)})")
        for item in unfound:
            print(f"  ✗  {item}")

    # ── Totals ────────────────────────────────────────────────────────────────
    _header("Estimated Total")
    print(f"  ${result.get('estimated_total', 0):.2f}  ({len(cart)} items found"
          f"{f', {len(unfound)} not found' if unfound else ''})")

    # ── Full results (item → product list) ───────────────────────────────────
    _header("All Product Matches  (raw results)")
    results = result.get("results", {})
    for item_name, products in results.items():
        print(f"\n  [{item_name}]")
        if not products:
            print("    (no results)")
            continue
        for p in products[:3]:  # show top 3 per item
            price_str = f"${p['price']:.2f}" if p.get("price") else "N/A"
            print(f"    • {p.get('name', '')[:45]:<45}  {price_str:>6}  {p.get('unit', '')}")

    # ── Error / timing ────────────────────────────────────────────────────────
    if result.get("error"):
        _header("⚠  Error")
        print(f"  {result['error']}")

    _header(f"Completed in {elapsed:.1f}s")


# ─────────────────────────────────────────────────────────────────────────────
# Assertions (basic shape validation)
# ─────────────────────────────────────────────────────────────────────────────

def _assert_result(result: KrogerResult) -> None:
    """
    Raise AssertionError if the result is missing required fields.
    Works for both stub and live results.
    """
    assert isinstance(result, dict), "Result must be a dict"
    assert "store" in result, "Missing 'store' key"
    assert "results" in result, "Missing 'results' key"
    assert "unfound" in result, "Missing 'unfound' key"
    assert "cart_preview" in result, "Missing 'cart_preview' key"
    assert "estimated_total" in result, "Missing 'estimated_total' key"
    assert isinstance(result["results"], dict), "'results' must be a dict"
    assert isinstance(result["cart_preview"], list), "'cart_preview' must be a list"
    assert isinstance(result["unfound"], list), "'unfound' must be a list"
    assert isinstance(result["estimated_total"], (int, float)), "'estimated_total' must be numeric"

    # Every item in cart_preview must have required fields
    for item in result["cart_preview"]:
        assert "name" in item, f"cart_preview item missing 'name': {item}"
        assert "quantity" in item, f"cart_preview item missing 'quantity': {item}"
        assert "estimated_price" in item, f"cart_preview item missing 'estimated_price': {item}"

    print("\n  ✓  All shape assertions passed")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def main(zip_code: str, items: list[str], quantities: dict, delivery: str) -> None:
    mode = _detect_mode()

    print(f"\n{'═' * 60}")
    print(f"  Kroger Agent — Smoke Test")
    print(f"{'═' * 60}")
    print(f"  Mode:      {mode}")
    print(f"  Zip code:  {zip_code}")
    print(f"  Items:     {', '.join(items)}")
    print(f"  Delivery:  {delivery}")
    print(f"{'═' * 60}")

    print("\n  Running agent...")
    start = time.time()

    result = await search_kroger(
        items=items,
        zip_code=zip_code,
        quantities=quantities,
        delivery_preference=delivery,
    )

    elapsed = time.time() - start

    _print_result(result, elapsed)
    _assert_result(result)

    print("\n  ✓  Smoke test passed\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kroger agent smoke test")
    parser.add_argument("--zip", default=DEFAULT_ZIP, help="Zip code for store lookup")
    parser.add_argument(
        "--items",
        default=",".join(DEFAULT_ITEMS),
        help="Comma-separated list of grocery items",
    )
    parser.add_argument(
        "--delivery",
        choices=["pickup", "delivery"],
        default="pickup",
        help="Fulfillment preference",
    )
    args = parser.parse_args()

    items = [i.strip() for i in args.items.split(",") if i.strip()]
    quantities = {item: DEFAULT_QUANTITIES.get(item, 1) for item in items}

    asyncio.run(main(args.zip, items, quantities, args.delivery))
