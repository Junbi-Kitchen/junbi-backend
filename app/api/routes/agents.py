"""
Agent API routes — Smart Grocery (ADK orchestrator).

Same 3 endpoints as before, now backed by the ADK runner instead of LangGraph.

  POST /agents/smart-grocery/start
    → Runs the ADK orchestrator (load context → analyze → Kroger search → build cart).
    → Returns cart preview for user review.

  POST /agents/smart-grocery/confirm
    → Confirms or cancels the planned cart.
    → confirmed=true  → adds to Kroger cart + writes to DB → returns checkout URL.
    → confirmed=false → cleans up session → returns cancelled status.

  GET  /agents/smart-grocery/status/{session_id}
    → Returns current session state (pending / confirmed / cancelled).
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.agents.smart_grocery_agent.runner import (
    start_grocery_agent,
    confirm_grocery_order,
    _pending_sessions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class StartSmartGroceryRequest(BaseModel):
    delivery_preference: Literal["delivery", "pickup"] = "delivery"
    budget: float | None = None


class ConfirmSmartGroceryRequest(BaseModel):
    session_id: str
    confirmed: bool
    store_override: str | None = None  # user can switch store at review screen


# ---------------------------------------------------------------------------
# POST /agents/smart-grocery/start
# ---------------------------------------------------------------------------

@router.post("/smart-grocery/start")
async def start_smart_grocery(
    body: StartSmartGroceryRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Start a new Smart Grocery session.

    Runs the full ADK planning pipeline (pantry analysis → Kroger product search
    → cart building) and returns the cart for user review.
    """
    uid = current_user["id"]
    try:
        result = await start_grocery_agent(
            user_id=uid,
            delivery_preference=body.delivery_preference,
            budget=body.budget,
        )
    except Exception as e:
        logger.error("smart_grocery start failed for user %s: %s", uid, e)
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    return result


# ---------------------------------------------------------------------------
# POST /agents/smart-grocery/confirm
# ---------------------------------------------------------------------------

@router.post("/smart-grocery/confirm")
async def confirm_smart_grocery(
    body: ConfirmSmartGroceryRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Confirm or cancel a planned grocery cart.

    confirmed=true  → adds items to Kroger cart + writes to DB grocery list
    confirmed=false → session cleaned up, returns cancelled
    """
    uid = current_user["id"]
    try:
        result = await confirm_grocery_order(
            session_id=body.session_id,
            user_id=uid,
            confirmed=body.confirmed,
            store_override=body.store_override,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    except Exception as e:
        logger.error("smart_grocery confirm failed for user %s: %s", uid, e)
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    return result


# ---------------------------------------------------------------------------
# GET /agents/smart-grocery/status/{session_id}
# ---------------------------------------------------------------------------

@router.get("/smart-grocery/status/{session_id}")
def smart_grocery_status(
    session_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Check the status of a Smart Grocery session.
    """
    uid = current_user["id"]
    pending = _pending_sessions.get(session_id)

    if not pending:
        raise HTTPException(status_code=404, detail="Session not found or already completed")
    if pending["user_id"] != uid:
        raise HTTPException(status_code=403, detail="Session does not belong to this user")

    state = pending["state"]
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
    }
