from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.db import get_db

router = APIRouter(prefix="/collections", tags=["collections"])


class CollectionBody(BaseModel):
    name: str
    description: str = ""
    coverImageUri: str = ""
    emoji: str = "📁"


class UpdateCollectionBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    coverImageUri: Optional[str] = None
    emoji: Optional[str] = None


async def _row_to_collection(cur, row: dict) -> dict:
    await cur.execute(
        "SELECT recipe_id FROM collection_recipes WHERE collection_id = %s ORDER BY added_at",
        (row["id"],),
    )
    recipe_ids = [str(r["recipe_id"]) for r in await cur.fetchall()]
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"] or "",
        "coverImageUri": row["cover_photo_url"] or "",
        "emoji": row["emoji"] or "📁",
        "recipeIds": recipe_ids,
        "createdAt": row["created_at"].isoformat().replace("+00:00", "Z"),
    }


@router.get("")
async def get_collections(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> list:
    cur = db.cursor()
    await cur.execute(
        "SELECT * FROM collections WHERE user_id = %s ORDER BY sort_order, created_at",
        (current_user["id"],),
    )
    rows = await cur.fetchall()
    result = []
    for r in rows:
        result.append(await _row_to_collection(cur, dict(r)))
    return result


@router.post("", status_code=201)
async def create_collection(
    body: CollectionBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute("""
        INSERT INTO collections (user_id, name, description, cover_photo_url, emoji)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
    """, (current_user["id"], body.name, body.description, body.coverImageUri, body.emoji))
    return await _row_to_collection(cur, dict(await cur.fetchone()))


@router.patch("/{collection_id}")
async def update_collection(
    collection_id: str,
    body: UpdateCollectionBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "SELECT id FROM collections WHERE id = %s AND user_id = %s",
        (collection_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Collection not found")

    updates = body.model_dump(exclude_none=True)
    field_map = {"name": "name", "description": "description", "coverImageUri": "cover_photo_url", "emoji": "emoji"}
    set_clauses, params = [], []
    for fe_field, db_field in field_map.items():
        if fe_field in updates:
            set_clauses.append(f"{db_field} = %s")
            params.append(updates[fe_field])

    if set_clauses:
        params += [collection_id, current_user["id"]]
        await cur.execute(
            f"UPDATE collections SET {', '.join(set_clauses)} WHERE id = %s AND user_id = %s",
            params,
        )

    await cur.execute("SELECT * FROM collections WHERE id = %s", (collection_id,))
    return await _row_to_collection(cur, dict(await cur.fetchone()))


@router.delete("/{collection_id}", status_code=200)
async def delete_collection(
    collection_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "DELETE FROM collections WHERE id = %s AND user_id = %s RETURNING id",
        (collection_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"detail": "Deleted"}


@router.post("/{collection_id}/recipes/{recipe_id}", status_code=200)
async def add_recipe_to_collection(
    collection_id: str,
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "SELECT id FROM collections WHERE id = %s AND user_id = %s",
        (collection_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Collection not found")
    await cur.execute("""
        INSERT INTO collection_recipes (collection_id, recipe_id)
        VALUES (%s::uuid, %s::uuid)
        ON CONFLICT (collection_id, recipe_id) DO NOTHING
    """, (collection_id, recipe_id))
    return {"detail": "Added"}


@router.delete("/{collection_id}/recipes/{recipe_id}", status_code=200)
async def remove_recipe_from_collection(
    collection_id: str,
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "SELECT id FROM collections WHERE id = %s AND user_id = %s",
        (collection_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Collection not found")
    await cur.execute(
        "DELETE FROM collection_recipes WHERE collection_id = %s AND recipe_id = %s::uuid",
        (collection_id, recipe_id),
    )
    return {"detail": "Removed"}
