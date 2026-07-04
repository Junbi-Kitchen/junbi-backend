"""
smart_grocery_agent/graph.py
─────────────────────────────
Defines and compiles the Smart Grocery LangGraph.

Graph shape (linear, with one human-in-the-loop branch point):

    load_context → resolve_stores → analyze_pantry → search_store
        → compare_prices → build_cart → human_checkpoint
            ├─ confirmed=True  → place_order → finalize → END
            └─ confirmed=False → cancelled → END

Why LangGraph over an LLM tool-calling orchestrator
────────────────────────────────────────────────────
The step order here is fixed and known in advance — there's nothing for an
LLM to decide by "choosing" which tool to call next. Encoding that order as
graph edges instead of a system prompt means:
  - Zero orchestration LLM calls (Claude is only invoked inside analyze_pantry
    and build_cart, where actual reasoning happens)
  - No risk of the model skipping/reordering steps or hallucinating tool args
  - A real interrupt/resume primitive for the human review step (see below)

Human-in-the-loop checkpoint
─────────────────────────────
human_checkpoint calls `interrupt()`, which suspends the graph mid-run. The
first `ainvoke()` (runner.start_grocery_agent) returns as soon as it hits the
interrupt, carrying the state computed so far — the frontend renders that as
the cart-for-review screen. The graph only continues when
runner.confirm_grocery_order() calls `ainvoke(Command(resume=...), config)`
with the same thread_id. Never bypass this to "just run it through" — the
user must approve the cart before place_order/finalize run.

Checkpointing
─────────────
MemorySaver is an in-process, in-memory checkpointer — state is lost on
restart and isn't shared across replicas. That's an accepted trade-off for
now (same as the old ADK `_pending_sessions` dict); swap to `PostgresSaver`
pointing at Supabase before running multiple backend replicas in production.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from . import nodes as n
from .state import SmartGroceryState


def _route_after_checkpoint(state: SmartGroceryState) -> str:
    return "place_order" if state.get("confirmed") else "cancelled"


def _build_graph():
    builder = StateGraph(SmartGroceryState)

    builder.add_node("load_context", n.load_context)
    builder.add_node("resolve_stores", n.resolve_stores)
    builder.add_node("analyze_pantry", n.analyze_pantry)
    builder.add_node("search_store", n.search_store)
    builder.add_node("compare_prices", n.compare_prices)
    builder.add_node("build_cart", n.build_cart)
    builder.add_node("human_checkpoint", n.human_checkpoint)
    builder.add_node("place_order", n.place_order)
    builder.add_node("finalize", n.finalize)
    builder.add_node("cancelled", n.cancelled)

    builder.set_entry_point("load_context")
    builder.add_edge("load_context", "resolve_stores")
    builder.add_edge("resolve_stores", "analyze_pantry")
    builder.add_edge("analyze_pantry", "search_store")
    builder.add_edge("search_store", "compare_prices")
    builder.add_edge("compare_prices", "build_cart")
    builder.add_edge("build_cart", "human_checkpoint")
    builder.add_conditional_edges(
        "human_checkpoint",
        _route_after_checkpoint,
        {"place_order": "place_order", "cancelled": "cancelled"},
    )
    builder.add_edge("place_order", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("cancelled", END)

    return builder.compile(checkpointer=MemorySaver())


# Compiled once at import time and shared across all requests. The checkpointer
# is keyed by thread_id (== session_id), so concurrent sessions never collide
# even though this graph instance is shared.
smart_grocery_graph = _build_graph()
