"""
Agent API routes.

Smart Grocery — 3 endpoints:

  POST /agents/smart-grocery/start
    → Kicks off the graph. Runs until human_checkpoint interrupt.
    → Returns cart preview, price comparison, savings summary.

  POST /agents/smart-grocery/confirm
    → Resumes the graph after user review.
    → confirmed=true  → places order, writes to DB, returns checkout URL.
    → confirmed=false → cancels, returns status.

  GET  /agents/smart-grocery/status/{thread_id}
    → Returns current graph state for a session (polling / resume).
"""

import time
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.agents.smart_grocery.graph import graph
from app.agents.smart_grocery.state import SmartGroceryState

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class StartSmartGroceryRequest(BaseModel):
    store_preference: Literal["kroger", "instacart", "walmart", "whole_foods"] = "kroger"
    delivery_preference: Literal["delivery", "pickup"] = "delivery"
    budget: float | None = None


class ConfirmSmartGroceryRequest(BaseModel):
    thread_id: str
    confirmed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _thread_id(user_id: str, session_ts: int) -> str:
    return f"smart-grocery-{user_id}-{session_ts}"


def _extract_interrupt_payload(graph_state) -> dict | None:
    """Pull the interrupt payload out of graph state tasks if present."""
    tasks = getattr(graph_state, "tasks", None) or []
    for task in tasks:
        interrupts = getattr(task, "interrupts", None) or []
        for interrupt in interrupts:
            value = getattr(interrupt, "value", None)
            if value:
                return value
    return None


def _format_run_response(state_values: dict, thread_id: str, status: str) -> dict:
    return {
        "thread_id": thread_id,
        "status": status,
        # What the user sees for review
        "cart": {
            "items": state_values.get("cart_items", []),
            "total": state_values.get("cart_total", 0),
            "item_count": len(state_values.get("cart_items", [])),
            "store": state_values.get("store_preference"),
            "delivery_preference": state_values.get("delivery_preference"),
        },
        "price_comparison": state_values.get("price_comparison", {}),
        "savings_summary": state_values.get("savings_summary", {}),
        "missing_items": state_values.get("missing_items", []),
        "pantry_snapshot": {
            "total_items": len(state_values.get("pantry_items", [])),
            "expiring_count": sum(
                1 for i in state_values.get("pantry_items", [])
                if i.get("freshnessStatus") in ("expiring", "use_soon", "expired")
            ),
        },
        "error": state_values.get("error"),
    }


# ---------------------------------------------------------------------------
# POST /agents/smart-grocery/start
# ---------------------------------------------------------------------------

@router.post("/smart-grocery/start")
async def start_smart_grocery(
    body: StartSmartGroceryRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Start a new Smart Grocery agent session.

    Runs the full graph up to the human_checkpoint interrupt.
    Returns the cart preview, price comparisons, and savings info
    for the user to review before confirming the order.
    """
    uid = current_user["id"]
    session_ts = int(time.time())
    thread_id = _thread_id(uid, session_ts)
    config = {"configurable": {"thread_id": thread_id}}

    initial_state: SmartGroceryState = {
        "user_id": uid,
        "store_preference": body.store_preference,
        "delivery_preference": body.delivery_preference,
        "budget": body.budget,
        # All other fields start empty — nodes populate them
        "pantry_items": [],
        "saved_recipes": [],
        "existing_grocery_items": [],
        "user_preferences": {},
        "missing_items": [],
        "store_search_results": {},
        "price_comparison": {},
        "savings_summary": {},
        "cart_items": [],
        "cart_total": 0.0,
        "order_confirmed": None,
        "order_result": None,
        "grocery_list_id": None,
        "messages": [],
        "error": None,
    }

    try:
        # Run until interrupt_before=["human_checkpoint"]
        await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        logger.error("smart_grocery start failed for user %s: %s", uid, e)
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    # Read state after the run (graph is paused at human_checkpoint)
    graph_state = graph.get_state(config)
    state_values = graph_state.values

    return {
        **_format_run_response(state_values, thread_id, "awaiting_confirmation"),
        "session_ts": session_ts,
    }


# ---------------------------------------------------------------------------
# POST /agents/smart-grocery/confirm
# ---------------------------------------------------------------------------

@router.post("/smart-grocery/confirm")
async def confirm_smart_grocery(
    body: ConfirmSmartGroceryRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Resume the paused graph after user reviews the cart.

    confirmed=true  → place_order → finalize → returns checkout URL
    confirmed=false → cancelled   → returns cancelled status
    """
    uid = current_user["id"]
    config = {"configurable": {"thread_id": body.thread_id}}

    # Verify this thread belongs to the current user
    if not body.thread_id.startswith(f"smart-grocery-{uid}-"):
        raise HTTPException(status_code=403, detail="Thread does not belong to this user")

    # Check graph is actually paused
    graph_state = graph.get_state(config)
    if not graph_state or not graph_state.values:
        raise HTTPException(status_code=404, detail="Session not found or already completed")

    try:
        # Resume graph — pass the user's confirmation decision
        await graph.ainvoke(
            {"order_confirmed": body.confirmed},
            config=config,
        )
    except Exception as e:
        logger.error("smart_grocery confirm failed for user %s: %s", uid, e)
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    graph_state = graph.get_state(config)
    state_values = graph_state.values
    order_result = state_values.get("order_result", {})

    if not body.confirmed:
        return {"thread_id": body.thread_id, "status": "cancelled"}

    return {
        "thread_id": body.thread_id,
        "status": "order_placed",
        "order": order_result,
        "grocery_list_id": state_values.get("grocery_list_id"),
        "cart_total": state_values.get("cart_total", 0),
        # checkout_url is opened in in-app WebView by the frontend
        "checkout_url": order_result.get("checkout_url"),
        "method": order_result.get("method"),
    }


# ---------------------------------------------------------------------------
# GET /agents/smart-grocery/status/{thread_id}
# ---------------------------------------------------------------------------

@router.get("/smart-grocery/status/{thread_id}")
def smart_grocery_status(
    thread_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Poll current state of an active Smart Grocery session.
    Useful for the frontend to show live progress during the agent run.
    """
    uid = current_user["id"]

    if not thread_id.startswith(f"smart-grocery-{uid}-"):
        raise HTTPException(status_code=403, detail="Thread does not belong to this user")

    config = {"configurable": {"thread_id": thread_id}}
    graph_state = graph.get_state(config)

    if not graph_state or not graph_state.values:
        raise HTTPException(status_code=404, detail="Session not found")

    state_values = graph_state.values
    next_nodes = graph_state.next

    # Determine human-readable status
    if not next_nodes:
        order_result = state_values.get("order_result", {})
        status = order_result.get("status", "completed")
    elif "human_checkpoint" in next_nodes:
        status = "awaiting_confirmation"
    else:
        status = "running"

    return _format_run_response(state_values, thread_id, status)
