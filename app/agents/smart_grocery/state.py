from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class MissingItem(TypedDict):
    name: str
    quantity: float
    unit: str
    reason: str        # 'recipe_gap' | 'expiring_soon' | 'staple'
    priority: int      # 1=expiring (highest), 2=recipe gap, 3=staple


class StoreProduct(TypedDict):
    store: str
    product_id: str
    name: str
    price: float
    unit: str
    image_url: str | None


class CartItem(TypedDict):
    name: str
    quantity: float
    unit: str
    product_id: str | None
    estimated_price: float | None
    store: str
    aisle: str | None


class SmartGroceryState(TypedDict):
    user_id: str

    # Request inputs
    store_preference: str        # 'kroger' | 'instacart' | 'walmart'
    delivery_preference: str     # 'delivery' | 'pickup'
    budget: float | None

    # Context loaded from DB
    pantry_items: list[dict]
    saved_recipes: list[dict]
    existing_grocery_items: list[dict]
    user_preferences: dict       # dietary_tags, allergies, household_size, weekly_budget

    # Claude analysis output
    missing_items: list[MissingItem]

    # Store search results: item name → list of matching products
    store_search_results: dict[str, list[StoreProduct]]

    # Price comparison across stores: store → estimated cart total
    price_comparison: dict[str, float]
    savings_summary: dict        # {best_store, worst_store, savings_amount, savings_pct}

    # Final cart
    cart_items: list[CartItem]
    cart_total: float

    # Order lifecycle
    order_confirmed: bool | None   # None=pending review, True=go, False=cancelled
    order_result: dict | None      # {order_id, status, checkout_url (for WebView stores)}

    # Written back to DB after order
    grocery_list_id: str | None

    # LangGraph message history (Claude tool calls live here)
    messages: Annotated[list, add_messages]

    error: str | None
