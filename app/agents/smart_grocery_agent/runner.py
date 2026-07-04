"""
smart_grocery_agent/runner.py
──────────────────────────────
Public entry points for the Smart Grocery LangGraph — thin wrappers that
translate FastAPI request/response shapes onto graph invocations.

  start_grocery_agent(user_id, delivery_preference, budget) -> dict
    Phase 1. Runs the graph from load_context through build_cart, where
    human_checkpoint's interrupt() suspends it. ainvoke() returns as soon as
    that happens, carrying the state computed so far — this is packaged up
    as the cart-for-review response.

  confirm_grocery_order(session_id, user_id, confirmed, store_override) -> dict
    Phase 2. Resumes the same graph run via Command(resume=...). If confirmed,
    the graph continues through place_order → finalize. If not, it routes to
    the cancelled terminal node instead — no LLM involved either way, this
    step is pure graph routing.

  get_session_status(session_id, user_id) -> dict
    Reads the checkpointed state without resuming anything — used by the
    GET /status endpoint.

Session identity
─────────────────
Each session is one LangGraph thread: `thread_id=session_id`. The checkpointer
(MemorySaver, see graph.py) is what makes the interrupted state available to
confirm/status calls that arrive as separate HTTP requests. There's no
separate session dict to keep in sync — the graph's own checkpoint *is* the
session store, and validating ownership is just checking
`state["user_id"] == user_id` on whatever the checkpointer has for that thread.
"""

import logging
import uuid
from typing import Literal

from langgraph.types import Command

from .graph import smart_grocery_graph

logger = logging.getLogger(__name__)


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


async def _get_owned_state(session_id: str, user_id: str) -> dict:
    """Fetch checkpointed state for a session, raising if missing or owned by someone else."""
    snapshot = await smart_grocery_graph.aget_state(_config(session_id))
    if not snapshot or not snapshot.values:
        raise ValueError("Session not found or expired")
    if snapshot.values.get("user_id") != user_id:
        raise PermissionError("Session does not belong to this user")
    return snapshot.values


def _cart_response(session_id: str, state: dict) -> dict:
    cart_items = state.get("cart_items", [])
    return {
        "session_id": session_id,
        "status": "awaiting_confirmation",
        "cart": {
            "items": cart_items,
            "total": state.get("cart_total", 0),
            "item_count": len(cart_items),
            "store": state.get("store_preference", "kroger"),
            "delivery_preference": state.get("delivery_preference", "delivery"),
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
        "agent_summary": state.get("agent_summary"),
        "error": state.get("error"),
    }


# ---------------------------------------------------------------------------
# Phase 1 — Plan and build the cart
# ---------------------------------------------------------------------------

async def start_grocery_agent(
    user_id: str,
    delivery_preference: Literal["delivery", "pickup"] = "delivery",
    budget: float | None = None,
) -> dict:
    """
    Phase 1: Run the grocery planning graph up to the human checkpoint and
    return a cart for user review.

    Runs load_context → resolve_stores → analyze_pantry → search_store →
    compare_prices → build_cart → human_checkpoint (interrupt). Every node
    degrades gracefully on its own when API keys/credentials are missing
    (analyze_pantry and build_cart fall back to non-LLM heuristics, search_store
    falls back to stub Kroger data) — there's no separate top-level stub mode.

    Args:
        user_id: Firebase UID of the requesting user.
        delivery_preference: "pickup" or "delivery" — passed to the Kroger search.
        budget: Optional weekly budget override. If None, falls back to the
                user's saved weekly_budget preference (read in analyze_pantry).

    Returns a dict with:
      session_id       — opaque token; pass to confirm_grocery_order()/get_session_status()
      status           — always "awaiting_confirmation" on success
      cart             — {items, total, item_count, store, delivery_preference}
      price_comparison — estimated totals across store chains
      savings_summary  — best/worst/preferred store comparison
      nearby_stores    — ranked list of stores near the user's zip code
      missing_items    — what analyze_pantry identified as needing to be bought
      pantry_snapshot  — {total_items, expiring_count} summary
      agent_summary    — one-sentence deterministic summary (for UI display)
      error            — set if a node hit a recoverable failure
    """
    session_id = f"sg-{user_id}-{uuid.uuid4().hex[:10]}"

    initial_state = {
        "user_id": user_id,
        "delivery_preference": delivery_preference,
        "budget": budget,
    }

    try:
        state = await smart_grocery_graph.ainvoke(initial_state, config=_config(session_id))
    except Exception as e:
        logger.error("smart_grocery graph run failed for user %s: %s", user_id, e)
        raise

    return _cart_response(session_id, state)


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
    Phase 2: Resume the graph past human_checkpoint to finalize or cancel.

    Resumes via Command(resume={"confirmed": ..., "store_override": ...}).
    human_checkpoint's interrupt() call receives this dict as its return value
    and the conditional edge in graph.py routes to place_order (confirmed) or
    cancelled (not confirmed).

    Args:
        session_id: Token returned by start_grocery_agent().
        user_id: Firebase UID — validated against the checkpointed session state
                 to prevent one user from confirming another's cart.
        confirmed: True to place the order; False to cancel.
        store_override: Optional store slug if the user switched stores at review.

    Returns a dict with:
      session_id, status ("order_placed" | "cancelled")
      On order_placed: kroger_cart, grocery_list_id, cart_total, checkout_url

    Raises:
        ValueError: session_id not found or expired
        PermissionError: session belongs to a different user
    """
    await _get_owned_state(session_id, user_id)  # raises if missing/not owned

    resume_value = {"confirmed": confirmed, "store_override": store_override}
    state = await smart_grocery_graph.ainvoke(Command(resume=resume_value), config=_config(session_id))

    if not confirmed:
        return {"session_id": session_id, "status": "cancelled"}

    return {
        "session_id": session_id,
        "status": "order_placed",
        "kroger_cart": state.get("kroger_cart_status"),
        "grocery_list_id": state.get("grocery_list_id"),
        "cart_total": state.get("cart_total", 0),
        "item_count": len(state.get("cart_items", [])),
        "store": state.get("store_preference", "kroger"),
        # checkout_url: deep-link to Kroger app/web checkout
        # Requires user OAuth token — set once OAuth linking is implemented
        "checkout_url": "https://www.kroger.com/cart",
    }


# ---------------------------------------------------------------------------
# Status — read-only session lookup
# ---------------------------------------------------------------------------

async def get_session_status(session_id: str, user_id: str) -> dict:
    """
    Check the status of a Smart Grocery session without resuming it.

    Raises:
        ValueError: session_id not found or expired
        PermissionError: session belongs to a different user
    """
    snapshot = await smart_grocery_graph.aget_state(_config(session_id))
    if not snapshot or not snapshot.values:
        raise ValueError("Session not found or already completed")
    state = snapshot.values
    if state.get("user_id") != user_id:
        raise PermissionError("Session does not belong to this user")

    if snapshot.next:
        # human_checkpoint is still pending resume
        return _cart_response(session_id, state)

    status = state.get("status", "cancelled")
    return {
        "session_id": session_id,
        "status": status,
        "cart": {
            "items": state.get("cart_items", []),
            "total": state.get("cart_total", 0),
            "item_count": len(state.get("cart_items", [])),
            "store": state.get("store_preference", "kroger"),
            "delivery_preference": state.get("delivery_preference", "delivery"),
        },
        "price_comparison": state.get("price_comparison", {}),
        "savings_summary": state.get("savings_summary", {}),
        "nearby_stores": state.get("nearby_stores", []),
        "missing_items": state.get("missing_items", []),
    }
