#!/usr/bin/env python3
"""
scripts/test_kroger_locations.py
──────────────────────────────────
Live multi-location test for the Kroger ADK agent.

Tests the same shopping list across several US zip codes to verify:
  - Store lookup works in each region
  - Products are found (Kroger coverage varies by market)
  - Prices differ by location as expected
  - Agent handles markets where Kroger has no presence gracefully

Requires real credentials in .env:
  KROGER_CLIENT_ID, KROGER_CLIENT_SECRET, GOOGLE_API_KEY

Run:
    source venv/bin/activate
    python scripts/test_kroger_locations.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

from app.agents.kroger_client import search_kroger

# ─────────────────────────────────────────────────────────────────────────────
# Test locations
# ─────────────────────────────────────────────────────────────────────────────

LOCATIONS = [
    {"label": "Carmel, IN (Kroger country)",     "zip": "46032"},
    {"label": "Indianapolis, IN",                 "zip": "46201"},
    {"label": "Cincinnati, OH (Kroger HQ city)",  "zip": "45202"},
    {"label": "Atlanta, GA (Kroger + Kroger)",    "zip": "30301"},
    {"label": "Dallas, TX",                       "zip": "75201"},
    {"label": "Los Angeles, CA (Ralphs/Kroger)",  "zip": "90001"},
    {"label": "Seattle, WA (Fred Meyer/Kroger)",  "zip": "98101"},
    {"label": "New York, NY (limited Kroger)",    "zip": "10001"},
]

# A short but representative grocery list — common items Kroger almost always stocks
TEST_ITEMS = [
    "eggs",
    "whole milk",
    "chicken breast",
    "pasta",
    "olive oil",
]

QUANTITIES = {
    "eggs": 1,
    "whole milk": 1,
    "chicken breast": 2,
    "pasta": 2,
    "olive oil": 1,
}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bar(found: int, total: int, width: int = 20) -> str:
    filled = round(found / total * width) if total else 0
    return f"[{'█' * filled}{'░' * (width - filled)}] {found}/{total}"


def _print_location_result(label: str, zip_code: str, result: dict, elapsed: float) -> None:
    store = result.get("store") or {}
    cart = result.get("cart_preview", [])
    unfound = result.get("unfound", [])
    total = result.get("estimated_total", 0)
    is_stub = "stub" in store.get("name", "").lower()

    mode = "STUB" if is_stub else "LIVE"
    status = "⚠  error" if result.get("error") else ("✓" if not is_stub else "~")

    print(f"\n  {status}  [{zip_code}] {label}  ({mode}, {elapsed:.1f}s)")
    print(f"       Store:   {store.get('name', 'n/a')}  —  {store.get('address', '')}")
    print(f"       Items:   {_bar(len(cart), len(TEST_ITEMS))}  "
          f"({'unfound: ' + ', '.join(unfound) if unfound else 'all found'})")
    print(f"       Total:   ${total:.2f}")

    if result.get("error"):
        print(f"       Error:   {result['error']}")

    # Per-item prices
    if cart and not is_stub:
        print("       Prices:  ", end="")
        prices = [f"{i['name']} ${i['estimated_price']:.2f}" for i in cart if i.get("estimated_price")]
        print("  |  ".join(prices))


def _print_summary(results: list[dict]) -> None:
    live = [r for r in results if "stub" not in (r["result"].get("store") or {}).get("name", "").lower()]
    stub = [r for r in results if r not in live]
    errors = [r for r in results if r["result"].get("error")]

    print(f"\n{'═' * 60}")
    print(f"  Summary")
    print(f"{'═' * 60}")
    print(f"  Locations tested:  {len(results)}")
    print(f"  Live API results:  {len(live)}")
    print(f"  Stub fallbacks:    {len(stub)}  (credentials missing or no store found)")
    print(f"  Errors:            {len(errors)}")

    if live:
        # Price range across live results
        totals = [r["result"]["estimated_total"] for r in live if r["result"]["estimated_total"] > 0]
        if totals:
            print(f"\n  Price range for same cart across locations:")
            print(f"    Low:  ${min(totals):.2f}")
            print(f"    High: ${max(totals):.2f}")
            print(f"    Diff: ${max(totals) - min(totals):.2f}  ({(max(totals) - min(totals)) / min(totals) * 100:.0f}% variance)")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run_location(location: dict) -> dict:
    start = time.time()
    result = await search_kroger(
        items=TEST_ITEMS,
        zip_code=location["zip"],
        quantities=QUANTITIES,
        delivery_preference="pickup",
    )
    return {
        "location": location,
        "result": result,
        "elapsed": time.time() - start,
    }


async def main() -> None:
    has_creds = (
        bool(os.environ.get("KROGER_CLIENT_ID"))
        and bool(os.environ.get("KROGER_CLIENT_SECRET"))
        and bool(os.environ.get("GOOGLE_API_KEY"))
    )

    print(f"\n{'═' * 60}")
    print(f"  Kroger Agent — Multi-Location Test")
    print(f"{'═' * 60}")
    print(f"  Mode:     {'LIVE (credentials found)' if has_creds else 'STUB (add credentials to .env)'}")
    print(f"  Items:    {', '.join(TEST_ITEMS)}")
    print(f"  Locations: {len(LOCATIONS)}")
    print(f"{'═' * 60}")

    if not has_creds:
        print("\n  ⚠  No credentials — all results will be stub data.")
        print("     Add to .env: KROGER_CLIENT_ID, KROGER_CLIENT_SECRET, GOOGLE_API_KEY\n")

    # Run sequentially to avoid hammering the API rate limits
    all_results = []
    for location in LOCATIONS:
        print(f"\n  Searching {location['zip']} ({location['label']})...", end="", flush=True)
        run = await run_location(location)
        all_results.append(run)
        _print_location_result(
            run["location"]["label"],
            run["location"]["zip"],
            run["result"],
            run["elapsed"],
        )

    _print_summary(all_results)


if __name__ == "__main__":
    asyncio.run(main())
