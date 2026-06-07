from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.db import get_db
from app.services.ingredient_resolver import run_ingredient_resolver

router = APIRouter(prefix="/grocery", tags=["grocery"])

CATEGORY_TO_AISLE = {
    "produce": "Produce",
    "proteins": "Meat & Seafood",
    "dairy": "Dairy",
    "grains": "Bread & Grains",
    "pantry": "Pantry",
    "frozen": "Frozen",
    "beverages": "Beverages",
    "condiments": "Condiments",
    "spices": "Spices & Herbs",
    "bakery": "Bakery",
}


class GroceryItemBody(BaseModel):
    name: str
    quantity: float
    unit: str
    recipeId: Optional[str] = None
    recipeTitle: Optional[str] = None
    checked: bool = False
    aisle: Optional[str] = None
    category: Optional[str] = None  # frontend may send category instead of aisle


class UpdateGroceryItemBody(BaseModel):
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    checked: Optional[bool] = None
    aisle: Optional[str] = None


class GenerateRequest(BaseModel):
    recipe_ids: list[str]


async def _get_or_create_list(cur, user_id: str) -> str:
    """Return the active grocery list id for the user, creating one if needed."""
    await cur.execute(
        "SELECT id FROM grocery_lists WHERE user_id = %s AND status = 'active' ORDER BY created_at LIMIT 1",
        (user_id,),
    )
    row = await cur.fetchone()
    if row:
        return str(row["id"])
    await cur.execute(
        "INSERT INTO grocery_lists (user_id, name, status) VALUES (%s, 'My List', 'active') RETURNING id",
        (user_id,),
    )
    return str((await cur.fetchone())["id"])


def _row_to_item(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"] or "",
        "quantity": float(row["quantity"] or 0),
        "unit": row["unit"] or "",
        "recipeId": str(row["source_recipe_id"]) if row["source_recipe_id"] else None,
        "recipeTitle": row.get("recipe_title"),
        "checked": row["is_checked"],
        "aisle": row["aisle"] or "Pantry",
    }


async def _get_items(cur, list_id: str) -> tuple[list, list]:
    await cur.execute("""
        SELECT gi.*, r.title AS recipe_title
        FROM grocery_items gi
        LEFT JOIN recipes r ON r.id = gi.source_recipe_id
        WHERE gi.list_id = %s
        ORDER BY gi.sort_order, gi.created_at
    """, (list_id,))
    items = [_row_to_item(dict(r)) for r in await cur.fetchall()]

    await cur.execute("""
        SELECT DISTINCT source_recipe_id::text
        FROM grocery_items
        WHERE list_id = %s AND source_recipe_id IS NOT NULL
    """, (list_id,))
    linked = [r["source_recipe_id"] for r in await cur.fetchall()]
    return items, linked


@router.get("")
async def get_grocery(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    list_id = await _get_or_create_list(cur, current_user["id"])
    items, linked = await _get_items(cur, list_id)
    return {"items": items, "linkedRecipeIds": linked}


@router.post("/items", status_code=201)
async def add_item(
    body: GroceryItemBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    list_id = await _get_or_create_list(cur, current_user["id"])
    ingredient_id = await run_ingredient_resolver(body.name)
    aisle = body.aisle or CATEGORY_TO_AISLE.get(body.category or "", "Pantry")
    await cur.execute("""
        INSERT INTO grocery_items
            (list_id, name, ingredient_id, quantity, unit, is_checked, aisle, source_recipe_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        list_id, body.name, ingredient_id,
        body.quantity, body.unit, body.checked,
        aisle,
        body.recipeId if body.recipeId else None,
    ))
    new_id = str((await cur.fetchone())["id"])
    await cur.execute("""
        SELECT gi.*, r.title AS recipe_title
        FROM grocery_items gi LEFT JOIN recipes r ON r.id = gi.source_recipe_id
        WHERE gi.id = %s
    """, (new_id,))
    return _row_to_item(dict(await cur.fetchone()))


@router.patch("/items/{item_id}")
async def update_item(
    item_id: str,
    body: UpdateGroceryItemBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    list_id = await _get_or_create_list(cur, current_user["id"])
    await cur.execute(
        "SELECT id FROM grocery_items WHERE id = %s AND list_id = %s",
        (item_id, list_id),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Grocery item not found")

    updates = body.model_dump(exclude_none=True)
    field_map = {
        "name": "name", "quantity": "quantity", "unit": "unit",
        "checked": "is_checked", "aisle": "aisle",
    }
    set_clauses, params = [], []
    for fe, db_col in field_map.items():
        if fe in updates:
            set_clauses.append(f"{db_col} = %s")
            params.append(updates[fe])

    if set_clauses:
        params += [item_id, list_id]
        await cur.execute(
            f"UPDATE grocery_items SET {', '.join(set_clauses)} WHERE id = %s AND list_id = %s",
            params,
        )

    await cur.execute("""
        SELECT gi.*, r.title AS recipe_title
        FROM grocery_items gi LEFT JOIN recipes r ON r.id = gi.source_recipe_id
        WHERE gi.id = %s
    """, (item_id,))
    return _row_to_item(dict(await cur.fetchone()))


@router.delete("/items/{item_id}", status_code=200)
async def delete_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    list_id = await _get_or_create_list(cur, current_user["id"])
    await cur.execute(
        "DELETE FROM grocery_items WHERE id = %s AND list_id = %s RETURNING id",
        (item_id, list_id),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Grocery item not found")
    return {"detail": "Deleted"}


@router.delete("/checked", status_code=200)
async def clear_checked(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    list_id = await _get_or_create_list(cur, current_user["id"])
    await cur.execute(
        "DELETE FROM grocery_items WHERE list_id = %s AND is_checked = true",
        (list_id,),
    )
    return {"deleted_count": cur.rowcount}


@router.post("/generate")
async def generate_from_recipes(
    body: GenerateRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    list_id = await _get_or_create_list(cur, uid)

    if not body.recipe_ids:
        items, linked = await _get_items(cur, list_id)
        return {"items": items, "linkedRecipeIds": linked}

    # Fetch recipe ingredients + pantry coverage
    await cur.execute("""
        SELECT
            ri.display_text AS name,
            ri.quantity AS needed_qty,
            ri.unit,
            ri.ingredient_id,
            ri.recipe_id,
            r.title AS recipe_title,
            COALESCE(
                (SELECT SUM(pi.quantity) FROM pantry_items pi
                 WHERE pi.ingredient_id = ri.ingredient_id
                   AND pi.user_id = %s AND pi.is_active = true),
                0
            ) AS pantry_qty,
            COALESCE(ic.slug, 'pantry') AS category
        FROM recipe_ingredients ri
        JOIN recipes r ON r.id = ri.recipe_id
        LEFT JOIN ingredients i ON i.id = ri.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE ri.recipe_id = ANY(%s::uuid[])
          AND ri.ingredient_id IS NOT NULL
    """, (uid, body.recipe_ids))

    rows = await cur.fetchall()

    # Deduplicate by ingredient_id, sum quantities, subtract pantry
    from collections import defaultdict
    aggregated: dict[str, dict] = {}
    for r in rows:
        key = str(r["ingredient_id"])
        needed = float(r["needed_qty"] or 1)
        pantry = float(r["pantry_qty"] or 0)
        shortage = max(0, needed - pantry)
        if shortage <= 0:
            continue
        if key not in aggregated:
            aggregated[key] = {
                "name": r["name"] or "",
                "quantity": shortage,
                "unit": r["unit"] or "",
                "ingredient_id": key,
                "recipe_id": str(r["recipe_id"]),
                "recipe_title": r["recipe_title"],
                "aisle": CATEGORY_TO_AISLE.get(r["category"], "Pantry"),
            }
        else:
            aggregated[key]["quantity"] += shortage

    if aggregated:
        rows_to_insert = [
            (
                list_id, v["name"], v["ingredient_id"],
                round(v["quantity"], 2), v["unit"],
                v["aisle"],
                v["recipe_id"],
            )
            for v in aggregated.values()
        ]
        await cur.executemany("""
            INSERT INTO grocery_items
                (list_id, name, ingredient_id, quantity, unit, aisle, source_recipe_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, rows_to_insert)

    items, linked = await _get_items(cur, list_id)
    return {"items": items, "linkedRecipeIds": linked}
