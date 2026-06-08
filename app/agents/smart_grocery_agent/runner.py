"""
smart_grocery_agent/runner.py
──────────────────────────────
Two-phase runner for the Smart Grocery ADK orchestrator.

Overview
────────
Grocery planning has a mandatory human review step — the user must see and
approve their cart before any order is placed. This runner implements that
as two separate async functions:

  Phase 1 — start_grocery_agent()
    Runs the full ADK orchestrator (5 tool calls → cart built).
    Stores the resulting cart in _pending_sessions keyed by a session_id.
    Returns the cart to the FastAPI route, which sends it to the frontend.
    The agent completes — nothing is paused or blocked.

  Phase 2 — confirm_grocery_order()
    Called after the user reviews and taps confirm/cancel in the app.
    Reads the pre-built cart from _pending_sessions.
    If confirmed: calls _add_to_kroger_cart() + _finalize_to_db().
    If cancelled: removes the session and returns a cancelled status.
    No LLM is involved in Phase 2 — it's purely deterministic.

Why not LangGraph interrupt?
────────────────────────────
The previous implementation used LangGraph's `interrupt()` primitive to pause
the graph mid-run. ADK doesn't have an equivalent — agents run to completion.
The two-phase pattern achieves the same UX outcome: the user reviews before
anything is ordered, and the agent result is preserved between the two HTTP calls.

Session storage
───────────────
_pending_sessions is an in-process dict (same trade-off as MemorySaver in the
old LangGraph implementation). Sessions expire after 1 hour. For production,
swap this for a DB-backed store — e.g. a `pending_carts` table — so sessions
survive server restarts and work across multiple process replicas.

Public functions
────────────────
  start_grocery_agent(user_id, delivery_preference, budget) → dict
  confirm_grocery_order(session_id, user_id, confirmed, store_override) → dict

Internal functions (not imported elsewhere)
───────────────────────────────────────────
  _add_to_kroger_cart()  — adds confirmed items to user's real Kroger cart
  _finalize_to_db()      — writes confirmed cart to grocery_lists / grocery_items
  _stub_start_response() — returns fake data when GOOGLE_API_KEY is absent
  _prune_expired()       — removes sessions older than SESSION_TTL_SECONDS
"""

import json
import logging
import time
import uuid
from typing import TypedDict, Literal

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.db import get_async_pool
from app.config import settings
from .agent import smart_grocery_agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process session store (swap for DB in production)
# ---------------------------------------------------------------------------

class _PendingSession(TypedDict):
    """Cart state kept between the /start and /confirm API calls."""
    user_id: str
    session_id: str
    adk_session_id: str
    state: dict          # full tool_context.state snapshot from Phase 1
    created_at: float    # unix timestamp; used for TTL expiry


# Keyed by session_id — one entry per active user cart review.
_pending_sessions: dict[str, _PendingSession] = {}
_SESSION_TTL_SECONDS = 3600  # sessions expire after 1 hour of inactivity


def _prune_expired() -> None:
    """Remove sessions older than SESSION_TTL_SECONDS to prevent memory growth."""
    now = time.time()
    expired = [k for k, v in _pending_sessions.items() if now - v["created_at"] > _SESSION_TTL_SECONDS]
    for k in expired:
        del _pending_sessions[k]


# ─────────────────────────────────────────────────────────────────────────────
# Shared ADK runtime (created once per process)
# ─────────────────────────────────────────────────────────────────────────────

# Runner and session_service are module-level singletons. ADK's InMemorySessionService
# is thread-safe and the Runner holds no per-request state, so sharing them across
# concurrent requests is safe. Each request gets its own ADK session via create_session().
_session_service = InMemorySessionService()
_runner = Runner(
    agent=smart_grocery_agent,
    app_name="smart_grocery_agent",
    session_service=_session_service,
)

# ---------------------------------------------------------------------------
# Phase 1 — Plan and build the cart
# ---------------------------------------------------------------------------

async def start_grocery_agent(
    user_id: str,
    delivery_preference: Literal["delivery", "pickup"] = "delivery",
    budget: float | None = None,
) -> dict:
    """
    Phase 1: Run the grocery planning agent and return a cart for user review.

    Runs the ADK orchestrator, which calls 5 tools in sequence:
      load_user_context → resolve_nearby_stores → analyze_missing_items
          → search_kroger_products → build_grocery_cart

    After the agent completes, the full tool_context.state (cart, prices,
    store info, etc.) is snapshotted into _pending_sessions so confirm_grocery_order()
    can access it without re-running the agent.

    Args:
        user_id: Firebase UID of the requesting user.
        delivery_preference: "pickup" or "delivery" — passed to the Kroger search.
        budget: Optional weekly budget override. If None, uses the user's saved
                weekly_budget preference from their profile.

    Returns a dict with:
      session_id       — opaque token; pass to confirm_grocery_order()
      status           — always "awaiting_confirmation" on success
      cart             — {items, total, item_count, store, delivery_preference}
      price_comparison — estimated totals across store chains
      savings_summary  — best/worst/preferred store comparison
      nearby_stores    — ranked list of stores near the user's zip code
      missing_items    — what Claude identified as needing to be bought
      pantry_snapshot  — {total_items, expiring_count} summary
      agent_summary    — one-sentence summary from Gemini (for UI display)
      error            — set on failure; cart populated with stub data
    """
    if not settings.GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY not set — returning stub session")
        return _stub_start_response(user_id, delivery_preference, budget)

    _prune_expired()

    session_id = f"sg-{user_id}-{uuid.uuid4().hex[:10]}"
    adk_user_id = f"user-{user_id}"

    adk_session = await _session_service.create_session(
        app_name="smart_grocery_agent",
        user_id=adk_user_id,
    )

    prompt = (
        f"Start grocery planning for user_id={user_id}. "
        f"Delivery preference: {delivery_preference}. "
        f"Budget: {budget or 'use user preference'}."
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    agent_summary: str | None = None
    try:
        async for event in _runner.run_async(
            user_id=adk_user_id,
            session_id=adk_session.id,
            new_message=content,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                agent_summary = event.content.parts[0].text
    except Exception as e:
        logger.error("smart_grocery ADK run failed for user %s: %s", user_id, e)
        return {**_stub_start_response(user_id, delivery_preference, budget), "error": str(e)}

    # Read tool_context.state back out of the ADK session
    refreshed = await _session_service.get_session(
        app_name="smart_grocery_agent",
        user_id=adk_user_id,
        session_id=adk_session.id,
    )
    state = dict(refreshed.state) if refreshed else {}

    # Store for confirm step
    _pending_sessions[session_id] = {
        "user_id": user_id,
        "session_id": session_id,
        "adk_session_id": adk_session.id,
        "state": state,
        "created_at": time.time(),
    }

    cart_items = state.get("cart_items", [])
    return {
        "session_id": session_id,
        "status": "awaiting_confirmation",
        "cart": {
            "items": cart_items,
            "total": state.get("cart_total", 0),
            "item_count": len(cart_items),
            "store": state.get("store_preference", "kroger"),
            "delivery_preference": state.get("delivery_preference", delivery_preference),
        },
        "price_comparison": state.get("price_comparison", {}),
        "savings_summary": state.get("savings_summary", {}),
        "nearby_stores": state.get("nearby_stores", []),
        "missing_items": state.get("missing_items", []),
        "pantry_snapshot": {
            "total_items": len(state.get("pantry_items", [])),
            "expiring_count": sum(
                1 for i in state.get("pantry_items", [])
                if i.get("freshnessStatus") in ("expiring", "use_soon", "expired")
            ),
        },
        "agent_summary": agent_summary,
        "error": state.get("error"),
    }


# ---------------------------------------------------------------------------
# Phase 2 — Confirm or cancel
# ---------------------------------------------------------------------------

async def confirm_grocery_order(
    session_id: str,
    user_id: str,
    confirmed: bool,
    store_override: str | None = None,
) -> dict:
    """
    Phase 2: Finalize or cancel a planned grocery order. No LLM involved.

    Reads the pre-built cart from _pending_sessions (written by start_grocery_agent).
    On confirmation, runs two side effects in sequence:
      1. _add_to_kroger_cart() — HTTP PUT to Kroger cart API for each item that
         has a valid product_id. Requires user's Kroger OAuth token in connected_accounts.
      2. _finalize_to_db() — upserts items into grocery_lists / grocery_items and
         sets the list's estimated_total.

    Args:
        session_id: Token returned by start_grocery_agent().
        user_id: Firebase UID — validated against the session to prevent spoofing.
        confirmed: True to place the order; False to cancel.
        store_override: Optional store slug if the user switched stores at review.

    Returns a dict with:
      session_id, status ("order_placed" | "cancelled")
      On order_placed: kroger_cart, grocery_list_id, cart_total, checkout_url

    Raises:
        ValueError: session_id not found or expired
        PermissionError: session belongs to a different user
    """
    pending = _pending_sessions.get(session_id)
    if not pending:
        raise ValueError("Session not found or expired")
    if pending["user_id"] != user_id:
        raise PermissionError("Session does not belong to this user")

    if not confirmed:
        del _pending_sessions[session_id]
        return {"session_id": session_id, "status": "cancelled"}

    state = pending["state"]
    if store_override:
        state["store_preference"] = store_override

    cart_items = state.get("cart_items", [])
    kroger_result = state.get("kroger_result", {})

    # Add items to Kroger cart (requires user OAuth token — stub until OAuth flow is built)
    kroger_cart_status = await _add_to_kroger_cart(cart_items, kroger_result, user_id)

    # Write confirmed cart to DB
    grocery_list_id = await _finalize_to_db(state, user_id)

    del _pending_sessions[session_id]

    return {
        "session_id": session_id,
        "status": "order_placed",
        "kroger_cart": kroger_cart_status,
        "grocery_list_id": grocery_list_id,
        "cart_total": state.get("cart_total", 0),
        "item_count": len(cart_items),
        "store": state.get("store_preference", "kroger"),
        # checkout_url: deep-link to Kroger app/web checkout
        # Requires user OAuth token — set once OAuth linking is implemented
        "checkout_url": "https://www.kroger.com/cart",
    }


# ---------------------------------------------------------------------------
# Internal: add items to Kroger cart via kroger-api
# ---------------------------------------------------------------------------

async def _add_to_kroger_cart(
    cart_items: list,
    kroger_result: dict,
    user_id: str,
) -> dict:
    """
    Add confirmed cart items to the user's real Kroger cart via the Kroger Cart API.

    Flow:
      1. Fetch the user's Kroger OAuth access token from connected_accounts table
         (provider='kroger'). Returns "pending_oauth" if not linked yet.
      2. Filter cart_items to those with real Kroger product_ids (not stub- prefixed).
      3. PUT /v1/cart/add with the item UPCs and quantities.

    The Kroger Cart API is write-only — it cannot read back or remove cart items.
    Cart state is tracked in _pending_sessions / grocery_list_items instead.

    Returns a status dict (not raises) so a cart failure doesn't block the DB write.

    TODO: Build the OAuth linking flow to populate connected_accounts:
      POST /auth/kroger/connect → returns OAuth URL for the frontend to open
      GET  /auth/kroger/callback → exchanges code, stores token in connected_accounts
    """
    if not settings.KROGER_CLIENT_ID:
        return {"status": "stub", "message": "Kroger credentials not configured"}

    pool = get_async_pool()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT access_token FROM connected_accounts
                       WHERE user_id = %s AND provider = 'kroger'
                       AND expires_at > NOW() LIMIT 1""",
                    (user_id,)
                )
                row = await cur.fetchone()
    except Exception:
        row = None

    if not row:
        return {
            "status": "pending_oauth",
            "message": "User has not linked their Kroger account yet",
        }

    # Build payload for kroger-api add_to_cart
    items_payload = []
    store_info = kroger_result.get("store", {})
    location_id = store_info.get("location_id") if store_info else None

    for item in cart_items:
        if item.get("product_id") and not item["product_id"].startswith("stub-"):
            items_payload.append({
                "upc": item["product_id"],
                "quantity": int(max(item.get("quantity", 1), 1)),
                "modality": "PICKUP",
            })

    if not items_payload:
        return {"status": "no_valid_products", "message": "No Kroger product IDs available to add"}

    # Direct HTTP call to Kroger cart API
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                "https://api.kroger.com/v1/cart/add",
                headers={
                    "Authorization": f"Bearer {row['access_token']}",
                    "Content-Type": "application/json",
                },
                json={"items": items_payload},
                timeout=10.0,
            )
            resp.raise_for_status()
        return {"status": "added", "item_count": len(items_payload), "location_id": location_id}
    except Exception as e:
        logger.error("Kroger cart add failed: %s", e)
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Internal: write confirmed cart to DB grocery list
# ---------------------------------------------------------------------------

async def _finalize_to_db(state: dict, user_id: str) -> str | None:
    """
    Persist the confirmed cart to the database as a grocery list.

    Finds or creates the user's active grocery_list, then upserts each cart
    item into grocery_items with its ingredient_id, aisle, and estimated_price.
    Updates the list's estimated_total on completion.

    Returns the grocery_list_id (UUID string) on success, or None on failure.
    Failure is logged but not raised — a DB write failure doesn't roll back the
    Kroger cart add that already happened.
    """
    cart_items = state.get("cart_items", [])
    if not cart_items:
        return None

    pool = get_async_pool()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM grocery_lists WHERE user_id = %s AND status = 'active' ORDER BY created_at LIMIT 1",
                    (user_id,)
                )
                row = await cur.fetchone()
                if row:
                    list_id = str(row["id"])
                else:
                    await cur.execute(
                        "INSERT INTO grocery_lists (user_id, name, status) VALUES (%s, 'My List', 'active') RETURNING id",
                        (user_id,)
                    )
                    list_id = str((await cur.fetchone())["id"])

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
                await cur.execute(
                    "UPDATE grocery_lists SET estimated_total = %s WHERE id = %s",
                    (state.get("cart_total"), list_id)
                )
        return list_id
    except Exception as e:
        logger.error("_finalize_to_db failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Stub (used when GOOGLE_API_KEY is not set)
# ---------------------------------------------------------------------------

def _stub_start_response(
    user_id: str,
    delivery_preference: str,
    budget: float | None,
) -> dict:
    session_id = f"sg-stub-{user_id}-{uuid.uuid4().hex[:8]}"
    stub_items = [
        {"name": "eggs", "quantity": 1, "unit": "dozen", "product_id": "stub-eggs", "estimated_price": 3.49, "store": "kroger", "aisle": "Dairy"},
        {"name": "milk", "quantity": 1, "unit": "gallon", "product_id": "stub-milk", "estimated_price": 4.29, "store": "kroger", "aisle": "Dairy"},
        {"name": "olive oil", "quantity": 1, "unit": "bottle", "product_id": "stub-olive-oil", "estimated_price": 6.99, "store": "kroger", "aisle": "Pantry"},
    ]
    total = sum(i["estimated_price"] for i in stub_items)
    _pending_sessions[session_id] = {
        "user_id": user_id,
        "session_id": session_id,
        "adk_session_id": "stub",
        "state": {
            "cart_items": stub_items,
            "cart_total": total,
            "store_preference": "kroger",
            "delivery_preference": delivery_preference,
            "price_comparison": {"kroger": total, "walmart": round(total * 0.88, 2), "whole_foods": round(total * 1.28, 2)},
            "savings_summary": {},
            "nearby_stores": [],
            "missing_items": [],
            "pantry_items": [],
            "kroger_result": {},
        },
        "created_at": time.time(),
    }
    return {
        "session_id": session_id,
        "status": "awaiting_confirmation",
        "cart": {"items": stub_items, "total": total, "item_count": len(stub_items), "store": "kroger", "delivery_preference": delivery_preference},
        "price_comparison": {},
        "savings_summary": {},
        "nearby_stores": [],
        "missing_items": [],
        "pantry_snapshot": {"total_items": 0, "expiring_count": 0},
        "agent_summary": "Stub mode — add GOOGLE_API_KEY to enable the full agent.",
        "error": None,
    }
