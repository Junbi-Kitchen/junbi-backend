import random
import uuid
from typing import Literal, Optional

import psycopg2.extras
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.db import get_db

router = APIRouter(prefix="/pantry", tags=["pantry"])

# Maps frontend category → DB storage_location enum
USDA_SLUG_TO_CATEGORY: dict[str, str] = {
    "dairy-and-egg-products": "dairy",
    "spices-and-herbs": "spices",
    "fats-and-oils": "condiments",
    "poultry-products": "proteins",
    "soups-sauces-and-gravies": "condiments",
    "sausages-and-luncheon-meats": "proteins",
    "breakfast-cereals": "grains",
    "fruits-and-fruit-juices": "produce",
    "pork-products": "proteins",
    "vegetables-and-vegetable-products": "produce",
    "nut-and-seed-products": "proteins",
    "beef-products": "proteins",
    "beverages": "beverages",
    "alcoholic-beverages": "beverages",
    "finfish-and-shellfish-products": "proteins",
    "legumes-and-legume-products": "proteins",
    "lamb-veal-and-game-products": "proteins",
    "baked-products": "bakery",
    "sweets": "pantry",
    "cereal-grains-and-pasta": "grains",
    "fast-foods": "pantry",
    "meals-entrees-and-side-dishes": "pantry",
    "snacks": "pantry",
    "baby-foods": "pantry",
    # already-simplified pass-through
    "produce": "produce",
    "proteins": "proteins",
    "dairy": "dairy",
    "grains": "grains",
    "pantry": "pantry",
    "frozen": "frozen",
    "condiments": "condiments",
    "spices": "spices",
    "bakery": "bakery",
}

CATEGORY_TO_LOCATION: dict[str, str] = {
    "proteins": "fridge",
    "dairy": "fridge",
    "produce": "fridge",
    "beverages": "fridge",
    "frozen": "freezer",
    "pantry": "pantry",
    "spices": "pantry",
    "condiments": "pantry",
    "grains": "pantry",
    "bakery": "pantry",
}

# Fallback when no USDA category is available
LOCATION_TO_CATEGORY: dict[str, str] = {
    "fridge": "produce",
    "freezer": "frozen",
    "pantry": "pantry",
}


class PantryItemBody(BaseModel):
    name: str
    quantity: float
    unit: str
    category: str
    expiryDate: Optional[str] = None
    addedVia: str = "manual"


class UpdatePantryItemBody(BaseModel):
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    category: Optional[str] = None
    expiryDate: Optional[str] = None


class BulkAddBody(BaseModel):
    items: list[PantryItemBody]


class LogActionBody(BaseModel):
    action: Literal["used", "tossed"]
    estimatedValue: float = 0.0


def _resolve_ingredient(cur, name: str) -> str:
    """Upsert ingredient by name and return its UUID."""
    cur.execute(
        "INSERT INTO ingredients (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
    return str(cur.fetchone()["id"])


def _row_to_item(row: dict) -> dict:
    """Convert a pantry_items_with_freshness DB row to the frontend shape."""
    raw = row.get("ic_slug") or LOCATION_TO_CATEGORY.get(str(row.get("location", "pantry")), "pantry")
    category = USDA_SLUG_TO_CATEGORY.get(raw, "pantry")
    expiry = row["expiry_date"]
    return {
        "id": str(row["id"]),
        "name": row["name"] or "",
        "quantity": float(row["quantity"] or 0),
        "unit": row["unit"] or "",
        "category": category,
        "expiryDate": expiry.isoformat() if expiry else None,
        "addedAt": row["created_at"].isoformat().replace("+00:00", "Z"),
        "addedVia": row["added_via"],
        "freshnessStatus": row.get("freshness_status"),
    }


def _get_items(cur, user_id: str) -> list:
    cur.execute("""
        SELECT p.*, p.freshness_status,
               ic.slug AS ic_slug
        FROM pantry_items_with_freshness p
        LEFT JOIN ingredients i ON i.id = p.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE p.user_id = %s AND p.is_active = true
        ORDER BY p.expiry_date ASC NULLS LAST
    """, (user_id,))
    return [_row_to_item(dict(r)) for r in cur.fetchall()]


@router.get("/ingredients/search")
def search_ingredients(
    q: str = "",
    limit: int = 20,
    db=Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list:
    cur = db.cursor()
    cur.execute("""
        SELECT i.name, COALESCE(ic.slug, 'pantry') AS category
        FROM ingredients i
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE i.name ILIKE %s
        ORDER BY i.name
        LIMIT %s
    """, (f"%{q}%", limit))
    return [
        {"name": r["name"], "category": USDA_SLUG_TO_CATEGORY.get(r["category"], "pantry")}
        for r in cur.fetchall()
    ]


@router.get("")
def get_pantry(current_user: dict = Depends(get_current_user), db=Depends(get_db)) -> list:
    return _get_items(db.cursor(), current_user["id"])


@router.post("", status_code=201)
def add_item(
    body: PantryItemBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    ingredient_id = _resolve_ingredient(cur, body.name)
    location = CATEGORY_TO_LOCATION.get(body.category, "pantry")
    cur.execute("""
        INSERT INTO pantry_items
            (user_id, name, ingredient_id, quantity, unit, location, expiry_date, added_via)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        current_user["id"], body.name, ingredient_id,
        body.quantity, body.unit, location,
        body.expiryDate, body.addedVia,
    ))
    new_id = str(cur.fetchone()["id"])
    cur.execute("""
        SELECT p.*, p.freshness_status, ic.slug AS ic_slug
        FROM pantry_items_with_freshness p
        LEFT JOIN ingredients i ON i.id = p.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE p.id = %s
    """, (new_id,))
    return _row_to_item(dict(cur.fetchone()))


@router.patch("/{item_id}")
def update_item(
    item_id: str,
    body: UpdatePantryItemBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    cur.execute(
        "SELECT id FROM pantry_items WHERE id = %s AND user_id = %s AND is_active = true",
        (item_id, current_user["id"]),
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Pantry item not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        # Nothing to change — just return current state
        cur.execute("""
            SELECT p.*, p.freshness_status, ic.slug AS ic_slug
            FROM pantry_items_with_freshness p
            LEFT JOIN ingredients i ON i.id = p.ingredient_id
            LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
            WHERE p.id = %s
        """, (item_id,))
        return _row_to_item(dict(cur.fetchone()))

    set_clauses = []
    params = []

    if "name" in updates:
        ingredient_id = _resolve_ingredient(cur, updates["name"])
        set_clauses += ["name = %s", "ingredient_id = %s"]
        params += [updates["name"], ingredient_id]
    if "quantity" in updates:
        set_clauses.append("quantity = %s")
        params.append(updates["quantity"])
    if "unit" in updates:
        set_clauses.append("unit = %s")
        params.append(updates["unit"])
    if "category" in updates:
        set_clauses.append("location = %s")
        params.append(CATEGORY_TO_LOCATION.get(updates["category"], "pantry"))
    if "expiryDate" in updates:
        set_clauses.append("expiry_date = %s")
        params.append(updates["expiryDate"])

    set_clauses.append("updated_at = now()")
    params += [item_id, current_user["id"]]

    cur.execute(
        f"UPDATE pantry_items SET {', '.join(set_clauses)} WHERE id = %s AND user_id = %s",
        params,
    )
    cur.execute("""
        SELECT p.*, p.freshness_status, ic.slug AS ic_slug
        FROM pantry_items_with_freshness p
        LEFT JOIN ingredients i ON i.id = p.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE p.id = %s
    """, (item_id,))
    return _row_to_item(dict(cur.fetchone()))


@router.delete("/{item_id}", status_code=200)
def delete_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    cur.execute(
        "UPDATE pantry_items SET is_active = false WHERE id = %s AND user_id = %s AND is_active = true RETURNING id",
        (item_id, current_user["id"]),
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Pantry item not found")
    return {"detail": "Deleted"}


@router.post("/{item_id}/log-action", status_code=200)
def log_action(
    item_id: str,
    body: LogActionBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    cur.execute(
        "SELECT id FROM pantry_items WHERE id = %s AND user_id = %s AND is_active = true",
        (item_id, current_user["id"]),
    )
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Pantry item not found")
    cur.execute(
        "SELECT log_pantry_action(%s, %s, %s)",
        (item_id, body.action, body.estimatedValue),
    )
    return {"action": body.action, "estimatedValue": body.estimatedValue}


@router.post("/ocr")
async def ocr_receipt(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    # Stub: return 3-5 random items from mock data
    from app.data.mock_data import MOCK_PANTRY
    import copy
    count = random.randint(3, 5)
    sample = random.sample(MOCK_PANTRY, min(count, len(MOCK_PANTRY)))
    result = []
    for item in sample:
        new = copy.deepcopy(item)
        new["id"] = str(uuid.uuid4())
        new["addedVia"] = "receipt"
        result.append(new)
    return {"items": result}


@router.post("/bulk", status_code=201)
def bulk_add(
    body: BulkAddBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> list:
    cur = db.cursor()
    uid = current_user["id"]
    rows = []
    for item in body.items:
        ingredient_id = _resolve_ingredient(cur, item.name)
        location = CATEGORY_TO_LOCATION.get(item.category, "pantry")
        rows.append((
            uid, item.name, ingredient_id,
            item.quantity, item.unit, location,
            item.expiryDate, item.addedVia,
        ))
    psycopg2.extras.execute_values(cur, """
        INSERT INTO pantry_items
            (user_id, name, ingredient_id, quantity, unit, location, expiry_date, added_via)
        VALUES %s
    """, rows)
    return _get_items(cur, uid)
