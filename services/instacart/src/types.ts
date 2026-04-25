/**
 * Shared types for the Gook Instacart integration service.
 *
 * API reference: https://docs.instacart.com/connect
 */

// ---------------------------------------------------------------------------
// Instacart Connect API — raw response shapes
// ---------------------------------------------------------------------------

export interface InstacartRetailer {
  id: string;             // Instacart's internal retailer ID
  name: string;           // e.g. "Walmart", "Kroger", "Costco"
  slug: string;           // e.g. "walmart", "kroger"
  logo_url: string | null;
  distance_miles: number | null;
  fulfillment_types: Array<"delivery" | "pickup">;
  pickup_locations: PickupLocation[];
}

export interface PickupLocation {
  id: string;
  name: string;
  address: string;
  distance_miles: number | null;
}

export interface InstacartProduct {
  id: string;             // Instacart product ID (used as SKU in cart)
  name: string;
  brand: string | null;
  size: string | null;    // e.g. "32 fl oz"
  price: number;          // unit price in USD
  sale_price: number | null;
  image_url: string | null;
  available: boolean;
  aisle: string | null;
}

export interface InstacartCart {
  cart_id: string;
  retailer_id: string;
  items: CartLineItem[];
  subtotal: number;
  estimated_tax: number;
  estimated_total: number;
  created_at: string;
}

export interface CartLineItem {
  product_id: string;
  product_name: string;
  quantity: number;
  unit_price: number;
  line_total: number;
  substitution_policy: SubstitutionPolicy | null;
}

export interface SubstitutionPolicy {
  type: "specific_item" | "best_match" | "do_not_substitute";
  fallback_product_id?: string;   // only for specific_item
  fallback_product_name?: string; // human-readable, for agent context
}

// ---------------------------------------------------------------------------
// Checkout — what the Connect API returns vs what ACP will add
// ---------------------------------------------------------------------------

/**
 * Standard Connect API checkout result.
 * The `checkout_url` is opened in an in-app WebView for payment.
 *
 * NOTE: Programmatic order placement (no WebView) requires an Instacart
 * partnership agreement beyond the standard developer tier.
 * When Instacart's Agentic Commerce Protocol (ACP) ships, the `order_id`
 * will be returned directly here without a WebView redirect.
 */
export interface CheckoutResult {
  cart_id: string;
  checkout_url: string;       // open in WKWebView — user completes payment in-app
  estimated_total: number;
  fulfillment_type: "delivery" | "pickup";
  pickup_location_id: string | null;
  // ACP fields (future — not yet in production API)
  order_id?: string;          // present when Instacart places the order autonomously
  acp_status?: "placed" | "pending_approval" | "budget_exceeded";
}

// ---------------------------------------------------------------------------
// Budget guard
// ---------------------------------------------------------------------------

export type CheckoutOutcome =
  | { success: true; result: CheckoutResult }
  | { success: false; reason: "budget_exceeded"; estimated_total: number; max_budget: number }
  | { success: false; reason: "api_error"; message: string };

// ---------------------------------------------------------------------------
// Webhook — order.delivered payload
// ---------------------------------------------------------------------------

export interface InstacartWebhookPayload {
  event_type: "order.delivered" | "order.cancelled" | "order.updated";
  event_id: string;
  order_id: string;
  user_id: string;          // Instacart user ID — must map to our user via connected_accounts
  retailer_id: string;
  retailer_name: string;
  delivered_at: string;     // ISO 8601
  items: DeliveredItem[];
}

export interface DeliveredItem {
  product_id: string;
  product_name: string;
  brand: string | null;
  quantity: number;
  unit: string | null;
  was_substituted: boolean;
  original_product_id: string | null;  // null if not substituted
  original_product_name: string | null;
}

// ---------------------------------------------------------------------------
// Gook domain types (passed between agent and service)
// ---------------------------------------------------------------------------

export interface StoreResult {
  store_id: string;
  name: string;
  logo_url: string | null;
  supports_delivery: boolean;
  supports_pickup: boolean;
  distance_miles: number | null;
}

export interface ProductSearchResult {
  sku: string;
  name: string;
  brand: string | null;
  size: string | null;
  price: number;
  sale_price: number | null;
  available: boolean;
  image_url: string | null;
  aisle: string | null;
}

export interface CartItem {
  sku: string;
  qty: number;
  substitution?: {
    type: "specific_item" | "best_match" | "do_not_substitute";
    fallback_sku?: string;
    fallback_name?: string;
  };
}

// Normalised pantry item written back to Gook DB after delivery
export interface PantryUpdateItem {
  name: string;
  quantity: number;
  unit: string | null;
  source: "instacart_delivery";
  was_substituted: boolean;
}
