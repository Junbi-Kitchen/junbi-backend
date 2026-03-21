from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.services import data_store

router = APIRouter(prefix="/collections", tags=["collections"])


class CollectionBody(BaseModel):
    id: Optional[str] = None
    name: str
    description: str = ""
    coverImageUri: str = ""
    emoji: str = "📁"


class UpdateCollectionBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    coverImageUri: Optional[str] = None
    emoji: Optional[str] = None


@router.get("")
def get_collections(current_user: dict = Depends(get_current_user)) -> list:
    return data_store.get_collections(current_user["id"])


@router.post("", status_code=201)
def create_collection(
    body: CollectionBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    return data_store.add_collection(current_user["id"], body.model_dump(exclude_none=True))


@router.patch("/{collection_id}")
def update_collection(
    collection_id: str,
    body: UpdateCollectionBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    updated = data_store.update_collection(
        current_user["id"], collection_id, body.model_dump(exclude_none=True)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Collection not found")
    return updated


@router.delete("/{collection_id}", status_code=200)
def delete_collection(
    collection_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not data_store.delete_collection(current_user["id"], collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"detail": "Deleted"}


@router.post("/{collection_id}/recipes/{recipe_id}", status_code=200)
def add_recipe_to_collection(
    collection_id: str,
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not data_store.add_recipe_to_collection(current_user["id"], collection_id, recipe_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"detail": "Added"}


@router.delete("/{collection_id}/recipes/{recipe_id}", status_code=200)
def remove_recipe_from_collection(
    collection_id: str,
    recipe_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not data_store.remove_recipe_from_collection(current_user["id"], collection_id, recipe_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"detail": "Removed"}
