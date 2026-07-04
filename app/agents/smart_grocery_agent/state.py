"""
smart_grocery_agent/state.py
─────────────────────────────
Shared state for the Smart Grocery LangGraph.

Every node receives the full state and returns a partial dict of the keys it
wrote — LangGraph merges that into the running state before the next node
runs. There is no LLM sequencing tool calls here: the node order below *is*
the graph (see graph.py), so state only needs to carry data forward, not
instruct an orchestrator what to do next.

Field ownership (which node writes each key):
  user_id, delivery_preference, budget   — set by the caller (runner.py) at invoke time
  pantry_items, saved_recipes,
  existing_grocery_items, user_preferences,
  user_address                           — load_context
  nearby_stores, store_preference        — resolve_stores
  missing_items                          — analyze_pantry (budget may also be
                                            refined here from user_preferences)
  kroger_result                          — search_store
  price_comparison, savings_summary      — compare_prices
  cart_items, cart_total, agent_summary  — build_cart
  confirmed, store_override              — human_checkpoint (from the resumed value)
  kroger_cart_status                     — place_order
  grocery_list_id                        — finalize
  error                                  — any node, on a recoverable failure
"""

from typing import Literal, TypedDict


class SmartGroceryState(TypedDict, total=False):
    # Set at invoke time
    user_id: str
    delivery_preference: Literal["delivery", "pickup"]
    budget: float | None

    # load_context
    pantry_items: list[dict]
    saved_recipes: list[dict]
    existing_grocery_items: list[dict]
    user_preferences: dict
    user_address: str | None

    # resolve_stores
    nearby_stores: list[dict]
    store_preference: str

    # analyze_pantry
    missing_items: list[dict]

    # search_store
    kroger_result: dict

    # compare_prices
    price_comparison: dict
    savings_summary: dict

    # build_cart
    cart_items: list[dict]
    cart_total: float
    agent_summary: str

    # human_checkpoint (from the resumed value)
    confirmed: bool
    store_override: str | None

    # place_order
    kroger_cart_status: dict

    # finalize / cancelled — terminal status, read by get_session_status()
    # ("order_placed" | "cancelled"). Not derived from grocery_list_id, which
    # is legitimately None both when the cart was empty and when the DB write
    # failed — neither of those means the order wasn't placed.
    status: Literal["order_placed", "cancelled"]
    grocery_list_id: str | None

    # any node
    error: str
