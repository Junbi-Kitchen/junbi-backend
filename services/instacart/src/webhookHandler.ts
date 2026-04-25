/**
 * Instacart webhook handler — Express server.
 *
 * Listens for Instacart Connect API events and translates them into
 * pantry updates in the Gook backend.
 *
 * Registered events:
 *   order.delivered  → extract delivered items (with substitutions) → update pantry
 *   order.cancelled  → log cancellation, no pantry change
 *   order.updated    → log for debugging
 *
 * Security:
 *   Instacart signs webhook payloads with HMAC-SHA256 using your webhook secret.
 *   Verify the signature on every request before processing.
 *
 * Environment variables required:
 *   INSTACART_WEBHOOK_SECRET   — from your Instacart developer dashboard
 *   GOOK_BACKEND_URL           — e.g. http://localhost:8000 or https://api.gook.app
 *   GOOK_SERVICE_API_KEY       — internal key for service-to-service calls to Python backend
 *   PORT                       — defaults to 3001
 *
 * Register your webhook URL in the Instacart developer dashboard:
 *   https://connect.instacart.com/developer → Webhooks → Add endpoint
 *   URL: https://your-domain.com/webhooks/instacart
 *   Events: order.delivered, order.cancelled
 */

import express, { Request, Response, NextFunction } from "express";
import crypto from "crypto";
import axios from "axios";
import { InstacartWebhookPayload, PantryUpdateItem } from "./types";

const app = express();

// Parse raw body for signature verification — must come before json()
app.use(
  "/webhooks/instacart",
  express.raw({ type: "application/json" }),
  signatureVerifier,
  express.json()
);
app.use(express.json());

// ---------------------------------------------------------------------------
// Signature verification middleware
// ---------------------------------------------------------------------------

function signatureVerifier(req: Request, res: Response, next: NextFunction): void {
  const secret = process.env.INSTACART_WEBHOOK_SECRET;
  if (!secret) {
    console.error("[Webhook] INSTACART_WEBHOOK_SECRET not set — cannot verify signatures");
    res.status(500).json({ error: "Webhook secret not configured" });
    return;
  }

  const signature = req.headers["x-instacart-signature"] as string | undefined;
  if (!signature) {
    console.warn("[Webhook] Missing x-instacart-signature header");
    res.status(401).json({ error: "Missing signature" });
    return;
  }

  // Instacart uses HMAC-SHA256: signature = hex(HMAC(secret, raw_body))
  const rawBody = req.body as Buffer;
  const expected = crypto
    .createHmac("sha256", secret)
    .update(rawBody)
    .digest("hex");

  if (!crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) {
    console.warn("[Webhook] Signature mismatch — rejecting request");
    res.status(401).json({ error: "Invalid signature" });
    return;
  }

  // Re-parse body as JSON after signature check
  try {
    req.body = JSON.parse(rawBody.toString("utf8"));
  } catch {
    res.status(400).json({ error: "Invalid JSON body" });
    return;
  }

  next();
}

// ---------------------------------------------------------------------------
// POST /webhooks/instacart — main event receiver
// ---------------------------------------------------------------------------

app.post("/webhooks/instacart", async (req: Request, res: Response) => {
  // Acknowledge immediately — Instacart expects a 2xx within 5 seconds
  res.status(200).json({ received: true });

  const payload = req.body as InstacartWebhookPayload;

  console.info(
    `[Webhook] Received event=${payload.event_type} order_id=${payload.order_id}`
  );

  switch (payload.event_type) {
    case "order.delivered":
      await handleOrderDelivered(payload);
      break;

    case "order.cancelled":
      console.info(`[Webhook] Order ${payload.order_id} cancelled — no pantry update needed`);
      break;

    case "order.updated":
      console.debug(`[Webhook] Order ${payload.order_id} updated — no action taken`);
      break;

    default:
      console.warn(`[Webhook] Unhandled event type: ${payload.event_type}`);
  }
});

// ---------------------------------------------------------------------------
// order.delivered handler
// ---------------------------------------------------------------------------

async function handleOrderDelivered(payload: InstacartWebhookPayload): Promise<void> {
  const { order_id, user_id, retailer_name, items } = payload;

  console.info(
    `[Webhook] Processing delivery: order=${order_id} user=${user_id} ` +
    `retailer=${retailer_name} items=${items.length}`
  );

  // Build pantry update items — use delivered product (may be a substitution)
  const pantryItems: PantryUpdateItem[] = items.map((item) => ({
    name: item.product_name,
    quantity: item.quantity,
    unit: item.unit,
    source: "instacart_delivery" as const,
    was_substituted: item.was_substituted,
  }));

  // Log substitutions for transparency in the app
  const substituted = items.filter((i) => i.was_substituted);
  if (substituted.length > 0) {
    console.info(
      `[Webhook] ${substituted.length} substitution(s) in order ${order_id}:`,
      substituted.map((s) => `"${s.original_product_name}" → "${s.product_name}"`).join(", ")
    );
  }

  // Resolve Instacart user_id → Gook user_id via the Python backend
  const gookUserId = await resolveGookUser(user_id);
  if (!gookUserId) {
    console.error(
      `[Webhook] Cannot resolve Instacart user_id=${user_id} to a Gook account. ` +
      `Make sure the user has linked their Instacart account in connected_accounts.`
    );
    return;
  }

  // Push pantry update to the Gook Python backend
  await pushPantryUpdate(gookUserId, order_id, pantryItems);
}

// ---------------------------------------------------------------------------
// Resolve Instacart user → Gook user
// ---------------------------------------------------------------------------

async function resolveGookUser(instacartUserId: string): Promise<string | null> {
  const backendUrl = process.env.GOOK_BACKEND_URL ?? "http://localhost:8000";
  const apiKey = process.env.GOOK_SERVICE_API_KEY;

  try {
    const response = await axios.get<{ gook_user_id: string }>(
      `${backendUrl}/internal/users/by-provider`,
      {
        params: { provider: "instacart", provider_user_id: instacartUserId },
        headers: { "X-Service-Key": apiKey },
        timeout: 5_000,
      }
    );
    return response.data.gook_user_id;
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) {
      return null; // User hasn't linked their Instacart account
    }
    console.error("[Webhook] resolveGookUser error:", err);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Push pantry update to Gook Python backend
// ---------------------------------------------------------------------------

async function pushPantryUpdate(
  gookUserId: string,
  orderId: string,
  items: PantryUpdateItem[]
): Promise<void> {
  const backendUrl = process.env.GOOK_BACKEND_URL ?? "http://localhost:8000";
  const apiKey = process.env.GOOK_SERVICE_API_KEY;

  try {
    await axios.post(
      `${backendUrl}/internal/pantry/bulk-from-delivery`,
      { user_id: gookUserId, order_id: orderId, items },
      {
        headers: {
          "Content-Type": "application/json",
          "X-Service-Key": apiKey,
        },
        timeout: 10_000,
      }
    );
    console.info(
      `[Webhook] Pantry updated for user=${gookUserId}: ${items.length} items added`
    );
  } catch (err) {
    console.error(
      `[Webhook] Failed to push pantry update for user=${gookUserId} order=${orderId}:`,
      err instanceof Error ? err.message : err
    );
    // TODO: Add to a retry queue (e.g. BullMQ or a simple DB retry table)
  }
}

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------

app.get("/health", (_req, res) => {
  res.json({ status: "ok", service: "gook-instacart-webhook" });
});

// ---------------------------------------------------------------------------
// Start server
// ---------------------------------------------------------------------------

const PORT = parseInt(process.env.PORT ?? "3001", 10);
app.listen(PORT, () => {
  console.info(`[Webhook] Gook Instacart webhook server running on port ${PORT}`);
  console.info(`[Webhook] Endpoint: POST http://localhost:${PORT}/webhooks/instacart`);
});

export default app;
