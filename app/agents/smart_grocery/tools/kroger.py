"""
Kroger API client.

Kroger is the only major grocery chain with a public buyer-facing API:
  https://developer.kroger.com

OAuth flows:
  - Product search  → Client Credentials (no user login needed)
  - Cart / Checkout → Authorization Code (user must link their Kroger account)

TODO: Register at developer.kroger.com and add to .env:
  KROGER_CLIENT_ID=...
  KROGER_CLIENT_SECRET=...

User OAuth tokens should be stored in connected_accounts table
(provider='kroger', access_token=..., refresh_token=..., expires_at=...).
"""

import httpx
from config import settings

_KROGER_BASE = "https://api.kroger.com/v1"
_TOKEN_URL = "https://api.kroger.com/v1/connect/oauth2/token"


async def _get_app_token() -> str:
    """Client credentials token — for product search only (no user context)."""
    if not settings.KROGER_CLIENT_ID or not settings.KROGER_CLIENT_SECRET:
        raise RuntimeError("Kroger API keys not configured (KROGER_CLIENT_ID / KROGER_CLIENT_SECRET)")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": "product.compact",
            },
            auth=(settings.KROGER_CLIENT_ID, settings.KROGER_CLIENT_SECRET),
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def search_products(item_name: str, location_id: str = "01400943") -> list[dict]:
    """
    Search Kroger product catalog for an item.

    Returns up to 5 products: [{product_id, name, price, unit, image_url}]

    location_id defaults to a generic Kroger store. In production this should
    come from the user's selected store / address lookup.

    TODO: Wire real API call once KROGER_CLIENT_ID is set.
          Remove the stub block below and uncomment the real block.
    """
    # --- STUB (returns mock data when keys are not set) ---
    if not settings.KROGER_CLIENT_ID:
        return _stub_search(item_name)

    # --- REAL Kroger API call ---
    token = await _get_app_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_KROGER_BASE}/products",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "filter.term": item_name,
                "filter.locationId": location_id,
                "filter.limit": 5,
            },
        )
        resp.raise_for_status()
        products = resp.json().get("data", [])

    results = []
    for p in products:
        price_info = p.get("items", [{}])[0]
        price = price_info.get("price", {}).get("regular")
        results.append({
            "product_id": p.get("productId"),
            "name": p.get("description", item_name),
            "price": float(price) if price else None,
            "unit": price_info.get("size", ""),
            "image_url": (p.get("images") or [{}])[0].get("sizes", [{}])[-1].get("url"),
        })
    return results


async def add_to_cart(user_access_token: str, items: list[dict]) -> dict:
    """
    Add items to the authenticated user's Kroger cart.

    items: [{product_id, quantity}]

    Requires the user to have authorized Kroger via OAuth
    (stored in connected_accounts with provider='kroger').

    TODO: Implement user OAuth linking flow in frontend + backend.
    """
    if not settings.KROGER_CLIENT_ID:
        return {"status": "stub", "message": "Kroger cart stub — keys not configured"}

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{_KROGER_BASE}/cart/add",
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json",
            },
            json={"items": [{"upc": i["product_id"], "quantity": i["quantity"]} for i in items]},
        )
        resp.raise_for_status()
    return {"status": "added", "item_count": len(items)}


def _stub_search(item_name: str) -> list[dict]:
    """Deterministic mock results for development without API keys."""
    base_price = round(2.0 + len(item_name) % 5, 2)
    return [
        {
            "product_id": f"stub-kroger-{item_name.lower().replace(' ', '-')}-1",
            "name": f"{item_name.title()} (Kroger Brand)",
            "price": base_price,
            "unit": "1 ea",
            "image_url": None,
        },
        {
            "product_id": f"stub-kroger-{item_name.lower().replace(' ', '-')}-2",
            "name": f"{item_name.title()} (Simple Truth Organic)",
            "price": round(base_price * 1.3, 2),
            "unit": "1 ea",
            "image_url": None,
        },
    ]
