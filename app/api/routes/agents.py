"""
Agent API routes — Smart Grocery (LangGraph orchestrator).

  POST /agents/smart-grocery/start
    → Runs the graph up to its human_checkpoint interrupt (load context →
      analyze → Kroger search → build cart).
    → Returns cart preview for user review.

  POST /agents/smart-grocery/confirm
    → Resumes the graph past human_checkpoint to confirm or cancel the cart.
    → confirmed=true  → adds to Kroger cart + writes to DB → returns checkout URL.
    → confirmed=false → routes to the cancelled terminal node → returns cancelled status.

  GET  /agents/smart-grocery/status/{session_id}
    → Returns current session state (awaiting_confirmation / order_placed / cancelled).
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.agents.smart_grocery_agent.runner import (
    start_grocery_agent,
    confirm_grocery_order,
    get_session_status,
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
async def smart_grocery_status(
    session_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Check the status of a Smart Grocery session.
    """
    uid = current_user["id"]
    try:
        return await get_session_status(session_id, uid)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    except ValueError:
        raise HTTPException(status_code=404, detail="Session not found or already completed")
