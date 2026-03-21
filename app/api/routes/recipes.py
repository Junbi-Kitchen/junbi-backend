import copy
import random
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.services import data_store

router = APIRouter(prefix="/recipes", tags=["recipes"])


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ImportRequest(BaseModel):
    url: str
    source: str


class CreateRecipeRequest(BaseModel):
    title: str
    description: str
    imageUri: str
    blurhash: str
    cookTimeMinutes: int
    difficulty: str
    importedFrom: str
    source: str
    tags: list[str]
    ingredients: list[dict]
    steps: list[dict]
    nutrition: dict


@router.get("/feed")
def get_feed(
    tags: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    catalog = data_store.get_catalog()
    skipped = data_store.get_skipped_ids(current_user["id"])
    saved = {r["id"] for r in data_store.get_saved_recipes(current_user["id"])}

    # Filter by tags if provided
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    filtered = [
        r for r in catalog
        if r["id"] not in skipped
        and r["id"] not in saved
        and (not tag_list or any(t in r.get("tags", []) for t in tag_list))
    ]

    # Cursor pagination by index
    start = 0
    if cursor:
        ids = [r["id"] for r in filtered]
        if cursor in ids:
            start = ids.index(cursor) + 1

    page = filtered[start: start + limit]
    next_cursor = page[-1]["id"] if len(page) == limit and start + limit < len(filtered) else None
    return {"recipes": page, "next_cursor": next_cursor}


@router.get("/saved")
def get_saved(
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    saved = data_store.get_saved_recipes(current_user["id"])
    start = 0
    if cursor:
        ids = [r["id"] for r in saved]
        if cursor in ids:
            start = ids.index(cursor) + 1
    page = saved[start: start + limit]
    next_cursor = page[-1]["id"] if len(page) == limit and start + limit < len(saved) else None
    return {"recipes": page, "next_cursor": next_cursor}


@router.get("/{recipe_id}")
def get_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    recipe = data_store.get_catalog_recipe(recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@router.post("/import")
def import_recipe(
    body: ImportRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    # Stub: return a random catalog recipe with a new ID and source set
    base = copy.deepcopy(random.choice(data_store.get_catalog()))
    base["id"] = str(uuid.uuid4())
    base["importedFrom"] = body.source
    base["source"] = body.url
    base["savedAt"] = _iso_now()
    data_store.add_to_catalog(base)
    data_store.save_recipe(current_user["id"], base["id"])
    return base


@router.post("")
def create_recipe(
    body: CreateRecipeRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    recipe = body.model_dump()
    recipe["id"] = str(uuid.uuid4())
    recipe["savedAt"] = _iso_now()
    data_store.add_to_catalog(recipe)
    data_store.save_recipe(current_user["id"], recipe["id"])
    return recipe


@router.post("/saved/{recipe_id}", status_code=200)
def save_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not data_store.get_catalog_recipe(recipe_id):
        raise HTTPException(status_code=404, detail="Recipe not found")
    data_store.save_recipe(current_user["id"], recipe_id)
    return {"detail": "Saved"}


@router.delete("/saved/{recipe_id}", status_code=200)
def unsave_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    data_store.unsave_recipe(current_user["id"], recipe_id)
    return {"detail": "Removed"}


@router.post("/skip/{recipe_id}", status_code=200)
def skip_recipe(
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    data_store.skip_recipe(current_user["id"], recipe_id)
    return {"detail": "Skipped"}
