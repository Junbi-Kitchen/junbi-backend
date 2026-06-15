# Meal Planning MVP — Demonstrate the Loop

**Goal:** a functioning vertical slice that makes a stakeholder say *"oh, it can work like that."* Not a toy, not production. The centerpiece is **the loop closing live**: plan → react → learn → visibly better plan. A bundle generator alone is what every meal app demos; the demo-able moat is week 2 responding to week 1.

**Demo cheat (deliberate):** a "close week" control that simulates time passing — triggers the learning step and generates the next week on demand. Everything else is the real engine, real data, real LLM calls.

---

## 1. The demo arc (what the stakeholder sees, ~10 minutes)

1. **Onboard (90 sec).** Set a shellfish allergy, a $90/week budget, household of 2. Swipe through ~15 recipe cards — like / pass. *"That 60 seconds just seeded the taste model."*
2. **Week 1 bundle.** One proposal: 5 dinners, **$84 of $90** (code-computed, shown against the cap), a one-line "why" on each, a soft rhythm (ambitious meal → Sunday, quick ones → busy nights), one meal labeled *"something new — tell us."* No shrimp anywhere — and we show why: the pool view with allergens filtered out before the AI ever saw them. A budget-trade chip sits on the bundle: *"+$9.40 adds the salmon back."*
3. **React.** Swap one meal — alternates appear instantly (pre-computed, no spinner). Mark one "never show again." Take the salmon trade — watch the total update from code, not the model.
4. **Live the week (simulated).** The "tonight" view: open a recipe (that's a cooked-proxy signal), one-tap "made it" on three nights, leave one untouched, swap one night to a quick alternate.
5. **Close the week.** Learning runs. A **"what we learned" panel** shows the model deltas: affinity moves (with the evidence — "swapped out twice"), and the AI-written narrative: *"Avoids seafood except salmon. Weeknights need ≤30 min. The Tuesday slot reliably slips."*
6. **Week 2 bundle.** Visibly different, and we point at the diff line by line: the never-show recipe is gone, the swap-in's cuisine got more weight, the probe outcome shaped a new probe, quick meals landed on the nights that slipped. Edits needed: week 1 took 3 taps, week 2 takes 1. **That falling number is the product.**

## 2. In scope (the core that must work for real)

| Piece | Why it's in |
|---|---|
| Seeded recipe catalog (~60–80 recipes, ingredients mapped, priced) | The pool everything ranks over — prerequisite #1 |
| Hard-constraint filtering + budget-capped assembly (A-library math: union cost, marginal cost) | The "Safe" demo beat — must be provably airtight |
| The ranking LLM call (ranking + pairing cautions + rationales, structured output; deterministic fallback) | The one sync call, exactly as the strategy specs it |
| Bundle output: one proposal, soft rhythm, computed cost, per-meal "why" | The acceptance UX |
| Explore slot, labeled, probe-tagged | The learning-forward story |
| Budget trade chip (code-computed marginal cost, one tap) | Cheap to build, lands the "code owns money" point |
| Pre-computed per-slot alternates → instant swaps | The "no spinner at 6pm" beat |
| **`plan_events` signal log** (append-only, every reaction/tap) | The moat's fuel line — events are the source of truth |
| Tonight view: open-recipe proxy, one-tap "made it," quick-swap | Signal exhaust, not homework — demonstrated |
| Learning step: code updates affinity + features from events; one reflection call writes the narrative | The loop actually closing |
| "What we learned" panel (deltas + narrative + evidence) | Makes the invisible loop visible — the demo's heart |
| "Close week" time-travel control | Demo mechanics |

## 3. Explicitly out (and why it's safe to cut)

- **Embedding priors / nearest-neighbor generalization** — with a swipe-seeded model and an 80-recipe catalog, direct affinity + tags carry a 2-week demo. First post-MVP addition.
- **Real checkout** — stop at the consolidated, priced grocery list. (Static/cached Kroger prices at seed time; live pricing is plumbing, not proof.)
- **Per-member household modeling, negotiation** — single household profile; the allergy array protects everyone, which is the part that can't wait.
- **Mid-week LLM re-planning** — cached alternates cover the demo; the novel-situation call is post-MVP.
- **Quantity-aware pantry matching** — v1 "have any = covered," per the strategy's own allowance.
- **Reflection proposals → bounded L2 writes** — MVP reflection writes narrative only; code owns all numbers from stats. (Tightest possible safety story for the demo, and one less moving part.)
- **Firebase auth in the demo path** — demo runs as the seeded mock user.

## 4. Build plan

**Phase 0 — Substrate (prerequisites).**
- Seed catalog: ~60–80 recipes generated offline (Claude-assisted, then human-skimmed), ingredients mapped to existing `ingredients` rows, prices cached into the DB (Kroger lookups at seed time where easy, static table elsewhere). Spread across cuisines/proteins/effort so ranking and variety have room to move.
- Migration (dbmate + mirror in `draft_schema.sql`): `plan_events`, `user_recipe_affinity`, `user_model_features`, `user_model_narratives`; evolve `meal_plans` (+`ranking_artifact JSONB`, `computed_cost`, status lifecycle, trade record) and `meal_plan_items` (`suggested_day` nullable, `is_probe`, `alternates JSONB`, `rationale`, `added_via`).
- Seed a demo pantry (a few expiring items, so "uses the spinach" rationales appear).

**Phase 1 — Engine (`app/services/meal_planning/`).**
- Pool facts: filter (L1), cost band, marginal `cost_to_add`, `expiring_rescued`, effort, affinity.
- Assembly: ranked walk, budget/variety caps, set-wise quick-meal rule, pairing-caution penalties, explore-slot reservation, alternates, budget-trade computation. **Headless-testable: deterministic affinity rank, no API key needed — this is also the test suite's spine.**
- Ranking call: structured output (`ranking`, `pairing_cautions`, `rationales`), `claude-sonnet-4-6`, fallback to deterministic rank.

**Phase 2 — API (`app/api/routes/meal_plans.py`).**
- `POST /meal-plans/generate` · `GET /meal-plans/current` · `POST /meal-plans/{id}/react` (swap / never-show / trade / accept — every reaction writes `plan_events`) · `GET /meal-plans/{id}/tonight` + `POST .../tonight/ack` (opened / made-it / quick-swap) · `POST /meal-plans/{id}/close-week` (learning step + next-week generation) · `GET /users/me/model` (affinity deltas + narrative, for the panel) · `POST /onboarding/swipes`.

**Phase 3 — Learning step.**
- Code: event log → affinity updates per the strategy's §6 signal weights, rhythm/budget features, recency decay.
- Reflection call: events + old narrative → new versioned narrative (3–5 observations, capped).
- The "what we learned" payload: top score moves with their evidence events, narrative diff.

**Phase 4 — Demo surface + script.**
- A thin single-page demo UI served by FastAPI (mock user, no auth): swipe deck → bundle card → reactions → tonight view → close-week → learned panel → week-2 bundle with a highlighted diff. It calls the same real API the app would.
- The written demo script (the §1 arc), plus a reset endpoint so the demo replays cleanly.

**Order matters:** Phases 0–1 are pure code and fully testable before any UI exists; the LLM call drops in last within Phase 1.

## 5. MVP success criteria

1. **The loop visibly closes:** every week-1 reaction has a traceable consequence in week 2 (we can point at the event row and the diff it caused).
2. **Safety is demonstrable, not asserted:** the allergen never appears in any pool, bundle, or alternate; the bundle cost never exceeds the cap — under adversarial demo conditions (tiny budget, heavy constraints).
3. **Edits needed drop from week 1 to week 2** in the scripted demo path.
4. The demo runs end-to-end in ~10 minutes, resets cleanly, and works headless (no API key → deterministic mode) so a dead network can't kill the meeting.
5. The numbers shown on screen are all code-computed — grep-provably never from model output.

## 6. Open decisions (small, needed before Phase 0)

1. **Demo UI vs. real app frontend** — recommended: the thin served demo page now (controllable, fast, no app-release coupling); the real app adopts the same endpoints later.
2. **Catalog sourcing** — recommended: Claude-generated structured recipes, human-skimmed, seeded via `scripts/seed/`; revisit real import post-MVP.
3. ~~Whether the schema goes through dbmate now~~ — yes, per earlier discussion; events are forever, start them on real migrations.
