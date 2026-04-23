from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.db import get_db

router = APIRouter(prefix="/orders", tags=["orders"])

VALID_STATUSES = {"placed", "preparing", "ready", "picked_up", "cancelled"}


class PlaceOrderRequest(BaseModel):
    store_id: str
    items: list[dict]
    address_id: Optional[str] = None


class UpdateStatusRequest(BaseModel):
    status: str


def _row_to_store(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "logo": row["logo"],
        "distance": 0.0,
        "supportsPickup": row["supports_pickup"],
        "isInstacart": row["is_instacart"],
    }


async def _build_order(cur, row: dict) -> dict:
    oid = row["id"]

    await cur.execute("SELECT * FROM stores WHERE id = %s", (row["store_id"],))
    store_row = await cur.fetchone()
    store = _row_to_store(dict(store_row)) if store_row else {}

    await cur.execute("""
        SELECT oi.quantity, oi.unit, oi.price, i.name
        FROM order_items oi
        LEFT JOIN ingredients i ON i.id = oi.ingredient_id
        WHERE oi.order_id = %s
    """, (oid,))
    items = [
        {
            "name": r["name"] or "",
            "quantity": float(r["quantity"] or 0),
            "unit": r["unit"] or "",
            "price": float(r["price"] or 0),
        }
        for r in await cur.fetchall()
    ]

    placed_at = row["placed_at"]
    estimated_pickup = (placed_at + timedelta(hours=2)).isoformat().replace("+00:00", "Z") if placed_at else None

    return {
        "id": str(oid),
        "store": store,
        "items": items,
        "status": row["status"],
        "subtotal": float(row["total"] or 0),
        "estimatedPickup": estimated_pickup,
        "placedAt": placed_at.isoformat().replace("+00:00", "Z") if placed_at else None,
    }


async def _resolve_ingredient(cur, name: str) -> str:
    await cur.execute(
        "INSERT INTO ingredients (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    await cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
    return str((await cur.fetchone())["id"])


@router.get("/active")
async def get_active_order(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> Optional[dict]:
    cur = db.cursor()
    await cur.execute("""
        SELECT * FROM orders
        WHERE user_id = %s AND status IN ('placed', 'preparing', 'ready')
        ORDER BY placed_at DESC
        LIMIT 1
    """, (current_user["id"],))
    row = await cur.fetchone()
    if not row:
        return None
    return await _build_order(cur, dict(row))


@router.get("/history")
async def get_order_history(
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, le=50),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    if cursor:
        await cur.execute("""
            SELECT * FROM orders
            WHERE user_id = %s AND status IN ('picked_up', 'cancelled')
              AND placed_at < %s::timestamptz
            ORDER BY placed_at DESC
            LIMIT %s
        """, (uid, cursor, limit + 1))
    else:
        await cur.execute("""
            SELECT * FROM orders
            WHERE user_id = %s AND status IN ('picked_up', 'cancelled')
            ORDER BY placed_at DESC
            LIMIT %s
        """, (uid, limit + 1))

    rows = [dict(r) for r in await cur.fetchall()]
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1]["placed_at"].isoformat() if has_more and page else None

    orders = []
    for r in page:
        orders.append(await _build_order(cur, r))

    return {"orders": orders, "next_cursor": next_cursor}


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "SELECT * FROM orders WHERE id = %s::uuid AND user_id = %s",
        (order_id, current_user["id"]),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return await _build_order(cur, dict(row))


@router.post("")
async def place_order(
    body: PlaceOrderRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute("SELECT * FROM stores WHERE id = %s::uuid", (body.store_id,))
    store_row = await cur.fetchone()
    if not store_row:
        raise HTTPException(status_code=404, detail="Store not found")

    subtotal = round(len(body.items) * 4.99, 2)
    await cur.execute("""
        INSERT INTO orders (user_id, store_id, address_id, status, total)
        VALUES (%s, %s::uuid, %s, 'placed', %s)
        RETURNING *
    """, (
        current_user["id"],
        body.store_id,
        body.address_id if body.address_id else None,
        subtotal,
    ))
    order_row = dict(await cur.fetchone())
    oid = order_row["id"]

    # Insert order items, resolving ingredient names
    item_rows = []
    for item in body.items:
        name = item.get("name", "")
        if name:
            ingredient_id = await _resolve_ingredient(cur, name)
            item_rows.append((
                oid, ingredient_id,
                item.get("quantity", 1), item.get("unit", ""),
                round(4.99, 2),
            ))

    if item_rows:
        await cur.executemany("""
            INSERT INTO order_items (order_id, ingredient_id, quantity, unit, price)
            VALUES (%s, %s, %s, %s, %s)
        """, item_rows)

    return await _build_order(cur, order_row)


@router.patch("/{order_id}/status")
async def update_order_status(
    order_id: str,
    body: UpdateStatusRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    if body.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {VALID_STATUSES}",
        )
    cur = db.cursor()
    await cur.execute("""
        UPDATE orders SET status = %s, updated_at = now()
        WHERE id = %s::uuid AND user_id = %s
        RETURNING *
    """, (body.status, order_id, current_user["id"]))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return await _build_order(cur, dict(row))
