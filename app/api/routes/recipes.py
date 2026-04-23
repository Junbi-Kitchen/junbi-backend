import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.db import get_db

router = APIRouter(prefix="/recipes", tags=["recipes"])

SOURCE_TYPE_MAP = {
    "instagram": "instagram",
    "tiktok": "tiktok",
    "url": "url",
    "manual": "manual",
}

DIFFICULTY_MAP = {"easy": "easy", "medium": "medium", "hard": "hard"}


class ImportRequest(BaseModel):
    url: str
    source: str


class CreateRecipeRequest(BaseModel):
    title: str
    description: str = ""
    imageUri: str = ""
    blurhash: str = ""
    cookTimeMinutes: int = 30
    difficulty: str = "medium"
    importedFrom: str = "manual"
    source: str = ""
    tags: list[str] = []
    ingredients: list[dict] = []
    steps: list[dict] = []
    nutrition: dict = {}


async def _build_recipe(cur, row: dict) -> dict:
    rid = row["id"]

    await cur.execute("""
        SELECT ri.display_text AS name, ri.quantity, ri.unit,
               COALESCE(ic.slug, '') AS category
        FROM recipe_ingredients ri
        LEFT JOIN ingredients i ON i.id = ri.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE ri.recipe_id = %s
        ORDER BY ri.order_index NULLS LAST
    """, (rid,))
    ingredients = [
        {
            "name": r["name"] or "",
            "quantity": float(r["quantity"] or 0),
            "unit": r["unit"] or "",
            "category": r["category"],
        }
        for r in await cur.fetchall()
    ]

    await cur.execute("""
        SELECT step_number, instruction, duration_minutes AS timer_minutes
        FROM recipe_steps WHERE recipe_id = %s ORDER BY step_number
    """, (rid,))
    steps = [
        {
            "instruction": r["instruction"],
            "timerMinutes": r["timer_minutes"],
        }
        for r in await cur.fetchall()
    ]

    await cur.execute("SELECT tag FROM recipe_tags WHERE recipe_id = %s", (rid,))
    tags = [r["tag"] for r in await cur.fetchall()]

    source = row.get("creator_handle") or row.get("source_url") or ""
    imported_from = row.get("source_type") or "manual"

    saved_at = row.get("saved_at") or row.get("created_at")

    return {
        "id": str(rid),
        "title": row["title"],
        "description": row["description"] or "",
        "imageUri": row["image_url"] or "",
        "blurhash": row["blurhash"] or "",
        "cookTimeMinutes": row["cook_time_mins"] or 30,
        "difficulty": row["difficulty"] or "medium",
        "importedFrom": imported_from,
        "source": source,
        "savedAt": saved_at.isoformat().replace("+00:00", "Z") if saved_at else None,
        "tags": tags,
        "nutrition": {
            "calories": row["calories"] or 0,
            "proteinG": float(row["protein_g"] or 0),
            "carbsG": float(row["carbs_g"] or 0),
            "fatG": float(row["fat_g"] or 0),
        },
        "ingredients": ingredients,
        "steps": steps,
    }


async def _resolve_ingredient(cur, name: str) -> str:
    await cur.execute(
        "INSERT INTO ingredients (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    await cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
    return str((await cur.fetchone())["id"])


async def _insert_recipe(cur, user_id: str, body: CreateRecipeRequest) -> dict:
    difficulty = DIFFICULTY_MAP.get(body.difficulty, "medium")
    source_type = SOURCE_TYPE_MAP.get(body.importedFrom, "manual")
    nutrition = body.nutrition or {}

    await cur.execute("""
        INSERT INTO recipes (
            title, description, image_url, blurhash, cook_time_mins, difficulty,
            source_type, source_url, creator_handle,
            calories, protein_g, carbs_g, fat_g,
            created_by, is_global
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
    """, (
        body.title, body.description, body.imageUri, body.blurhash,
        body.cookTimeMinutes, difficulty,
        source_type,
        body.source if source_type == "url" else None,
        body.source if source_type in ("instagram", "tiktok") else None,
        nutrition.get("calories"), nutrition.get("proteinG"),
        nutrition.get("carbsG"), nutrition.get("fatG"),
        user_id, False,
    ))
    row = dict(await cur.fetchone())
    rid = row["id"]

    for idx, ing in enumerate(body.ingredients):
        name = ing.get("name", "")
        if not name:
            continue
        ingredient_id = await _resolve_ingredient(cur, name)
        await cur.execute("""
            INSERT INTO recipe_ingredients
                (recipe_id, ingredient_id, quantity, unit, display_text, order_index)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (rid, ingredient_id, ing.get("quantity"), ing.get("unit"), name, idx))

    for step in body.steps:
        await cur.execute("""
            INSERT INTO recipe_steps (recipe_id, step_number, instruction, duration_minutes)
            VALUES (%s, %s, %s, %s)
        """, (rid, step.get("stepNumber", step.get("step_number", 1)),
              step.get("instruction", ""), step.get("timerMinutes")))

    for tag in body.tags:
        await cur.execute("""
            INSERT INTO recipe_tags (recipe_id, tag) VALUES (%s, %s)
            ON CONFLICT (recipe_id, tag) DO NOTHING
        """, (rid, tag))

    # Auto-save for the creator
    await cur.execute("""
        INSERT INTO user_recipe_interactions (user_id, recipe_id, action)
        VALUES (%s, %s, 'saved')
        ON CONFLICT (user_id, recipe_id, action) DO NOTHING
    """, (user_id, rid))

    return await _build_recipe(cur, row)


@router.get("/feed")
async def get_feed(
    tags: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, le=50),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]
    tag_list = [t.strip() for t in tags.split(",")] if tags else []

    # Recipes the user has already interacted with (saved or skipped)
    await cur.execute("""
        SELECT recipe_id FROM user_recipe_interactions
        WHERE user_id = %s AND action IN ('saved', 'skipped')
    """, (uid,))
    excluded = {str(r["recipe_id"]) for r in await cur.fetchall()}

    if cursor:
        await cur.execute("""
            SELECT r.* FROM recipes r
            WHERE (r.is_global = true OR r.created_by = %s)
              AND r.created_at < %s::timestamptz
            ORDER BY r.created_at DESC
            LIMIT %s
        """, (uid, cursor, limit + 1))
    else:
        await cur.execute("""
            SELECT r.* FROM recipes r
            WHERE (r.is_global = true OR r.created_by = %s)
            ORDER BY r.created_at DESC
            LIMIT %s
        """, (uid, limit + 1))

    rows = [dict(r) for r in await cur.fetchall()]

    # Filter excluded + tags in Python (simpler than complex SQL for now)
    filtered = [r for r in rows if str(r["id"]) not in excluded]
    if tag_list:
        result = []
        for row in filtered:
            await cur.execute("SELECT tag FROM recipe_tags WHERE recipe_id = %s", (row["id"],))
            recipe_tags = {r["tag"] for r in await cur.fetchall()}
            if any(t in recipe_tags for t in tag_list):
                result.append(row)
        filtered = result

    page = filtered[:limit]
    has_more = len(filtered) > limit
    next_cursor = page[-1]["created_at"].isoformat() if has_more and page else None

    recipes = []
    for r in page:
        recipes.append(await _build_recipe(cur, r))

    return {"recipes": recipes, "next_cursor": next_cursor}


@router.get("/saved")
async def get_saved(
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, le=50),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    uid = current_user["id"]

    if cursor:
        await cur.execute("""
            SELECT r.*, uri.created_at AS saved_at
            FROM recipes r
            JOIN user_recipe_interactions uri ON uri.recipe_id = r.id
            WHERE uri.user_id = %s AND uri.action = 'saved'
              AND uri.created_at < %s::timestamptz
            ORDER BY uri.created_at DESC
            LIMIT %s
        """, (uid, cursor, limit + 1))
    else:
        await cur.execute("""
            SELECT r.*, uri.created_at AS saved_at
            FROM recipes r
            JOIN user_recipe_interactions uri ON uri.recipe_id = r.id
            WHERE uri.user_id = %s AND uri.action = 'saved'
            ORDER BY uri.created_at DESC
            LIMIT %s
        """, (uid, limit + 1))

    rows = [dict(r) for r in await cur.fetchall()]
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1]["saved_at"].isoformat() if has_more and page else None

    recipes = []
    for r in page:
        recipes.append(await _build_recipe(cur, r))

    return {"recipes": recipes, "next_cursor": next_cursor}


@router.get("/{recipe_id}")
async def get_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute("SELECT * FROM recipes WHERE id = %s::uuid", (recipe_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return await _build_recipe(cur, dict(row))


@router.post("/import")
async def import_recipe(
    body: ImportRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    create_body = CreateRecipeRequest(
        title="Imported Recipe",
        imageUri="",
        importedFrom=body.source,
        source=body.url,
    )
    cur = db.cursor()
    return await _insert_recipe(cur, current_user["id"], create_body)


@router.post("")
async def create_recipe(
    body: CreateRecipeRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    return await _insert_recipe(db.cursor(), current_user["id"], body)


@router.post("/saved/{recipe_id}", status_code=200)
async def save_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute("SELECT id FROM recipes WHERE id = %s::uuid", (recipe_id,))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Recipe not found")
    await cur.execute("""
        INSERT INTO user_recipe_interactions (user_id, recipe_id, action)
        VALUES (%s, %s::uuid, 'saved')
        ON CONFLICT (user_id, recipe_id, action) DO NOTHING
    """, (current_user["id"], recipe_id))
    return {"detail": "Saved"}


@router.delete("/saved/{recipe_id}", status_code=200)
async def unsave_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute("""
        DELETE FROM user_recipe_interactions
        WHERE user_id = %s AND recipe_id = %s::uuid AND action = 'saved'
    """, (current_user["id"], recipe_id))
    return {"detail": "Removed"}


@router.post("/skip/{recipe_id}", status_code=200)
async def skip_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute("""
        INSERT INTO user_recipe_interactions (user_id, recipe_id, action)
        VALUES (%s, %s::uuid, 'skipped')
        ON CONFLICT (user_id, recipe_id, action) DO NOTHING
    """, (current_user["id"], recipe_id))
    return {"detail": "Skipped"}
