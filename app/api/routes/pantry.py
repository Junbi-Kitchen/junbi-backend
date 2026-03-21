import random
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.services import data_store

router = APIRouter(prefix="/pantry", tags=["pantry"])


class PantryItemBody(BaseModel):
    id: Optional[str] = None
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


@router.get("")
def get_pantry(current_user: dict = Depends(get_current_user)) -> list:
    return data_store.get_pantry(current_user["id"])


@router.post("", status_code=201)
def add_item(
    body: PantryItemBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    return data_store.add_pantry_item(current_user["id"], body.model_dump(exclude_none=True))


@router.patch("/{item_id}")
def update_item(
    item_id: str,
    body: UpdatePantryItemBody,
    current_user: dict = Depends(get_current_user),
) -> dict:
    updated = data_store.update_pantry_item(
        current_user["id"], item_id, body.model_dump(exclude_none=True)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Pantry item not found")
    return updated


@router.delete("/{item_id}", status_code=200)
def delete_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not data_store.delete_pantry_item(current_user["id"], item_id):
        raise HTTPException(status_code=404, detail="Pantry item not found")
    return {"detail": "Deleted"}


@router.post("/ocr")
async def ocr_receipt(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> list:
    # Stub: return 3-5 random items from the global pantry mock
    from app.data.mock_data import MOCK_PANTRY
    import copy, uuid
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
) -> list:
    items = [i.model_dump(exclude_none=True) for i in body.items]
    return data_store.bulk_add_pantry_items(current_user["id"], items)
