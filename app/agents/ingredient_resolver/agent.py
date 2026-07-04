"""
ingredient_resolver/agent.py
─────────────────────────────
Defines the Ingredient Resolver LangGraph — a 2-node Anthropic tool-use loop.

Role of this agent
──────────────────
Resolves a raw ingredient name (e.g. "2 cups finely chopped organic yellow
onion") to a canonical row in the shared `ingredients` table. It's only
invoked after a cheap exact-match pre-screen misses (see
app/services/ingredient_resolver.py) — this is the fallback path for names
that need fuzzy matching or genuinely don't exist yet.

Why a graph instead of a single Claude call
─────────────────────────────────────────────
The model needs to look at real candidates before deciding: it calls
search_fulltext and search_vector to fetch candidate ingredients, evaluates
them, and only then either returns an existing id or calls create_ingredient.
That's a variable number of tool round-trips (Claude decides how many), which
is exactly the standard Anthropic tool-use loop:

    call_model ⇄ call_tools

  call_model  — sends the running message list to Claude with the 3 tools
                available. If the response contains no tool_use blocks,
                that's the final answer and the graph ends.
  call_tools  — executes whatever tool(s) the model asked for (dispatched
                through _TOOL_FUNCTIONS, which just wraps the plain async
                functions in tools.py — those are already framework-agnostic
                and needed no changes) and appends the results as a tool
                result message, then loops back to call_model.

No checkpointer/interrupt here — unlike the Smart Grocery graph, this is a
single-shot, non-interactive resolution with no human review step.

Model
─────
claude-haiku-4-5-20251001 — this is a narrow classification/lookup task, not
a task that benefits from a larger model.
"""

import json
import logging
from typing import TypedDict

import anthropic
from langgraph.graph import END, StateGraph

from app.config import settings

from .tools import create_ingredient, search_fulltext, search_vector

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """\
You are resolving an ingredient name to a canonical entry in a shared food database.

You will receive a JSON object with:
- raw_name: the original text (e.g. "2 cups finely chopped organic yellow onion")
- normalized_name: already cleaned (e.g. "yellow onion")

The exact match pre-screen has already run and found nothing. Use your tools.

PROCESS:
1. Call search_fulltext with normalized_name to find candidates.
2. Call search_vector with normalized_name to find more candidates.
3. Evaluate all candidates across both results.
4. STRONGLY prefer matching an existing ingredient over creating a new one.
   - "spring onion" → "green onion" is a match
   - "brown sugar" → "sugar" is a match when no brown sugar entry exists
   - Case differences alone are NOT a reason to create a new ingredient
   - Only create a new ingredient if nothing in the candidates is the same food
5. If you pick an existing ingredient: respond with JSON only:
   {"ingredient_id": "<uuid>", "method": "matched", "reason": "brief explanation"}
6. If nothing matches and the ingredient is genuinely new: call create_ingredient,
   then respond with JSON only:
   {"ingredient_id": "<uuid>", "method": "created", "reason": "brief explanation"}

Your final response must be ONLY the JSON object — no preamble, no explanation
before or after it, no markdown fence. Just the raw JSON.\
"""

# Anthropic tool schemas — input_schema mirrors the actual function signatures
# in tools.py (which stayed framework-agnostic and needed no changes).
_TOOLS = [
    {
        "name": "search_fulltext",
        "description": "Search ingredients by full-text match. Returns up to 5 candidates with id, name, score.",
        "input_schema": {
            "type": "object",
            "properties": {"normalized_name": {"type": "string"}},
            "required": ["normalized_name"],
        },
    },
    {
        "name": "search_vector",
        "description": "Search ingredients by vector similarity. Returns up to 5 candidates with id, name, similarity.",
        "input_schema": {
            "type": "object",
            "properties": {"normalized_name": {"type": "string"}},
            "required": ["normalized_name"],
        },
    },
    {
        "name": "create_ingredient",
        "description": "Create a new canonical ingredient. Only call when no existing candidate is the same food.",
        "input_schema": {
            "type": "object",
            "properties": {
                "normalized_name": {"type": "string"},
                "raw_name": {"type": "string"},
            },
            "required": ["normalized_name", "raw_name"],
        },
    },
]

_TOOL_FUNCTIONS = {
    "search_fulltext": search_fulltext,
    "search_vector": search_vector,
    "create_ingredient": create_ingredient,
}


class IngredientResolverState(TypedDict):
    # Anthropic-native message dicts, e.g. {"role": "user"|"assistant", "content": [...]}.
    # Each node returns the full list (not just its own delta) — simple overwrite
    # semantics, no reducer needed for a loop this short.
    messages: list[dict]


async def call_model(state: IngredientResolverState) -> dict:
    """Ask Claude to either call a tool or return its final JSON verdict."""
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=_TOOLS,
        messages=state["messages"],
    )
    assistant_message = {"role": "assistant", "content": response.content}
    return {"messages": state["messages"] + [assistant_message]}


async def call_tools(state: IngredientResolverState) -> dict:
    """Execute every tool_use block in the last assistant message, then hand back to call_model."""
    last = state["messages"][-1]
    tool_results = []
    for block in last["content"]:
        if getattr(block, "type", None) != "tool_use":
            continue
        fn = _TOOL_FUNCTIONS[block.name]
        try:
            result = await fn(**block.input)
        except Exception as e:
            logger.warning("ingredient_resolver tool %s failed: %s", block.name, e)
            result = {"error": str(e)}
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(result),
        })
    return {"messages": state["messages"] + [{"role": "user", "content": tool_results}]}


def _should_continue(state: IngredientResolverState) -> str:
    last = state["messages"][-1]
    has_tool_use = any(getattr(b, "type", None) == "tool_use" for b in last["content"])
    return "call_tools" if has_tool_use else END


def _build_graph():
    builder = StateGraph(IngredientResolverState)
    builder.add_node("call_model", call_model)
    builder.add_node("call_tools", call_tools)
    builder.set_entry_point("call_model")
    builder.add_conditional_edges("call_model", _should_continue, {"call_tools": "call_tools", END: END})
    builder.add_edge("call_tools", "call_model")
    return builder.compile()


# Compiled once at import time and shared across all requests — stateless
# between calls (no checkpointer), so this is safe under concurrency.
ingredient_resolver_graph = _build_graph()
