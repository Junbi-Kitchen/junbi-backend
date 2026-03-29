from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import get_current_user
from app.db import get_db

router = APIRouter(prefix="/stores", tags=["stores"])


def _row_to_store(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "logo": row["logo"],
        "distance": 0.0,  # no geolocation yet
        "supportsPickup": row["supports_pickup"],
        "isInstacart": row["is_instacart"],
    }


@router.get("/nearby")
def get_nearby_stores(
    address_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> list:
    cur = db.cursor()
    cur.execute("SELECT * FROM stores WHERE supports_pickup = true ORDER BY name")
    return [_row_to_store(dict(r)) for r in cur.fetchall()]
