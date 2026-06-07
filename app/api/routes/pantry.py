import base64
import json
import logging
from datetime import date, timedelta
from typing import Literal, Optional

import anthropic
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.dependencies import get_current_user
from app.services.receipt_parser import parse_receipt_image
from app.db import get_db
from app.services.ingredient_resolver import run_ingredient_resolver
from config import settings

logger = logging.getLogger(__name__)

_SCAN_SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
# Claude's vision limit is 5 MB for base64-encoded images
_SCAN_MAX_BYTES = 5 * 1024 * 1024

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


async def _get_items(cur, user_id: str) -> list:
    await cur.execute("""
        SELECT p.*, p.freshness_status,
               ic.slug AS ic_slug
        FROM pantry_items_with_freshness p
        LEFT JOIN ingredients i ON i.id = p.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE p.user_id = %s AND p.is_active = true
        ORDER BY p.expiry_date ASC NULLS LAST
    """, (user_id,))
    return [_row_to_item(dict(r)) for r in await cur.fetchall()]


@router.get("/ingredients/search")
async def search_ingredients(
    q: str = "",
    limit: int = 20,
    db=Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list:
    cur = db.cursor()
    await cur.execute("""
        SELECT i.name, COALESCE(ic.slug, 'pantry') AS category
        FROM ingredients i
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE i.name ILIKE %s
        ORDER BY
          CASE WHEN i.name ILIKE %s THEN 0 ELSE 1 END,
          i.name
        LIMIT %s
    """, (f"%{q}%", f"{q}%", limit))
    return [
        {"name": r["name"], "category": USDA_SLUG_TO_CATEGORY.get(r["category"], "pantry")}
        for r in await cur.fetchall()
    ]


@router.get("")
async def get_pantry(current_user: dict = Depends(get_current_user), db=Depends(get_db)) -> list:
    return await _get_items(db.cursor(), current_user["id"])


@router.post("", status_code=201)
async def add_item(
    body: PantryItemBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    ingredient_id = await run_ingredient_resolver(body.name)
    location = CATEGORY_TO_LOCATION.get(body.category, "pantry")
    await cur.execute("""
        INSERT INTO pantry_items
            (user_id, name, ingredient_id, quantity, unit, location, expiry_date, added_via)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        current_user["id"], body.name, ingredient_id,
        body.quantity, body.unit, location,
        body.expiryDate, body.addedVia,
    ))
    new_id = str((await cur.fetchone())["id"])
    await cur.execute("""
        SELECT p.*, p.freshness_status, ic.slug AS ic_slug
        FROM pantry_items_with_freshness p
        LEFT JOIN ingredients i ON i.id = p.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE p.id = %s
    """, (new_id,))
    return _row_to_item(dict(await cur.fetchone()))


@router.patch("/{item_id}")
async def update_item(
    item_id: str,
    body: UpdatePantryItemBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "SELECT id FROM pantry_items WHERE id = %s AND user_id = %s AND is_active = true",
        (item_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Pantry item not found")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        await cur.execute("""
            SELECT p.*, p.freshness_status, ic.slug AS ic_slug
            FROM pantry_items_with_freshness p
            LEFT JOIN ingredients i ON i.id = p.ingredient_id
            LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
            WHERE p.id = %s
        """, (item_id,))
        return _row_to_item(dict(await cur.fetchone()))

    set_clauses = []
    params = []

    if "name" in updates:
        ingredient_id = await run_ingredient_resolver(updates["name"])
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

    await cur.execute(
        f"UPDATE pantry_items SET {', '.join(set_clauses)} WHERE id = %s AND user_id = %s",
        params,
    )
    await cur.execute("""
        SELECT p.*, p.freshness_status, ic.slug AS ic_slug
        FROM pantry_items_with_freshness p
        LEFT JOIN ingredients i ON i.id = p.ingredient_id
        LEFT JOIN ingredient_categories ic ON ic.id = i.category_id
        WHERE p.id = %s
    """, (item_id,))
    return _row_to_item(dict(await cur.fetchone()))


@router.delete("/{item_id}", status_code=200)
async def delete_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "UPDATE pantry_items SET is_active = false WHERE id = %s AND user_id = %s AND is_active = true RETURNING id",
        (item_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Pantry item not found")
    return {"detail": "Deleted"}


@router.post("/{item_id}/log-action", status_code=200)
async def log_action(
    item_id: str,
    body: LogActionBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> dict:
    cur = db.cursor()
    await cur.execute(
        "SELECT id FROM pantry_items WHERE id = %s AND user_id = %s AND is_active = true",
        (item_id, current_user["id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Pantry item not found")
    await cur.execute(
        "SELECT log_pantry_action(%s, %s, %s)",
        (item_id, body.action, body.estimatedValue),
    )
    return {"action": body.action, "estimatedValue": body.estimatedValue}


@router.post("/ocr")
async def ocr_receipt(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    content_type = (image.content_type or "image/jpeg").lower()
    image_bytes = await image.read()
    try:
        return await parse_receipt_image(image_bytes, content_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/scan")
async def scan_pantry(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Accepts a photo of a pantry or fridge and uses Claude vision to detect
    ingredients, estimate quantities, and assess freshness.

    Returns detected items for user review — does NOT write to DB.
    To save confirmed items call POST /pantry/bulk with addedVia='scan'.
    """
    content_type = (image.content_type or "image/jpeg").lower()
    if content_type not in _SCAN_SUPPORTED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image format '{content_type}'. Use JPEG, PNG, or WebP.",
        )

    image_bytes = await image.read()
    if len(image_bytes) > _SCAN_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Image exceeds 5 MB limit.")

    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — returning stub scan results")
        return {"items": _stub_scan_items(), "model": "stub", "count": len(_stub_scan_items())}

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    prompt = """Analyze this image of a pantry or refrigerator and identify all visible food items.

For each item return a JSON object with:
- name: string — simple common ingredient name (e.g. "eggs", "milk", "spinach", "cheddar cheese")
- quantity: number — estimated visible amount
- unit: string — e.g. "count", "oz", "lbs", "bunch", "carton", "bottle", "bag"
- category: string — one of: produce, proteins, dairy, grains, frozen, pantry, condiments, spices, bakery, beverages
- estimatedExpiryDays: number | null — days until likely expiry based on visible condition (null if uncertain)
- freshnessNote: string — brief condition note, e.g. "Looks fresh", "Slightly wilted", "Near expiry — use soon"
- confidence: "high" | "medium" | "low"

Rules:
- Only include items identifiable with at least low confidence
- For partially-used containers, estimate remaining quantity
- For produce, judge freshness from color, texture, and visible wilting or spoilage
- For packaged items, flag if packaging looks damaged or bloated
- Skip non-food items (napkins, containers without visible contents, etc.)

Respond with ONLY a valid JSON array, no explanation."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": content_type, "data": image_b64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        detected = json.loads(raw)
    except Exception as e:
        logger.error("Pantry scan Claude call failed: %s", e)
        raise HTTPException(status_code=502, detail="Image analysis failed. Please try again.")

    today = date.today()
    items = []
    for item in detected:
        expiry_days = item.get("estimatedExpiryDays")
        expiry_date = (today + timedelta(days=int(expiry_days))).isoformat() if expiry_days else None
        category = USDA_SLUG_TO_CATEGORY.get(item.get("category", "pantry"), "pantry")
        items.append({
            "name": item.get("name", ""),
            "quantity": float(item.get("quantity") or 1),
            "unit": item.get("unit", "count"),
            "category": category,
            "expiryDate": expiry_date,
            "addedVia": "scan",
            "freshnessNote": item.get("freshnessNote"),
            "confidence": item.get("confidence", "medium"),
        })

    return {"items": items, "model": "claude-sonnet-4-6", "count": len(items)}


def _stub_scan_items() -> list[dict]:
    """Dev stub returned when ANTHROPIC_API_KEY is not set."""
    today = date.today()
    return [
        {"name": "eggs", "quantity": 6, "unit": "count", "category": "dairy",
         "expiryDate": (today + timedelta(days=14)).isoformat(),
         "addedVia": "scan", "freshnessNote": "Looks fresh", "confidence": "high"},
        {"name": "spinach", "quantity": 1, "unit": "bag", "category": "produce",
         "expiryDate": (today + timedelta(days=3)).isoformat(),
         "addedVia": "scan", "freshnessNote": "Slightly wilted — use soon", "confidence": "high"},
        {"name": "cheddar cheese", "quantity": 8, "unit": "oz", "category": "dairy",
         "expiryDate": (today + timedelta(days=21)).isoformat(),
         "addedVia": "scan", "freshnessNote": "Looks fresh", "confidence": "medium"},
        {"name": "milk", "quantity": 0.5, "unit": "gallon", "category": "dairy",
         "expiryDate": (today + timedelta(days=5)).isoformat(),
         "addedVia": "scan", "freshnessNote": "Near expiry — use soon", "confidence": "high"},
    ]


@router.post("/bulk", status_code=201)
async def bulk_add(
    body: BulkAddBody,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> list:
    cur = db.cursor()
    uid = current_user["id"]
    rows = []
    for item in body.items:
        ingredient_id = await run_ingredient_resolver(item.name)
        location = CATEGORY_TO_LOCATION.get(item.category, "pantry")
        rows.append((
            uid, item.name, ingredient_id,
            item.quantity, item.unit, location,
            item.expiryDate, item.addedVia,
        ))
    await cur.executemany("""
        INSERT INTO pantry_items
            (user_id, name, ingredient_id, quantity, unit, location, expiry_date, added_via)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, rows)
    return await _get_items(cur, uid)
