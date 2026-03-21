from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_current_user
from app.services import data_store

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("/nearby")
def get_nearby_stores(
    address_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
) -> list:
    return sorted(data_store.get_stores(), key=lambda s: s.get("distance", 0))
