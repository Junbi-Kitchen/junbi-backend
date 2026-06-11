# 2026-06-10 20:28 — ledq — worklog-meal-planning-loop

**Branch:** ingredient_resolver
**Repo:** junbi-backend

---

## What was done

**Strategy (docs/):**
- Wrote plan-selection strategy C from the problem statement blind, compared against A/B, synthesized + adversarially audited into the canonical `meal-planning-final-strategy.md` (rank-and-assemble engine + learning loop; pairing cautions and inline budget trade replaced B's variants; "signals are exhaust, not homework"; "reflection proposes, code applies")
- Added `meal-planning-stakeholder-brief.md` (non-technical pitch), `meal-planning-mvp-plan.md` (superseded — went straight to full M0–M3 once synthetic seed data unblocked the catalog), and `meal-planning-frontend-plan.md` (API contract + 5 screen specs for junbi-frontend)

**Implementation (M0–M3, complete):**
- Migration `20260610000000_meal_planning_loop.sql` (+ `draft_schema.sql` mirror): bundle-shaped `meal_plans`/`meal_plan_items` (soft `suggested_day`, `is_probe`, `alternates`, `ranking_artifact`, `budget_trade`), new `plan_events` (append-only signal log, source of truth), `user_recipe_affinity`, `user_model_features`, `user_model_narratives`, `model_update_log`, `recipes.embedding vector(384)`, `ingredients.estimated_price`
- Seeds: 91-recipe synthetic catalog (`scripts/seed/data/recipe_catalog.json` + seeder, MiniLM embeddings, 174 pack prices, deliberate allergen coverage), demo user/pantry with staggered expiries, `make seed-catalog`
- Engine (`app/services/meal_planning/engine.py`, pure Python): union/marginal cost math, budget cap enforced by construction, variety cap, quick-meals-for-busy-nights, explore probe, per-slot alternates, budget-trade delta, soft rhythm
- Bounded candidate pool (`planner.select_candidates`): cap 30 with quota seats (rescuers/quick/cheap/probes) + cuisine round-robin fill
- ADK pipeline (`app/agents/meal_planner/`, gemini-2.5-flash): SequentialAgent load_facts → ranker (output_schema: ranking + pairing cautions + rationales) → assemble; reflector agent for weekly narrative; runner with deterministic no-LLM fallback
- API (`app/api/routes/meal_plans.py`): generate / current / react (accept, swap, never_show, budget trade) / tonight + ack / onboarding deck + swipes / close-week / model snapshot / DEMO_MODE reset — every reaction writes `plan_events`
- Learning (`learning.py`): full event-log replay with strategy §6 signal weights + probe multipliers, feature updates, recency decay; embedding-neighbor affinity priors; reflection applies clamped (±0.1) proposals, versioned narrative with money-figure guard
- Tests: 47 passing — engine (17, incl. adversarial rankings), pool cap (7), planted-persona learning (9), e2e loop integration (creates throwaway user, lives a scripted week, asserts week-2 consequences + zero allergen/budget violations)

**Verified live:** full loop on deterministic path (never-show gone from week 2, cooked probe promoted); Gemini ranking via Vertex/gcloud ADC (`rankingSource: llm`, context-aware rationales naming expiring ingredients)

## Decisions made

- **Rank-and-assemble over frontier-judge:** the LLM ranking is a persistent week-long artifact (powers alternates, mid-week swaps, trade) vs. a consumed choice; frontier-judge documented as fallback with written kill-criteria
- **LLM judges, code computes — everywhere:** cost appears to the model only as bands; constraints filtered in SQL before the prompt exists; ranking validated (hallucinated ids dropped); no agentic cost tools (repo's own Kroger token-burn lesson)
- **Events are source of truth; learned state is a rebuildable projection** (full replay each week — retuning weights is retroactive)
- **One recipe = one slot; pack-based "have any = covered" cost** (v1 crudeness, consistent across cap/trade/grocery list)
- **Prices: demand-populated cache, not live-per-plan** — live Kroger reserved for the final list at order time; per-store pricing, refresher, ingredient consolidation, facet tags, CP-SAT all parked with written triggers (anti-over-engineering pass)
- **Demo page dropped** — junbi-frontend is the demo surface (plan doc written); DEMO_MODE flag + reset endpoint kept for dev
- ADK + Gemini for all LLM steps per platform decision (not direct Anthropic SDK)

## Bottlenecks hit

- `waitlist_signups` existed unrecorded in `schema_migrations` → made that migration idempotent (IF NOT EXISTS) per approval
- ADK 2.2.0 moved LiteLLM behind `google-adk[extensions]` (smart_grocery import broke app boot) → installed extra, pinned in requirements.txt
- Root `conftest.py` injected `DATABASE_URL=sqlite:///test.db` env var, silently overriding `.env` under pytest (env vars beat .env in pydantic-settings) → conftest now loads `.env` first, dummies only as CI fallback; integration test went from mis-skipping to passing
- Runner gated Gemini on `GOOGLE_API_KEY` only → added Vertex/ADC detection (`GOOGLE_GENAI_USE_VERTEXAI`)

## Still mocked / pending

- Reflector has not run live yet (Gemini connected after last close-week; first real close-week will produce narrative v1)
- Ingredient prices are synthetic seed values (Kroger refresher parked); ~170 catalog ingredients are stubs not alias-linked to USDA rows (consolidation parked until user recipe import)
- Pre-existing, untouched: `tests/kroger/test_kroger_api.py` fails collection (imports removed `_parse_agent_response`); `seed_mock.py` still imports psycopg2 (not installed)
- Frontend screens (junbi-frontend) — contract in `docs/meal-planning-frontend-plan.md`

## Next up

- Commit this work
- First live close-week (verifies reflector narrative)
- Frontend implementation against the plan doc
