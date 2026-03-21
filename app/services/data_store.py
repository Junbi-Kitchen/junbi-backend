"""Per-user in-memory data store. Seeded from mock data on user creation."""
import copy
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.data.mock_data import (
    DEMO_USER_ID,
    MOCK_PANTRY,
    MOCK_GROCERY_LIST,
    MOCK_COLLECTIONS,
    MOCK_RECIPES,
    MOCK_STORES,
    get_mock_order,
)

# Global recipe catalog — all users share this
_catalog: list[dict] = copy.deepcopy(MOCK_RECIPES)

# Global store catalog
_stores: list[dict] = copy.deepcopy(MOCK_STORES)

# Per-user data keyed by user_id
_pantry: dict[str, list[dict]] = {}
_grocery: dict[str, list[dict]] = {}
_grocery_linked_recipes: dict[str, list[str]] = {}
_saved_recipe_ids: dict[str, list[str]] = {}
_skipped_recipe_ids: dict[str, set[str]] = {}
_collections: dict[str, list[dict]] = {}
_orders: dict[str, list[dict]] = {}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_user_data(user_id: str) -> None:
    """Called on registration. Gives every new user realistic starting data."""
    _pantry[user_id] = copy.deepcopy(MOCK_PANTRY)
    _grocery[user_id] = copy.deepcopy(MOCK_GROCERY_LIST)
    _grocery_linked_recipes[user_id] = ["r1", "r3", "r4", "r7", "r9"]
    _saved_recipe_ids[user_id] = [r["id"] for r in MOCK_RECIPES[:5]]
    _skipped_recipe_ids[user_id] = set()
    _collections[user_id] = copy.deepcopy(MOCK_COLLECTIONS)
    _orders[user_id] = [get_mock_order(user_id)]


# Seed demo user
seed_user_data(DEMO_USER_ID)


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def get_catalog() -> list[dict]:
    return _catalog


def get_catalog_recipe(recipe_id: str) -> Optional[dict]:
    return next((r for r in _catalog if r["id"] == recipe_id), None)


def add_to_catalog(recipe: dict) -> None:
    _catalog.append(recipe)


def get_stores() -> list[dict]:
    return _stores


# ---------------------------------------------------------------------------
# Pantry
# ---------------------------------------------------------------------------

def get_pantry(user_id: str) -> list[dict]:
    return _pantry.setdefault(user_id, [])


def add_pantry_item(user_id: str, item: dict) -> dict:
    item = {**item, "id": item.get("id") or str(uuid.uuid4()), "addedAt": _iso_now()}
    _pantry.setdefault(user_id, []).append(item)
    return item


def update_pantry_item(user_id: str, item_id: str, updates: dict) -> Optional[dict]:
    items = _pantry.get(user_id, [])
    for i, item in enumerate(items):
        if item["id"] == item_id:
            items[i] = {**item, **updates}
            return items[i]
    return None


def delete_pantry_item(user_id: str, item_id: str) -> bool:
    items = _pantry.get(user_id, [])
    before = len(items)
    _pantry[user_id] = [i for i in items if i["id"] != item_id]
    return len(_pantry[user_id]) < before


def bulk_add_pantry_items(user_id: str, items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        result.append(add_pantry_item(user_id, item))
    return result


# ---------------------------------------------------------------------------
# Grocery
# ---------------------------------------------------------------------------

def get_grocery(user_id: str) -> tuple[list[dict], list[str]]:
    items = _grocery.setdefault(user_id, [])
    linked = _grocery_linked_recipes.get(user_id, [])
    return items, linked


def add_grocery_item(user_id: str, item: dict) -> dict:
    item = {**item, "id": item.get("id") or str(uuid.uuid4())}
    _grocery.setdefault(user_id, []).append(item)
    return item


def update_grocery_item(user_id: str, item_id: str, updates: dict) -> Optional[dict]:
    items = _grocery.get(user_id, [])
    for i, item in enumerate(items):
        if item["id"] == item_id:
            items[i] = {**item, **updates}
            return items[i]
    return None


def delete_grocery_item(user_id: str, item_id: str) -> bool:
    items = _grocery.get(user_id, [])
    before = len(items)
    _grocery[user_id] = [i for i in items if i["id"] != item_id]
    return len(_grocery[user_id]) < before


def clear_checked_grocery_items(user_id: str) -> int:
    items = _grocery.get(user_id, [])
    checked = [i for i in items if i.get("checked")]
    _grocery[user_id] = [i for i in items if not i.get("checked")]
    return len(checked)


def generate_grocery_from_recipes(user_id: str, recipe_ids: list[str]) -> tuple[list[dict], list[str]]:
    """Generate grocery items from recipes, subtracting items already in pantry."""
    pantry_items = _pantry.get(user_id, [])
    pantry_map: dict[str, float] = {}
    for pi in pantry_items:
        pantry_map[pi["name"].lower()] = pi.get("quantity", 0)

    generated: dict[str, dict] = {}
    for recipe_id in recipe_ids:
        recipe = get_catalog_recipe(recipe_id)
        if not recipe:
            continue
        for ing in recipe.get("ingredients", []):
            key = ing["name"].lower()
            needed = ing.get("quantity", 1)
            have = pantry_map.get(key, 0)
            short = max(0, needed - have)
            if short <= 0:
                continue
            aisle = _category_to_aisle(ing.get("category", "pantry"))
            if key in generated:
                generated[key]["quantity"] += short
            else:
                generated[key] = {
                    "id": str(uuid.uuid4()),
                    "name": ing["name"],
                    "quantity": short,
                    "unit": ing.get("unit", ""),
                    "recipeId": recipe_id,
                    "recipeTitle": recipe.get("title", ""),
                    "checked": False,
                    "aisle": aisle,
                }

    new_items = list(generated.values())
    _grocery.setdefault(user_id, [])
    existing_names = {i["name"].lower() for i in _grocery[user_id]}
    for item in new_items:
        if item["name"].lower() not in existing_names:
            _grocery[user_id].append(item)

    linked = list(set(_grocery_linked_recipes.get(user_id, []) + recipe_ids))
    _grocery_linked_recipes[user_id] = linked

    return _grocery[user_id], linked


def _category_to_aisle(category: str) -> str:
    mapping = {
        "produce": "Produce",
        "proteins": "Meat & Seafood",
        "dairy": "Dairy & Eggs",
        "grains": "Grains & Pasta",
        "pantry": "Pantry",
        "frozen": "Frozen",
        "beverages": "Beverages",
        "condiments": "Condiments & Sauces",
        "spices": "Spices & Seasonings",
        "bakery": "Bakery",
    }
    return mapping.get(category, "Pantry")


# ---------------------------------------------------------------------------
# Saved recipes
# ---------------------------------------------------------------------------

def get_saved_recipes(user_id: str) -> list[dict]:
    ids = _saved_recipe_ids.get(user_id, [])
    return [r for r in _catalog if r["id"] in ids]


def save_recipe(user_id: str, recipe_id: str) -> None:
    ids = _saved_recipe_ids.setdefault(user_id, [])
    if recipe_id not in ids:
        ids.append(recipe_id)


def unsave_recipe(user_id: str, recipe_id: str) -> None:
    ids = _saved_recipe_ids.get(user_id, [])
    _saved_recipe_ids[user_id] = [i for i in ids if i != recipe_id]


def skip_recipe(user_id: str, recipe_id: str) -> None:
    _skipped_recipe_ids.setdefault(user_id, set()).add(recipe_id)


def get_skipped_ids(user_id: str) -> set[str]:
    return _skipped_recipe_ids.get(user_id, set())


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def get_collections(user_id: str) -> list[dict]:
    return _collections.setdefault(user_id, [])


def add_collection(user_id: str, collection: dict) -> dict:
    collection = {**collection, "id": collection.get("id") or str(uuid.uuid4()), "createdAt": _iso_now(), "recipeIds": []}
    _collections.setdefault(user_id, []).append(collection)
    return collection


def update_collection(user_id: str, col_id: str, updates: dict) -> Optional[dict]:
    cols = _collections.get(user_id, [])
    for i, col in enumerate(cols):
        if col["id"] == col_id:
            cols[i] = {**col, **{k: v for k, v in updates.items() if k not in ("id", "createdAt")}}
            return cols[i]
    return None


def delete_collection(user_id: str, col_id: str) -> bool:
    cols = _collections.get(user_id, [])
    before = len(cols)
    _collections[user_id] = [c for c in cols if c["id"] != col_id]
    return len(_collections[user_id]) < before


def add_recipe_to_collection(user_id: str, col_id: str, recipe_id: str) -> bool:
    for col in _collections.get(user_id, []):
        if col["id"] == col_id:
            if recipe_id not in col["recipeIds"]:
                col["recipeIds"].append(recipe_id)
            return True
    return False


def remove_recipe_from_collection(user_id: str, col_id: str, recipe_id: str) -> bool:
    for col in _collections.get(user_id, []):
        if col["id"] == col_id:
            col["recipeIds"] = [r for r in col["recipeIds"] if r != recipe_id]
            return True
    return False


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def get_active_order(user_id: str) -> Optional[dict]:
    active_statuses = {"placed", "preparing", "ready"}
    orders = _orders.get(user_id, [])
    for order in reversed(orders):
        if order.get("status") in active_statuses:
            return order
    return None


def get_order_history(user_id: str) -> list[dict]:
    done_statuses = {"picked_up", "cancelled"}
    return [o for o in _orders.get(user_id, []) if o.get("status") in done_statuses]


def create_order(user_id: str, store: dict, items: list[dict], subtotal: float) -> dict:
    now = datetime.now(timezone.utc)
    order = {
        "id": str(uuid.uuid4()),
        "store": store,
        "items": items,
        "status": "placed",
        "estimatedPickup": (now.replace(second=0, microsecond=0) + __import__("datetime").timedelta(minutes=60)).isoformat().replace("+00:00", "Z"),
        "subtotal": subtotal,
        "placedAt": _iso_now(),
    }
    _orders.setdefault(user_id, []).append(order)
    return order


def get_order_by_id(user_id: str, order_id: str) -> Optional[dict]:
    for order in _orders.get(user_id, []):
        if order["id"] == order_id:
            return order
    return None


def update_order_status(user_id: str, order_id: str, status: str) -> Optional[dict]:
    for order in _orders.get(user_id, []):
        if order["id"] == order_id:
            order["status"] = status
            return order
    return None
