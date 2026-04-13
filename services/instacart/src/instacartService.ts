/**
 * InstacartService — core HTTP client for the Instacart Connect API.
 *
 * API reference: https://docs.instacart.com/connect
 *
 * Authentication:
 *   All requests require:
 *     Authorization: Bearer <INSTACART_API_KEY>
 *
 *   Get your key at: https://connect.instacart.com/developer
 *
 * Partnership tiers:
 *   Standard developer key  → product search, cart creation, checkout URL
 *   Partnership tier        → programmatic order placement (no WebView needed)
 *
 * Environment variables required:
 *   INSTACART_API_KEY       — your Connect API key
 *   INSTACART_BASE_URL      — defaults to https://connect.instacart.com/v2
 */

import axios, { AxiosInstance, AxiosError } from "axios";
import {
  StoreResult,
  ProductSearchResult,
  CartItem,
  InstacartCart,
  CheckoutResult,
  CheckoutOutcome,
  InstacartRetailer,
  InstacartProduct,
} from "./types";

const DEFAULT_BASE_URL = "https://connect.instacart.com/v2";

export class InstacartService {
  private readonly client: AxiosInstance;

  constructor(apiKey?: string) {
    const key = apiKey ?? process.env.INSTACART_API_KEY;
    if (!key) {
      throw new Error(
        "INSTACART_API_KEY is required. Get one at https://connect.instacart.com/developer"
      );
    }

    this.client = axios.create({
      baseURL: process.env.INSTACART_BASE_URL ?? DEFAULT_BASE_URL,
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      timeout: 15_000,
    });

    // Log request/response in dev for visibility
    if (process.env.NODE_ENV !== "production") {
      this.client.interceptors.request.use((config) => {
        console.debug(`[Instacart] ${config.method?.toUpperCase()} ${config.url}`);
        return config;
      });
    }
  }

  // ---------------------------------------------------------------------------
  // getStores — find local retailers for a given zip code
  // ---------------------------------------------------------------------------

  /**
   * Returns retailers available in the given zip code.
   * Each retailer has a store_id used in all subsequent calls.
   *
   * Instacart surfaces Walmart, Kroger, Costco, Aldi, Publix, etc.
   * depending on what's available at that location.
   */
  async getStores(zipCode: string): Promise<StoreResult[]> {
    try {
      const response = await this.client.get<{ retailers: InstacartRetailer[] }>(
        "/retailers",
        { params: { zip_code: zipCode, include_fulfillment_types: true } }
      );

      return response.data.retailers.map((r) => ({
        store_id: r.id,
        name: r.name,
        logo_url: r.logo_url,
        supports_delivery: r.fulfillment_types.includes("delivery"),
        supports_pickup: r.fulfillment_types.includes("pickup"),
        distance_miles: r.distance_miles,
      }));
    } catch (err) {
      throw this.wrapError("getStores", err);
    }
  }

  // ---------------------------------------------------------------------------
  // searchProducts — find products at a specific store
  // ---------------------------------------------------------------------------

  /**
   * Searches a retailer's catalog for a query string.
   * Returns up to `limit` products with SKUs, prices, and availability.
   *
   * The returned `sku` (product_id) is what you pass into createCart.
   *
   * @param storeId  — retailer ID from getStores()
   * @param query    — plain text search, e.g. "organic whole milk"
   * @param limit    — max results to return (default 5, max 20)
   */
  async searchProducts(
    storeId: string,
    query: string,
    limit = 5
  ): Promise<ProductSearchResult[]> {
    try {
      const response = await this.client.get<{ products: InstacartProduct[] }>(
        `/retailers/${storeId}/products/search`,
        { params: { query, limit } }
      );

      return response.data.products.map((p) => ({
        sku: p.id,
        name: p.name,
        brand: p.brand,
        size: p.size,
        price: p.price,
        sale_price: p.sale_price,
        available: p.available,
        image_url: p.image_url,
        aisle: p.aisle,
      }));
    } catch (err) {
      throw this.wrapError("searchProducts", err);
    }
  }

  // ---------------------------------------------------------------------------
  // createCart — assemble a cart with substitution policies per item
  // ---------------------------------------------------------------------------

  /**
   * Creates a cart at the given retailer with the supplied items.
   * Each item can carry a substitution policy so the shopper knows what
   * to do if the primary SKU is out of stock.
   *
   * Returns the full cart including estimated totals.
   */
  async createCart(
    storeId: string,
    items: CartItem[]
  ): Promise<InstacartCart> {
    try {
      const payload = {
        retailer_id: storeId,
        line_items: items.map((item) => ({
          product_id: item.sku,
          quantity: item.qty,
          substitution_policy: item.substitution
            ? {
                type: item.substitution.type,
                ...(item.substitution.type === "specific_item" && {
                  fallback_product_id: item.substitution.fallback_sku,
                }),
              }
            : { type: "best_match" }, // default: let Instacart pick closest match
        })),
      };

      const response = await this.client.post<InstacartCart>("/carts", payload);
      return response.data;
    } catch (err) {
      throw this.wrapError("createCart", err);
    }
  }

  // ---------------------------------------------------------------------------
  // executeCheckout — soft-fail budget guard then proceed to checkout
  // ---------------------------------------------------------------------------

  /**
   * Attempts to finalise a cart as an order.
   *
   * Budget guard (soft-fail):
   *   If cart.estimated_total > maxBudget, returns { success: false, reason: "budget_exceeded" }
   *   instead of throwing. This lets the agent surface the overage to the user
   *   and ask for explicit approval before retrying with confirmed=true.
   *
   * Fulfillment:
   *   delivery  — Instacart sends a shopper to deliver to the user's address.
   *   pickup    — user collects from pickupLocationId at the store.
   *
   * Return value:
   *   On success, checkout_url is opened in an in-app WebView for payment.
   *   When Instacart's ACP (Agentic Commerce Protocol) ships, order_id will
   *   be returned here directly and no WebView is needed.
   *
   * @param cartId            — cart_id from createCart()
   * @param maxBudget         — hard ceiling in USD; 0 = no limit
   * @param fulfillmentType   — "delivery" | "pickup"
   * @param pickupLocationId  — required when fulfillmentType = "pickup"
   * @param bypassBudget      — set true if user explicitly approved the overage
   */
  async executeCheckout(
    cartId: string,
    maxBudget: number,
    fulfillmentType: "delivery" | "pickup" = "delivery",
    pickupLocationId?: string,
    bypassBudget = false
  ): Promise<CheckoutOutcome> {
    // First fetch current cart totals to run the budget check
    let cart: InstacartCart;
    try {
      const response = await this.client.get<InstacartCart>(`/carts/${cartId}`);
      cart = response.data;
    } catch (err) {
      return {
        success: false,
        reason: "api_error",
        message: this.wrapError("executeCheckout:fetchCart", err).message,
      };
    }

    // Soft-fail budget guard
    if (maxBudget > 0 && cart.estimated_total > maxBudget && !bypassBudget) {
      return {
        success: false,
        reason: "budget_exceeded",
        estimated_total: cart.estimated_total,
        max_budget: maxBudget,
      };
    }

    // Proceed to checkout
    try {
      const payload: Record<string, unknown> = {
        cart_id: cartId,
        fulfillment_type: fulfillmentType,
      };
      if (fulfillmentType === "pickup" && pickupLocationId) {
        payload.pickup_location_id = pickupLocationId;
      }

      const response = await this.client.post<CheckoutResult>(
        "/checkouts",
        payload
      );

      return { success: true, result: response.data };
    } catch (err) {
      return {
        success: false,
        reason: "api_error",
        message: this.wrapError("executeCheckout:checkout", err).message,
      };
    }
  }

  // ---------------------------------------------------------------------------
  // getCart — retrieve a cart by ID (used for polling / status)
  // ---------------------------------------------------------------------------

  async getCart(cartId: string): Promise<InstacartCart> {
    try {
      const response = await this.client.get<InstacartCart>(`/carts/${cartId}`);
      return response.data;
    } catch (err) {
      throw this.wrapError("getCart", err);
    }
  }

  // ---------------------------------------------------------------------------
  // Error normalisation
  // ---------------------------------------------------------------------------

  private wrapError(context: string, err: unknown): Error {
    if (err instanceof AxiosError) {
      const status = err.response?.status;
      const body = JSON.stringify(err.response?.data ?? {});
      return new Error(`[InstacartService.${context}] HTTP ${status}: ${body}`);
    }
    if (err instanceof Error) {
      return new Error(`[InstacartService.${context}] ${err.message}`);
    }
    return new Error(`[InstacartService.${context}] Unknown error`);
  }
}

// Singleton for use across tools and handlers
export const instacartService = new InstacartService();
