"""
kroger_agent/runner.py
──────────────────────
Public entry point for the Kroger ADK agent.

Primary export: `run_kroger_agent(items, zip_code, ...) → KrogerAgentResult`

This is the only function other modules should import from this package.
It manages the full lifecycle of an agent run — MCP subprocess, ADK session,
Gemini inference, response parsing — and returns a plain typed dict that any
caller can consume without knowing anything about ADK or MCP internals.

How a single call flows
───────────────────────
  1. Credentials check — if Kroger or Google keys are missing, returns stub data
     immediately so development works without live API credentials.
  2. `build_kroger_agent()` — creates a fresh LlmAgent + MCPToolset pair.
  3. `async with toolset:` — starts the `kroger-mcp` subprocess via stdio.
  4. ADK Runner + InMemorySessionService — minimal runtime for a single-turn
     agent session (no persistence needed; each grocery run is self-contained).
  5. `runner.run_async(...)` — Gemini calls MCP tools (location search, product
     search) iteratively until it has results for all items, then emits a final
     response event containing the JSON result.
  6. `_parse_agent_response()` — parses the JSON and maps it into KrogerAgentResult.
  7. On any failure — returns stub data with an `error` field set, so the
     smart_grocery_agent can continue with mock data rather than crashing.

Result types
────────────
KrogerAgentResult, KrogerProduct, KrogerCartItem, KrogerStoreInfo are TypedDicts
defined here. Their field names deliberately mirror the CartItem / StoreProduct
shapes used in smart_grocery_agent/tools.py so results can be passed through
without transformation.

Callers
───────
  - app/agents/smart_grocery_agent/tools.py  (search_kroger_products tool)
  - Future: a dedicated /agents/kroger/search FastAPI route for direct access
  - Future: other ADK agents that need Kroger product data
"""

import json
import logging
import uuid
from typing import TypedDict

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from config import settings
from .agent import build_kroger_agent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

class KrogerStoreInfo(TypedDict):
    """Identifies the Kroger store that was selected for this search run."""
    location_id: str   # Kroger's internal store identifier
    name: str          # Human-readable store name, e.g. "Kroger #123"
    address: str       # Street address or city string


class KrogerProduct(TypedDict):
    """One product match returned by the Kroger product search API."""
    product_id: str         # Kroger UPC / product ID — used for cart add calls
    name: str               # Product display name
    price: float | None     # Regular shelf price; None if not available
    unit: str               # Size string, e.g. "12 oz", "1 gal"
    image_url: str | None   # Front-of-package image URL
    aisle: str | None       # In-store aisle location, e.g. "Dairy"


class KrogerCartItem(TypedDict):
    """
    The agent's best product pick for one shopping-list item.
    Populated in cart_preview — one entry per requested item (skipped if unfound).
    """
    name: str                   # Original shopping-list item name
    product_id: str | None      # Kroger product ID for the chosen match
    product_name: str           # Full product name of the chosen match
    price: float | None         # Unit price
    quantity: float             # Requested quantity from the shopping list
    unit: str                   # Unit string (e.g. "dozen", "lbs")
    estimated_price: float | None  # price × quantity
    aisle: str | None


class KrogerAgentResult(TypedDict):
    """
    Full result returned by run_kroger_agent().

    store        — the Kroger location used for all searches
    results      — all product matches, keyed by shopping-list item name
    unfound      — item names for which Kroger returned zero results
    cart_preview — best single product pick per item (excludes unfound items)
    estimated_total — sum of all estimated_prices in cart_preview
    error        — set on partial/full failure; cart still populated with stub data
    """
    store: KrogerStoreInfo | None
    results: dict[str, list[KrogerProduct]]
    unfound: list[str]
    cart_preview: list[KrogerCartItem]
    estimated_total: float
    error: str | None


# ─────────────────────────────────────────────────────────────────────────────
# Stub fallback
# ─────────────────────────────────────────────────────────────────────────────

def _stub_result(items: list[str], zip_code: str) -> KrogerAgentResult:
    """
    Return deterministic fake data when credentials are missing or the agent fails.

    Prices are derived from item name length so the same item always returns the
    same stub price — useful for UI testing without live API calls.
    """
    cart_preview: list[KrogerCartItem] = []
    results: dict[str, list[KrogerProduct]] = {}
    total = 0.0

    for item in items:
        base_price = round(2.5 + len(item) % 4, 2)
        product: KrogerProduct = {
            "product_id": f"stub-kroger-{item.lower().replace(' ', '-')}",
            "name": f"{item.title()} (Kroger Brand)",
            "price": base_price,
            "unit": "1 ea",
            "image_url": None,
            "aisle": "Pantry",
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
# Response parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_agent_response(raw: str) -> KrogerAgentResult:
    """
    Parse the agent's final text response into a KrogerAgentResult.

    The agent is instructed to return raw JSON, but Gemini occasionally wraps
    it in markdown code fences (```json ... ```) — the strip handles that case.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Remove opening fence line (e.g. "```json")
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        # Remove closing fence
        text = text.rsplit("```", 1)[0]

    data = json.loads(text.strip())

    return {
        "store": data.get("store"),
        "results": data.get("results", {}),
        "unfound": data.get("unfound", []),
        "cart_preview": data.get("cart_preview", []),
        "estimated_total": float(data.get("estimated_total") or 0),
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_kroger_agent(
    items: list[str],
    zip_code: str,
    quantities: dict[str, float] | None = None,
    delivery_preference: str = "pickup",
) -> KrogerAgentResult:
    """
    Find Kroger products for a list of grocery items near a zip code.

    This is the only public function in this module. It handles the full
    ADK agent lifecycle — subprocess startup, session creation, inference,
    and cleanup — then returns a plain dict result.

    Args:
        items: Simple grocery item names, e.g. ["eggs", "whole milk", "olive oil"].
               Keep names generic — the agent handles brand/variant selection.
        zip_code: User's zip code. Used to find the nearest Kroger-family store
                  (Kroger, Ralphs, Fred Meyer, King Soopers, etc.).
        quantities: Optional mapping of item name → requested quantity.
                    Defaults to 1 per item if not provided.
        delivery_preference: "pickup" or "delivery". Passed to the agent prompt
                             so it can filter fulfillment options accordingly.

    Returns:
        KrogerAgentResult containing:
          - store: The Kroger location used for all searches
          - results: All product matches keyed by item name (up to 5 per item)
          - unfound: Items with no Kroger matches
          - cart_preview: Best single product pick per found item
          - estimated_total: Sum of cart_preview prices
          - error: Set on failure; result is still populated with stub data

    Raises:
        Nothing — all exceptions are caught and returned as error field + stub data.
    """
    # Guard: return stub immediately if required credentials are absent
    if not settings.KROGER_CLIENT_ID or not settings.KROGER_CLIENT_SECRET:
        logger.warning("Kroger credentials not set — returning stub result")
        return _stub_result(items, zip_code)

    if not settings.GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY not set — returning stub result")
        return _stub_result(items, zip_code)

    # Nothing to search — return an empty result rather than running the agent
    if not items:
        return {
            "store": None,
            "results": {},
            "unfound": [],
            "cart_preview": [],
            "estimated_total": 0.0,
            "error": None,
        }

    # Build the prompt: include quantities so the agent can compute estimated_price
    qty = quantities or {}
    items_payload = [
        {"name": item, "quantity": qty.get(item, 1)}
        for item in items
    ]

    prompt = (
        f"Find these grocery items at a Kroger store near zip code {zip_code}. "
        f"Fulfillment preference: {delivery_preference}. "
        f"Shopping list: {json.dumps(items_payload)}"
    )

    # Create a fresh agent + MCP toolset for this run.
    # The toolset is an async context manager that starts/stops the kroger-mcp
    # subprocess. Keeping it scoped to this function ensures clean subprocess
    # teardown even if the agent raises an exception.
    agent, toolset = build_kroger_agent()

    # InMemorySessionService is sufficient here — the Kroger agent is single-turn
    # (one prompt → one response) so we don't need persistent session storage.
    session_service = InMemorySessionService()
    app_name = "kroger_agent"

    try:
        async with toolset:
            runner = Runner(
                agent=agent,
                app_name=app_name,
                session_service=session_service,
            )

            # Use a random user_id per call — this agent doesn't carry user
            # identity; the smart_grocery_agent handles user-level context.
            user_id = f"system-{uuid.uuid4().hex[:8]}"
            session = await session_service.create_session(
                app_name=app_name,
                user_id=user_id,
            )

            content = types.Content(
                role="user",
                parts=[types.Part(text=prompt)],
            )

            # Stream events from the agent run. We only care about the final
            # response event — intermediate tool-call events are ignored.
            final_text: str | None = None
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=content,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = event.content.parts[0].text

            if not final_text:
                raise ValueError("Agent returned no response")

            return _parse_agent_response(final_text)

    except json.JSONDecodeError as e:
        # Agent responded but the JSON was malformed — log and fall back to stub
        logger.error("kroger_agent: failed to parse JSON response — %s", e)
        return {**_stub_result(items, zip_code), "error": f"parse_error: {e}"}
    except Exception as e:
        logger.error("kroger_agent: run failed — %s", e)
        return {**_stub_result(items, zip_code), "error": str(e)}
