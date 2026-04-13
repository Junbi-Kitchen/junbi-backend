"""
Smart Grocery LangGraph StateGraph.

Flow:
  load_context → analyze_pantry → search_store → compare_prices
      → build_cart → human_checkpoint → place_order → finalize

The graph is compiled with MemorySaver so state persists across the
human_checkpoint interrupt. Each user session uses a thread_id
(user_id + session timestamp) so concurrent runs don't collide.
"""

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from .state import SmartGroceryState
from .nodes import (
    load_context,
    analyze_pantry,
    search_store,
    compare_prices,
    build_cart,
    human_checkpoint,
    place_order,
    finalize,
)


def _should_proceed(state: SmartGroceryState) -> str:
    """After human_checkpoint: route to place_order or END based on confirmation."""
    if state.get("order_confirmed"):
        return "place_order"
    return "cancelled"


def _cancelled(state: SmartGroceryState) -> dict:
    return {"order_result": {"status": "cancelled"}, "error": None}


def build_graph() -> StateGraph:
    builder = StateGraph(SmartGroceryState)

    builder.add_node("load_context", load_context)
    builder.add_node("analyze_pantry", analyze_pantry)
    builder.add_node("search_store", search_store)
    builder.add_node("compare_prices", compare_prices)
    builder.add_node("build_cart", build_cart)
    builder.add_node("human_checkpoint", human_checkpoint)
    builder.add_node("place_order", place_order)
    builder.add_node("finalize", finalize)
    builder.add_node("cancelled", _cancelled)

    builder.set_entry_point("load_context")
    builder.add_edge("load_context", "analyze_pantry")
    builder.add_edge("analyze_pantry", "search_store")
    builder.add_edge("search_store", "compare_prices")
    builder.add_edge("compare_prices", "build_cart")
    builder.add_edge("build_cart", "human_checkpoint")

    builder.add_conditional_edges(
        "human_checkpoint",
        _should_proceed,
        {"place_order": "place_order", "cancelled": "cancelled"},
    )

    builder.add_edge("place_order", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("cancelled", END)

    return builder


# Compile once at module load — MemorySaver keeps interrupt state in-process.
# For multi-process / production deployments swap MemorySaver for
# langgraph.checkpoint.postgres.PostgresSaver pointed at your Supabase DB.
_checkpointer = MemorySaver()
graph = build_graph().compile(checkpointer=_checkpointer, interrupt_before=["human_checkpoint"])
