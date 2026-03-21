from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.services import data_store

router = APIRouter(prefix="/grocery", tags=["grocery"])


class GroceryItemBody(BaseModel):
    id: Optional[str] = None
    name: str
    quantity: float
    unit: str
    recipeId: Optional[str] = None
    recipeTitle: Optional[str] = None
    checked: bool = False
    aisle: str = "Pantry"


class UpdateGroceryItemBody(BaseModel):
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    checked: Optional[bool] = None
    aisle: Optional[str] = None


class GenerateRequest(BaseModel):
    recipe_ids: list[str]


@router.get("")
def get_grocery(current_user: dict = Depends(get_current_user)) -> dict:
    items, linked = data_store.get_grocery(current_user["id"])
    return {"items": items, "linked_recipe_ids": linked}


@router.post("/items", status_code=201)
def add_item(
    body: GroceryItemBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    return data_store.add_grocery_item(current_user["id"], body.model_dump(exclude_none=True))


@router.patch("/items/{item_id}")
def update_item(
    item_id: str,
    body: UpdateGroceryItemBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    updated = data_store.update_grocery_item(
        current_user["id"], item_id, body.model_dump(exclude_none=True)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Grocery item not found")
    return updated


@router.delete("/items/{item_id}", status_code=200)
def delete_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not data_store.delete_grocery_item(current_user["id"], item_id):
        raise HTTPException(status_code=404, detail="Grocery item not found")
    return {"detail": "Deleted"}


@router.delete("/checked", status_code=200)
def clear_checked(current_user: dict = Depends(get_current_user)) -> dict:
    count = data_store.clear_checked_grocery_items(current_user["id"])
    return {"deleted_count": count}


@router.post("/generate")
def generate_from_recipes(
    body: GenerateRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    items, linked = data_store.generate_grocery_from_recipes(current_user["id"], body.recipe_ids)
    return {"items": items, "linked_recipe_ids": linked}
