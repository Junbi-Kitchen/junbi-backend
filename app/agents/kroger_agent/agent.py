"""
kroger_agent/agent.py
─────────────────────
Defines the Kroger ADK agent and its MCP toolset connection.

Architecture overview
─────────────────────
This module wires together two things:

  1. MCPToolset — starts the `kroger-mcp` subprocess (via `uvx kroger-mcp`) and
     exposes all of its 30+ MCP tools (search_locations, search_products,
     add_items_to_cart, view_current_cart, etc.) to the ADK agent.

  2. LlmAgent — a Gemini 2.0 Flash agent that receives a shopping list + zip code,
     calls the MCP tools in sequence to find the nearest Kroger store and search
     for each item, then returns a structured JSON result.

Why MCPToolset + subprocess?
─────────────────────────────
The `kroger-mcp` package (https://github.com/CupOfOwls/kroger-mcp) wraps the
Kroger public developer API behind the Model Context Protocol. Using it as a
subprocess means we get OAuth token management, automatic refresh, and a local
cart-tracking layer for free — without reimplementing those things ourselves.

The MCPToolset must be used as an async context manager to manage the subprocess
lifecycle. `build_kroger_agent()` returns both the agent and the toolset so the
caller (runner.py) can open `async with toolset:` around the agent run.

Credentials
───────────
Reads from environment variables (see .env.example):
  KROGER_CLIENT_ID      — from developer.kroger.com
  KROGER_CLIENT_SECRET  — from developer.kroger.com
  KROGER_REDIRECT_URI   — must match your registered Kroger app redirect URI

The agent itself runs on Gemini via GOOGLE_API_KEY (set in env, picked up by
the google-genai SDK automatically).
"""

import os

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset, StdioServerParameters

# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

# Tells the agent exactly what sequence of MCP tool calls to make and what
# JSON shape to return. Being explicit about the output format avoids the
# model wrapping the result in prose or markdown.
INSTRUCTION = """You are a Kroger grocery shopping assistant. Your job is to:

1. Find the nearest Kroger-family store to the user's zip code using search_locations.
   Call set_preferred_location with the best location_id you find.

2. For each item in the shopping list, call search_products with:
   - The item name as search_term
   - The location_id you found
   - limit=5

3. After searching all items, return a JSON object (ONLY valid JSON, no prose) with this shape:
{
  "store": {
    "location_id": "<id>",
    "name": "<store name>",
    "address": "<street address or city>"
  },
  "results": {
    "<item_name>": [
      {
        "product_id": "<id>",
        "name": "<product name>",
        "price": <float or null>,
        "unit": "<size string>",
        "image_url": "<url or null>",
        "aisle": "<aisle location or null>"
      }
    ]
  },
  "unfound": ["<item names with zero results>"],
  "cart_preview": [
    {
      "name": "<original item name>",
      "product_id": "<best match product_id>",
      "product_name": "<best match product name>",
      "price": <unit price float or null>,
      "quantity": <requested quantity int>,
      "unit": "<unit string>",
      "estimated_price": <price * quantity or null>,
      "aisle": "<aisle or null>"
    }
  ],
  "estimated_total": <sum of all estimated_prices, float>
}

Pick the best match per item: prefer brand-name when it matches, otherwise pick
the lowest-priced option. If no products found for an item, add it to "unfound"
and skip it in cart_preview.

Respond with ONLY the JSON object — no explanation, no markdown fences."""


# ─────────────────────────────────────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────────────────────────────────────

def build_kroger_agent() -> tuple[LlmAgent, MCPToolset]:
    """
    Build and return the Kroger LlmAgent and its MCP toolset.

    Returns a (agent, toolset) tuple. The toolset MUST be used as an async
    context manager by the caller to start/stop the kroger-mcp subprocess:

        agent, toolset = build_kroger_agent()
        async with toolset:
            # agent is ready to run here

    A new agent + toolset pair is created on each call so each run gets a
    fresh subprocess and OAuth token context. This is intentional — Kroger
    tokens are stored on disk by kroger-mcp, so state persists across runs
    even though the objects are recreated.

    Environment variables read here:
      KROGER_CLIENT_ID      — Kroger developer app client ID
      KROGER_CLIENT_SECRET  — Kroger developer app client secret
      KROGER_REDIRECT_URI   — OAuth redirect URI registered in Kroger dashboard
    """
    # Pass Kroger credentials into the subprocess environment so kroger-mcp
    # can authenticate against the Kroger API without touching the host env.
    env = {
        "KROGER_CLIENT_ID": os.environ.get("KROGER_CLIENT_ID", ""),
        "KROGER_CLIENT_SECRET": os.environ.get("KROGER_CLIENT_SECRET", ""),
        "KROGER_REDIRECT_URI": os.environ.get(
            "KROGER_REDIRECT_URI",
            "http://localhost:8000/auth/kroger/callback",
        ),
    }

    # MCPToolset connects to the kroger-mcp server over stdio.
    # `uvx kroger-mcp` runs the installed kroger-mcp package without needing
    # it to be on PATH — uvx resolves it from the active Python environment.
    toolset = MCPToolset(
        connection_params=StdioServerParameters(
            command="uvx",
            args=["kroger-mcp"],
            env=env,
        )
    )

    # LlmAgent receives the MCP toolset and treats each MCP tool as a
    # callable function. Gemini decides which tools to call and in what
    # order based on the INSTRUCTION prompt.
    agent = LlmAgent(
        name="kroger_agent",
        model="gemini-2.0-flash",
        instruction=INSTRUCTION,
        tools=[toolset],
    )

    return agent, toolset
