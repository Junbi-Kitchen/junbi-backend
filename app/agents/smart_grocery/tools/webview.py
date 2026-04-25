"""
WebView cart URL builder for stores without a buyer-facing API.

For stores like Instacart, Walmart, etc., we generate a pre-filled cart
deep-link URL. The frontend opens this in an in-app WebView (WKWebView on iOS)
so the user never leaves the app.

Each builder takes a list of items and returns:
  {
    "store": "instacart",
    "checkout_url": "https://...",   ← opened in WebView
    "method": "webview",
  }
"""

import urllib.parse


def build_instacart_url(items: list[dict], retailer_slug: str = "walmart") -> dict:
    """
    Instacart storefront URL with search terms pre-filled.

    items: [{name, quantity, unit}]

    Instacart doesn't support a true pre-filled cart URL via public API,
    so we land on a search results page for the first item and let the
    user build the cart inside the WebView.

    TODO: If Instacart grants Connect API access, replace with their
          storefront embed or deep-link spec.
    """
    if not items:
        return {"store": "instacart", "checkout_url": "https://www.instacart.com", "method": "webview"}

    first_item = items[0]["name"]
    search_url = f"https://www.instacart.com/store/{retailer_slug}/search_v3/{urllib.parse.quote(first_item)}"
    return {
        "store": "instacart",
        "checkout_url": search_url,
        "method": "webview",
        "item_count": len(items),
    }


def build_walmart_url(items: list[dict]) -> dict:
    """
    Walmart search URL for the first item (affiliate deep-link).

    Walmart has no public cart API — deep-links to their search page.
    """
    if not items:
        return {"store": "walmart", "checkout_url": "https://www.walmart.com/grocery", "method": "webview"}

    first_item = items[0]["name"]
    search_url = f"https://www.walmart.com/search?q={urllib.parse.quote(first_item)}&typeahead={urllib.parse.quote(first_item)}"
    return {
        "store": "walmart",
        "checkout_url": search_url,
        "method": "webview",
        "item_count": len(items),
    }


def build_whole_foods_url(items: list[dict]) -> dict:
    """Amazon Fresh / Whole Foods — search landing only."""
    if not items:
        return {"store": "whole_foods", "checkout_url": "https://www.amazon.com/alm/storefront", "method": "webview"}

    first_item = items[0]["name"]
    search_url = f"https://www.amazon.com/s?k={urllib.parse.quote(first_item)}&i=amazonfresh"
    return {
        "store": "whole_foods",
        "checkout_url": search_url,
        "method": "webview",
        "item_count": len(items),
    }


def build_checkout_url(store: str, items: list[dict], delivery_preference: str = "delivery") -> dict:
    """
    Route to the correct URL builder for the given store.
    Fallback for any store not handled by the Instacart TS service.
    """
    builders = {
        "instacart": build_instacart_url,
        "walmart": build_walmart_url,
        "whole_foods": build_whole_foods_url,
    }

    if store in builders:
        result = builders[store](items)
    else:
        # Fallback: generic Instacart search
        result = build_instacart_url(items)

    result["delivery_preference"] = delivery_preference
    return result
