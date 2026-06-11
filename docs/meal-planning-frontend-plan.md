# Meal Planning — Frontend Implementation Plan (junbi-frontend)

**Audience:** frontend engineer implementing the meal-planning loop screens in the Expo app.
**Backend status:** all endpoints below are live, tested (47 backend tests incl. an end-to-end loop test), and return camelCase JSON. LLM ranking (Gemini) and the no-LLM fallback both work; the frontend never needs to care which ran (`rankingSource` says, for debugging).
**Product background (read once):** `meal-planning-stakeholder-brief.md` (5 min). Deep spec: `meal-planning-final-strategy.md`.

---

## 1. The one mental model

Every screen does two jobs at once: **show the plan** and **capture signals**. Each tap (swap, never-show, "made it", taking the budget trade, even just opening tonight's recipe) is a training event for the personalization loop — the backend logs it and next week's plan visibly improves because of it. **Never swallow a reaction client-side**: if the user changes something, an endpoint must hear about it. There is no "save" button concept; every reaction posts immediately.

Output shape rule: the week is a **bundle** (a set of ~5 meals + one grocery list), *not* a calendar. `suggestedDay` is advice — render it as a gentle ordering, never as a grid the user must obey.

## 2. Dev setup

```bash
# backend (junbi-backend):
make dev                # API on :8000 — lib/api.ts already targets this via Metro host
make seed-catalog       # 91 recipes + demo pantry (once per fresh DB)
```

Two auth modes:
- **Normal (recommended):** sign in with any Firebase test account; routes auto-provision the user. The existing `lib/api.ts` client works unchanged.
- **`DEMO_MODE=true` on the backend:** all meal-plan routes act as the seeded demo user `dev-user-alex-rivera` regardless of the token — useful with curl/postman, and `POST /meal-plans/demo/reset` wipes that user's loop state for clean replays. Don't run real-account testing with this flag on.

## 3. API reference

All under `/meal-plans`. Auth: standard `Authorization: Bearer <Firebase ID token>` (already handled by `lib/api.ts`).

### Onboarding

`GET /meal-plans/onboarding/deck`
```json
{ "deck": [ {"recipeId": "…", "title": "Teriyaki Salmon", "cuisine": "japanese", "description": "…"} ] }
```
~15 cards spanning cuisines. Show as a swipe deck (like/pass).

`POST /meal-plans/onboarding/swipes`
```json
{ "swipes": [ {"recipeId": "…", "liked": true}, … ] }   →   { "recorded": 15 }
```
Batch at the end of the deck. Each swipe seeds the taste model — week 1 is personalized because of this.

### The weekly plan

`POST /meal-plans/generate`
```json
{ "slots": 5, "busyNights": 2, "budgetCap": 90, "notes": "guests on Friday" }
```
`budgetCap` optional (falls back to the profile's weekly budget); `notes` is free text the AI actually uses. Response (also the shape of `GET /meal-plans/current`):
```json
{
  "planId": "…", "weekStart": "2026-06-08", "status": "proposed",
  "computedCost": 78.87, "budgetTarget": 90.0, "rankingSource": "llm",
  "bundleStatus": "feasible", "flags": [],
  "budgetTrade": { "recipe_id": "…", "title": "Teriyaki Salmon",
                   "swap_out_recipe_id": "…", "delta": 9.40, "taken": null },
  "items": [
    { "recipeId": "…", "title": "Chicken Tikka Masala", "cuisine": "indian",
      "totalTimeMins": 55, "suggestedDay": 6, "isProbe": false,
      "rationale": "Highest affinity and perfect for guests on Friday.",
      "alternates": ["id1", "id2", "id3"], "addedVia": "planner" }
  ],
  "groceryList": [ { "ingredientId": "…", "name": "coconut milk", "price": 2.29 } ]
}
```
Notes: `suggestedDay` 0=Mon…6=Sun, nullable. `budgetTrade` is null when the cap forced nothing out. `groceryList` is currently only on the generate response (backend follow-up: add to `/current`). Generation takes a few seconds (one LLM call) — show a real loading state; **this is the only slow endpoint**.

### Reactions (review screen)

`POST /meal-plans/{planId}/react` — body `{ "action": …, "recipeId"?, "replacementId"? }` → updated plan.

| action | params | meaning |
|---|---|---|
| `accept` | — | user takes the plan (status → accepted) |
| `swap` | `recipeId` (out) + `replacementId` (must be in that item's `alternates`) | strongest learning signal |
| `never_show` | `recipeId` | hard veto, recipe never appears again |
| `budget_trade_take` | — | execute the trade chip; cost is recomputed server-side |
| `budget_trade_decline` | — | dismiss the chip (also a signal — teaches budget strictness) |

Errors: `409` swap would exceed the cap (revert UI, show message), `422` replacement not in alternates.

### Tonight

`GET /meal-plans/{planId}/tonight`
```json
{ "recipeId": "…", "title": "Lemon Herb Baked Cod", "totalTimeMins": 28,
  "isProbe": false, "rationale": "…",
  "alternates": [ {"recipeId": "…", "title": "Croque Monsieur"} ] }
```
`POST /meal-plans/{planId}/tonight/ack` — `{ "recipeId", "action": "opened" | "made_it" | "quick_swap", "replacementId"? }` → `{ "ok": true }`

**Fire `opened` automatically** when the user views the recipe from this surface (it's the cook-through proxy — invisible UX, vital data). `made_it` is one explicit tap. `quick_swap` uses the precomputed alternates — render them instantly, no loading state, that's the point.

### Closing the loop

`POST /meal-plans/{planId}/close-week?generate_next=true`
```json
{ "learned": { "eventsReplayed": 26, "recipesScored": 20,
               "topMovers": [ {"recipeId": "…", "title": "Palak Paneer", "delta": -1.8, "score": -1.8} ],
               "features": { "budget_strictness": 0.45, … },
               "reflection": { "narrative_updated": true, "narrative_version": 2 } },
  "nextPlan": { …same shape as generate… } }
```
In production this runs on a schedule; for now it's also the **demo time-travel button** (dev-only UI is fine).

`GET /meal-plans/model` — the "what we learned" panel:
```json
{ "topRecipes": [ {"recipeId","title","score","confidence"} ],
  "bottomRecipes": [ … ], "features": [ {"key","value","confidence"} ],
  "narrative": "Household favors quick comfort meals on weeknights…", "narrativeVersion": 2 }
```

## 4. Screens (5 surfaces)

Suggested structure per existing conventions: `stores/mealPlanStore.ts` + `hooks/useMealPlan.ts`, screens under `app/meal-plan/`, entry from the home tab.

**S1 — Onboarding swipe deck** (once, after signup): full-screen cards, like/pass, batch-post at the end, then straight into generating week 1. Frame as "60 seconds to teach your planner."

**S2 — Weekly bundle review** (the core screen): **one proposal, never alternatives side-by-side.** Cost vs. cap rendered prominently (`$78.87 of $90` — these numbers are code-computed; display verbatim, never recompute client-side). Each meal card: title, time, `rationale` as a one-liner, probe badge on `isProbe` (*"Something new — tell us how it lands"* — honest labeling is the feature), overflow menu → swap (bottom-sheet listing the 3 alternates with titles — needs a titles lookup or backend follow-up below) and never-show. If `budgetTrade` is non-null, render the chip: *"Add {title} back · +${delta}"* with take/dismiss. Accept button → `react accept`. Soft rhythm: order cards by `suggestedDay` with weekday chips, visually advisory.

**S3 — Tonight** (home-tab widget + screen): tonight's pick, big "I made it" affordance, "swap tonight" → instant alternates list. Fire `opened` on viewing the recipe detail.

**S4 — What we learned** (after close-week, also reachable from profile): narrative text as the hero, top/bottom movers with deltas, features as simple meters. This panel is the retention story — make week-over-week improvement *visible*.

**S5 — Grocery list**: from `groceryList` — names + prices + total. Read-only v1; ordering integration is the existing grocery flow's job.

**Edge states to handle:** `GET /current` → 404 (no plan yet → CTA to generate); `bundleStatus: "infeasible"` + `flags: ["budget_too_tight"]` (banner: "couldn't fit 5 meals under $X — raise budget or drop a meal"); `"insufficient_pool"` (CTA: save more recipes); generation failure is handled server-side (deterministic fallback) so the frontend never needs an LLM-error state.

## 5. Signal capture rules (the part that makes or breaks the product)

1. Every reaction posts immediately — no local-only state for swaps/vetoes/trades.
2. `opened` fires automatically on recipe view from Tonight; never ask the user to "log" anything.
3. Optimistic UI is fine for swaps (the alternates were pre-validated server-side), but reconcile with the returned plan — `computedCost` may change.
4. Don't invent signals: only the actions in §3 exist. If a UX idea needs a new signal type, it's a 5-minute backend addition — ask, don't fake it client-side.

## 6. Small backend follow-ups the frontend may request (cheap, ask anytime)

- `groceryList` included in `GET /current` (currently only on generate).
- Alternates with titles inline on plan items (currently ids; titles available via the tonight endpoint pattern).
- Recipe detail fields (steps/ingredients) — exists via the recipes API already; meal-plan items link by `recipeId`.

## 7. Acceptance test (the full arc, manually or in Maestro/Detox)

1. Fresh account → swipe deck → generate → bundle shows 5 meals, cost ≤ cap, one probe badge, rationales present.
2. Swap a meal (instant), never-show another, take the trade chip → cost updates from server values.
3. Tonight: open recipe (auto-`opened`), tap "made it."
4. Close week (dev button) → learned panel shows movers + narrative → week 2 appears.
5. **Verify the loop visibly closed:** the never-show'd meal is absent from week 2; the cooked probe's cuisine is back. That moment is the product.
