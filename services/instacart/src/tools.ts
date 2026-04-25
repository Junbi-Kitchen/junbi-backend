/**
 * Claude Agent SDK tool definitions for the Instacart integration.
 *
 * These tools wrap InstacartService methods into the shape expected by
 * LangGraph nodes that call Claude with tool_use. Each tool has a detailed
 * description so Claude knows exactly when and how to invoke it.
 *
 * Tool execution flow in the Smart Grocery agent:
 *   findStores → searchProducts (per item) → buildCart → checkout
 *
 * The agent never calls checkout autonomously — it always surfaces
 * the cart + budget check to the user first (human_checkpoint node).
 */

import Anthropic from "@anthropic-ai/sdk";
import { z } from "zod";
import { instacartService } from "./instacartService";
import { CartItem } from "./types";

// Anthropic SDK tool type alias for clarity
type AnthropicTool = Anthropic.Tool;

// ---------------------------------------------------------------------------
// Input validators (Zod)
// These mirror the JSON schema in the tool definitions below.
// ---------------------------------------------------------------------------

const FindStoresInput = z.object({
  zip_code: z.string().min(5).max(10),
});

const SearchProductsInput = z.object({
  store_id: z.string(),
  query: z.string().min(1),
  limit: z.number().int().min(1).max(10).optional().default(5),
});

const BuildCartInput = z.object({
  store_id: z.string(),
  items: z.array(
    z.object({
      sku: z.string(),
      qty: z.number().positive(),
      substitution: z
        .object({
          type: z.enum(["specific_item", "best_match", "do_not_substitute"]),
          fallback_sku: z.string().optional(),
          fallback_name: z.string().optional(),
        })
        .optional(),
    })
  ),
});

const CheckoutInput = z.object({
  cart_id: z.string(),
  max_budget: z.number().nonnegative(),
  fulfillment_type: z.enum(["delivery", "pickup"]).default("delivery"),
  pickup_location_id: z.string().optional(),
  bypass_budget: z.boolean().optional().default(false),
});

const SubstitutionInput = z.object({
  original_item_name: z.string(),
  original_sku: z.string(),
  reason: z.enum(["out_of_stock", "price_too_high", "user_preference"]),
  fallback_strategy: z.enum(["specific_item", "best_match", "do_not_substitute"]),
  fallback_sku: z.string().optional(),
  fallback_name: z.string().optional(),
});

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

export const findStoresTool: AnthropicTool = {
  name: "find_stores",
  description: `
    Find grocery retailers available for delivery or pickup at a given zip code.
    Use this tool FIRST before searching for products. You need a store_id to
    search products. Returns stores like Walmart, Kroger, Costco, Aldi, Publix
    with their IDs, fulfillment options, and distances.

    Use this when:
    - Starting a new grocery session
    - The user hasn't specified a store preference yet
    - The user wants to compare which stores are available near them
  `.trim(),
  input_schema: {
    type: "object" as const,
    properties: {
      zip_code: {
        type: "string",
        description: "5-digit US zip code of the user's delivery address",
      },
    },
    required: ["zip_code"],
  },
};

export const searchProductsTool: AnthropicTool = {
  name: "search_products",
  description: `
    Search a specific store's product catalog by item name or description.
    Returns up to 'limit' products with SKUs, prices, and availability status.

    IMPORTANT: You must have a store_id from find_stores before calling this.
    Call this tool ONCE per distinct grocery item you need to buy.
    Prefer specific queries over generic ones — "organic whole milk 1 gallon"
    returns better results than just "milk".

    Use this when:
    - Building a grocery list and need to match pantry items to store SKUs
    - Checking if a specific product is available at a store
    - Comparing prices for a product across multiple searches

    Do NOT use this for checkout — use build_cart after you have all SKUs.
  `.trim(),
  input_schema: {
    type: "object" as const,
    properties: {
      store_id: {
        type: "string",
        description: "Retailer ID from find_stores",
      },
      query: {
        type: "string",
        description: "Product search query, e.g. 'organic whole milk 1 gallon'",
      },
      limit: {
        type: "number",
        description: "Max results to return (1-10, default 5)",
      },
    },
    required: ["store_id", "query"],
  },
};

export const buildCartTool: AnthropicTool = {
  name: "build_cart",
  description: `
    Create a cart at a store with a list of products (by SKU) and quantities.
    Each item can include a substitution policy for when the product is
    out of stock. Returns a cart_id and estimated total.

    IMPORTANT: Gather ALL product SKUs via search_products before calling this.
    Do not call this tool until you have searched for every item on the list.

    Substitution policies:
    - "best_match": Instacart picks the closest equivalent (recommended default)
    - "specific_item": you specify an exact fallback_sku (use the SubstitutionTool first)
    - "do_not_substitute": item is skipped if out of stock

    Use this when:
    - You have SKUs for all needed items and are ready to build the order
    - Never call this to "check" prices — use search_products for that
  `.trim(),
  input_schema: {
    type: "object" as const,
    properties: {
      store_id: {
        type: "string",
        description: "Retailer ID from find_stores",
      },
      items: {
        type: "array",
        description: "List of items with SKU, quantity, and optional substitution policy",
        items: {
          type: "object",
          properties: {
            sku: { type: "string", description: "Product ID from search_products" },
            qty: { type: "number", description: "Quantity to purchase" },
            substitution: {
              type: "object",
              description: "What to do if this item is out of stock",
              properties: {
                type: {
                  type: "string",
                  enum: ["specific_item", "best_match", "do_not_substitute"],
                },
                fallback_sku: {
                  type: "string",
                  description: "Required if type is specific_item",
                },
                fallback_name: {
                  type: "string",
                  description: "Human-readable name of fallback product",
                },
              },
              required: ["type"],
            },
          },
          required: ["sku", "qty"],
        },
      },
    },
    required: ["store_id", "items"],
  },
};

export const checkoutTool: AnthropicTool = {
  name: "checkout",
  description: `
    Finalise a cart as an order. Includes a MANDATORY budget check.

    Budget guard (soft-fail):
    If the cart total exceeds max_budget, this tool returns a "budget_exceeded"
    error with the actual total. DO NOT retry automatically. Instead, surface
    the overage to the user and ask if they want to proceed anyway. Only set
    bypass_budget=true if the user explicitly approves the overage.

    Returns a checkout_url that the frontend opens in an in-app WebView
    for the user to complete payment. The user never leaves the app.

    Use this ONLY after:
    1. The cart has been built with build_cart
    2. The user has reviewed and confirmed the cart (human checkpoint)
    3. Budget has been checked or explicitly bypassed by the user

    Never call this tool speculatively or before user confirmation.
  `.trim(),
  input_schema: {
    type: "object" as const,
    properties: {
      cart_id: {
        type: "string",
        description: "cart_id from build_cart",
      },
      max_budget: {
        type: "number",
        description: "Maximum spend in USD. Set to 0 for no limit.",
      },
      fulfillment_type: {
        type: "string",
        enum: ["delivery", "pickup"],
        description: "How the user wants to receive the order",
      },
      pickup_location_id: {
        type: "string",
        description: "Required when fulfillment_type is 'pickup'",
      },
      bypass_budget: {
        type: "boolean",
        description: "Set true only if the user explicitly approved a budget overage",
      },
    },
    required: ["cart_id", "max_budget"],
  },
};

export const substitutionTool: AnthropicTool = {
  name: "set_substitution",
  description: `
    Define what the Instacart shopper should do if a specific item is out of stock.
    Call this before build_cart for any item where a simple best_match isn't
    good enough — e.g. dietary restrictions, brand preferences, or when the user
    has expressed a preference.

    Examples:
    - "If organic eggs are gone, get free-range eggs instead"
      → fallback_strategy: specific_item, fallback_sku: <searched SKU>
    - "If almond milk is gone, do not substitute — I'm allergic to oat milk"
      → fallback_strategy: do_not_substitute
    - "Just get something similar" → fallback_strategy: best_match (this is the default)

    This tool returns a substitution policy object to attach to a cart item.
    It does NOT make any API calls — it's a pure data-structuring step.
  `.trim(),
  input_schema: {
    type: "object" as const,
    properties: {
      original_item_name: {
        type: "string",
        description: "Human-readable name of the primary item",
      },
      original_sku: {
        type: "string",
        description: "SKU of the primary item from search_products",
      },
      reason: {
        type: "string",
        enum: ["out_of_stock", "price_too_high", "user_preference"],
        description: "Why a substitution policy is needed",
      },
      fallback_strategy: {
        type: "string",
        enum: ["specific_item", "best_match", "do_not_substitute"],
      },
      fallback_sku: {
        type: "string",
        description: "SKU of the fallback product (required for specific_item strategy)",
      },
      fallback_name: {
        type: "string",
        description: "Human-readable name of the fallback product",
      },
    },
    required: ["original_item_name", "original_sku", "reason", "fallback_strategy"],
  },
};

// All tools exported as an array for easy registration in LangGraph nodes
export const ALL_INSTACART_TOOLS: AnthropicTool[] = [
  findStoresTool,
  searchProductsTool,
  buildCartTool,
  checkoutTool,
  substitutionTool,
];

// ---------------------------------------------------------------------------
// Tool executor — called by the LangGraph tool-execution node
// ---------------------------------------------------------------------------

export interface ToolResult {
  tool_name: string;
  success: boolean;
  data?: unknown;
  error?: string;
}

/**
 * Execute a tool by name with the given input.
 * The LangGraph node receives Claude's tool_use block and calls this.
 *
 * @param toolName  — matches tool.name above
 * @param rawInput  — the JSON object from Claude's tool_use.input
 */
export async function executeTool(
  toolName: string,
  rawInput: unknown
): Promise<ToolResult> {
  try {
    switch (toolName) {
      case "find_stores": {
        const input = FindStoresInput.parse(rawInput);
        const stores = await instacartService.getStores(input.zip_code);
        return { tool_name: toolName, success: true, data: stores };
      }

      case "search_products": {
        const input = SearchProductsInput.parse(rawInput);
        const products = await instacartService.searchProducts(
          input.store_id,
          input.query,
          input.limit
        );
        return { tool_name: toolName, success: true, data: products };
      }

      case "build_cart": {
        const input = BuildCartInput.parse(rawInput);
        const cartItems: CartItem[] = input.items.map((item) => ({
          sku: item.sku,
          qty: item.qty,
          substitution: item.substitution,
        }));
        const cart = await instacartService.createCart(input.store_id, cartItems);
        return { tool_name: toolName, success: true, data: cart };
      }

      case "checkout": {
        const input = CheckoutInput.parse(rawInput);
        const outcome = await instacartService.executeCheckout(
          input.cart_id,
          input.max_budget,
          input.fulfillment_type,
          input.pickup_location_id,
          input.bypass_budget
        );
        // Both success and budget_exceeded are valid non-throwing outcomes —
        // the agent decides what to do with them
        return { tool_name: toolName, success: true, data: outcome };
      }

      case "set_substitution": {
        // Pure data structuring — no API call needed
        const input = SubstitutionInput.parse(rawInput);
        const policy = {
          original_sku: input.original_sku,
          original_name: input.original_item_name,
          substitution: {
            type: input.fallback_strategy,
            ...(input.fallback_strategy === "specific_item" && {
              fallback_sku: input.fallback_sku,
              fallback_name: input.fallback_name,
            }),
          },
        };
        return { tool_name: toolName, success: true, data: policy };
      }

      default:
        return {
          tool_name: toolName,
          success: false,
          error: `Unknown tool: ${toolName}`,
        };
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { tool_name: toolName, success: false, error: message };
  }
}
