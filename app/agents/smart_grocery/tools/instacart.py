"""
Python bridge to the Instacart TypeScript service.

The TypeScript service (services/instacart/) owns all Instacart API calls.
This module makes HTTP calls to that service so LangGraph nodes can use it.

When the TS service is not running (dev without keys), every function falls
back to deterministic stub data so the agent graph still executes end-to-end.

Environment variables:
  INSTACART_SERVICE_URL  — defaults to http://localhost:3001
  INSTACART_SERVICE_KEY  — internal service-to-service key (X-Service-Key header)
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SERVICE_URL = os.getenv("INSTACART_SERVICE_URL", "http://localhost:3001")
_SERVICE_KEY = os.getenv("INSTACART_SERVICE_KEY", "")
_TIMEOUT = 15.0


def _headers() -> dict[str, str]:
    return {"X-Service-Key": _SERVICE_KEY, "Content-Type": "application/json"}


async def call_instacart_service(endpoint: str, payload: dict) -> dict[str, Any]:
    """
    POST to a TypeScript service endpoint and return the JSON response.
    Falls back to stub data if the service is unreachable.
    """
    url = f"{_SERVICE_URL}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Instacart service unreachable (%s): %s — using stub", url, e)
        return {"stub": True}


async def get_stores(zip_code: str) -> list[dict]:
    """Return available retailers for a zip code."""
    result = await call_instacart_service("/api/stores", {"zip_code": zip_code})
    if result.get("stub"):
        return _stub_stores()
    return result.get("stores", [])


async def search_products(store_id: str, query: str, limit: int = 5) -> list[dict]:
    """Search a store's catalog. Returns [{sku, name, price, available, ...}]"""
    result = await call_instacart_service(
        "/api/products/search",
        {"store_id": store_id, "query": query, "limit": limit},
    )
    if result.get("stub"):
        return _stub_search(query, store_id)
    return result.get("products", [])


async def create_cart(store_id: str, items: list[dict]) -> dict:
    """
    Create a cart at the given store.
    items: [{sku, qty, substitution?: {type, fallback_sku?, fallback_name?}}]
    Returns: {cart_id, estimated_total, ...}
    """
    result = await call_instacart_service(
        "/api/cart",
        {"store_id": store_id, "items": items},
    )
    if result.get("stub"):
        total = sum(i.get("qty", 1) * 3.5 for i in items)
        return {"cart_id": f"stub-cart-{store_id}", "estimated_total": round(total, 2)}
    return result


async def execute_checkout(
    cart_id: str,
    max_budget: float,
    fulfillment_type: str = "delivery",
    pickup_location_id: str | None = None,
    bypass_budget: bool = False,
) -> dict:
    """
    Attempt checkout with budget guard.
    Returns one of:
      {success: true,  result: {checkout_url, estimated_total, ...}}
      {success: false, reason: "budget_exceeded", estimated_total, max_budget}
      {success: false, reason: "api_error", message}
    """
    result = await call_instacart_service(
        "/api/checkout",
        {
            "cart_id": cart_id,
            "max_budget": max_budget,
            "fulfillment_type": fulfillment_type,
            "pickup_location_id": pickup_location_id,
            "bypass_budget": bypass_budget,
        },
    )
    if result.get("stub"):
        return {
            "success": True,
            "result": {
                "cart_id": cart_id,
                "checkout_url": "https://www.instacart.com/store",
                "estimated_total": 0.0,
                "fulfillment_type": fulfillment_type,
                "method": "webview",
            },
        }
    return result


# ---------------------------------------------------------------------------
# Stubs — used when TS service is unreachable (dev / CI)
# ---------------------------------------------------------------------------

def _stub_stores() -> list[dict]:
    return [
        {
            "store_id": "stub-walmart-1",
            "name": "Walmart",
            "logo_url": None,
            "supports_delivery": True,
            "supports_pickup": True,
            "distance_miles": 1.2,
        },
        {
            "store_id": "stub-publix-1",
            "name": "Publix",
            "logo_url": None,
            "supports_delivery": True,
            "supports_pickup": True,
            "distance_miles": 2.5,
        },
        {
            "store_id": "stub-costco-1",
            "name": "Costco",
            "logo_url": None,
            "supports_delivery": True,
            "supports_pickup": False,
            "distance_miles": 4.1,
        },
    ]


def _stub_search(query: str, store_id: str) -> list[dict]:
    base_price = round(2.5 + len(query) % 5, 2)
    return [
        {
            "sku": f"stub-{store_id}-{query.lower().replace(' ', '-')}-1",
            "name": f"{query.title()}",
            "brand": "Store Brand",
            "size": "1 unit",
            "price": base_price,
            "sale_price": None,
            "available": True,
            "image_url": None,
            "aisle": "Pantry",
        },
        {
            "sku": f"stub-{store_id}-{query.lower().replace(' ', '-')}-2",
            "name": f"{query.title()} (Organic)",
            "brand": "Organic Brand",
            "size": "1 unit",
            "price": round(base_price * 1.3, 2),
            "sale_price": None,
            "available": True,
            "image_url": None,
            "aisle": "Pantry",
        },
    ]
