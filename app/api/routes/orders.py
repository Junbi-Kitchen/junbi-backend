from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.services import data_store

router = APIRouter(prefix="/orders", tags=["orders"])

VALID_STATUSES = {"placed", "preparing", "ready", "picked_up", "cancelled"}


class PlaceOrderRequest(BaseModel):
    store_id: str
    items: list[dict]
    address_id: Optional[str] = None


class UpdateStatusRequest(BaseModel):
    status: str


@router.get("/active")
def get_active_order(current_user: dict = Depends(get_current_user)) -> Optional[dict]:
    return data_store.get_active_order(current_user["id"])


@router.get("/history")
def get_order_history(
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    orders = data_store.get_order_history(current_user["id"])
    start = 0
    if cursor:
        ids = [o["id"] for o in orders]
        if cursor in ids:
            start = ids.index(cursor) + 1
    page = orders[start: start + limit]
    next_cursor = page[-1]["id"] if len(page) == limit and start + limit < len(orders) else None
    return {"orders": page, "next_cursor": next_cursor}


@router.post("")
def place_order(
    body: PlaceOrderRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    stores = data_store.get_stores()
    store = next((s for s in stores if s["id"] == body.store_id), None)
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    subtotal = round(len(body.items) * 4.99, 2)
    return data_store.create_order(current_user["id"], store, body.items, subtotal)


@router.get("/{order_id}")
def get_order(
    order_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    order = data_store.get_order_by_id(current_user["id"], order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.patch("/{order_id}/status")
def update_order_status(
    order_id: str,
    body: UpdateStatusRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID_STATUSES}")
    order = data_store.update_order_status(current_user["id"], order_id, body.status)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
