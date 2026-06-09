# Meal Plan Selection — Design

**Status:** Design (not yet implemented)
**Scope:** Stage 2 of the Smart Grocery pipeline — selecting which recipes make up a user's week.
**Last updated:** 2026-06-09

---

## 1. Where this sits

The Smart Grocery feature is a deterministic pipeline with a small number of well-placed LLM calls — **not** an LLM agent that orchestrates everything. Full pipeline:

| Stage | What it does | Owner |
|---|---|---|
| 0. Load context | Pantry, recipes, prefs, addresses — parallel DB | code |
| 1. Score & narrow | Filter + rank recipe pool → candidates | code |
| **2. Plan selection** | **Choose the week's recipes** | **code + 1 LLM call** ← *this doc* |
| 3. Build shopping list | Aggregate selected recipes − pantry | code (+ optional LLM residual match) |
| 4. Kroger product search | Live products/prices for the to-buy list | code (existing) |
| 5. Cart product selection | Best product per item (allergen/budget/dietary) | LLM (existing) |
| 6. Review & confirm | User approves; write to DB | code (two-phase) |

This document specifies **Stage 2** in full.

---

## 2. Guiding principle

> **Formalize everything that has a correct formula — however hard (use a solver if needed). Hand the LLM only what has no correct formula for anyone.**

The decision rule has **three** buckets, not two:

| A formula… | …goes to | Why |
|---|---|---|
| exists & is easy | code | obvious |
| exists but is hard to compute (combinatorial, non-additive) | **solver (still code)** | this is what solvers are *for*; LLMs are **worst** at hard math |
| genuinely doesn't exist (no formula correct for everyone) | **LLM** | nothing else can |

**Hard ≠ fuzzy.** "Too complicated" sends work to a better algorithm. Only "undefinable" sends work to the LLM. Handing complicated-but-definable math to an LLM is the classic mistake — it produces confident wrong numbers.

Corollaries used throughout:
- **Code measures, the LLM weighs.** The LLM never does arithmetic and never produces the number the user sees.
- **Live prices belong at the cart (Stage 4/5), not at ranking.** Ranking uses coarse estimates; the displayed total is computed by code from live prices later.
- **The human review step (Stage 6) is the backstop** for fuzzy residue — selection need not be perfect.

---

## 3. Architecture: generate a feasible frontier, LLM picks the point

The cost / time / waste / quality trade-off is a **Pareto frontier** of feasible week-plans. The LLM's *only* irreducible job is choosing **where on that frontier this user sits**, given their fuzzy natural-language priorities.

```
load → filter+narrow → GENERATE feasible frontier → LLM picks the point → validate → persist
       [code]            [code: greedy/solver]        [LLM: 1 call]        [code]    [code]
```

- **Code** builds a handful of complete, feasible week-plans spanning the trade-off space.
- **The LLM** picks the plan that best fits the user's priorities (plus at most one swap).

This eliminates, by construction:
- the budget **repair loop** (every generated plan is already feasible),
- **LLM arithmetic** (it compares pre-computed totals),
- **cost-band hacks** (each plan has a real computed cost),
- **hallucinated/invalid selections** (it picks from valid plans),
- **per-constraint branching** (the generator takes all constraints jointly).

---

## 4. Factor classification (what is code vs LLM)

| Factor | Has a formula? | Owner |
|---|---|---|
| Coverage (fill the week) | yes, trivial — `Σ servings/household ≥ slots` | code |
| Cost of a set | yes — union of missing ingredients × price (hard, non-additive) | **code/solver** |
| Total time | yes — additive sum of per-recipe times | code |
| Ingredient synergy | yes — *is* the union-cost formula | code/solver |
| Recency / repetition | yes — penalty on `last_cooked_at` / `times_cooked` | code |
| Nutrition vs target | yes (once the target is numeric) | code |
| Variety | metric: yes (distinct proteins/cuisines, entropy) / "samey feel": no | code metric + LLM residue |
| Preference match | tag/cuisine overlap: yes / taste & mood: no | code proxy + LLM residue |
| **Trade-off weighting** | **no — none correct for everyone** | **LLM** |
| **Coherence as a week** | **no** | **LLM** |

The bottom two rows are the LLM's entire job. Everything above is code (a solver where the math is hard).

---

## 5. Inputs

### A. Request parameters (API / frontend)
- `slots_needed` — dinners to plan (default 7)
- `week_start` — date the plan covers
- `priority_profile` — natural-language priorities (or null → inferred in Step 4b)
- `budget_cap` — hard ceiling, optional
- `delivery_preference` — pickup/delivery (carried downstream; not used in selection)

### B. User context (`user_preferences`, `user_addresses`)
- `household_size` → coverage math
- `weekly_budget` → soft signal (distinct from hard `budget_cap`)
- `dietary_tags` → **hard filter**
- `allergies` → **hard filter**
- `cuisines` → soft preference
- `zip` → pricing/store (downstream)

### C. Pantry (`pantry_items` + `pantry_items_with_freshness`)
- `ingredient_id` (NOT NULL), `quantity`, `unit`
- `expiry_date`, `freshness_status`
- `is_active = true`

### D. Recipe pool — saved set (`user_recipe_interactions.action='saved'`)
- `recipes`: `servings`, `cuisine`, `difficulty`, `prep_time_mins`, `cook_time_mins`, `title`, `calories/protein/carbs/fat`, `times_cooked`, `last_cooked_at`
- `recipe_ingredients`: `ingredient_id` (nullable), `quantity`, `unit`, `is_optional`
- `recipe_tags`
- `user_recipe_interactions.created_at` (save recency)

### E. Pricing reference (derived)
- coarse per-ingredient cost — aggregated from `receipt_line_items.price` / `order_items.price` history, or seeded. **For ranking only; live prices come at Stage 4.**

### F. Ingredient resolution (`ingredient_aliases`, `ingredients.embedding`)
- pantry↔recipe matching + overlap

### G. Config constants
- expiry window (e.g. 3 days), variety caps (e.g. ≤2 same protein/cuisine), `K` (frontier size), model id, effort

---

## 6. The flow

### Step 0 — Load context · `[code, parallel DB]`
Pull B, C, D, E, F in parallel.
→ state: `pantry`, `prefs`, `recipe_pool`, `price_ref`

### Step 1 — Filter + narrow · `[code]`
- **AND all per-item hard filters**: allergies, dietary lock, max-time-per-recipe, disliked ingredients → safe pool.
- Score & narrow generously to ~15–25 candidates (cheap; recall over precision).
- Per candidate compute the formula facts: `coverage`, standalone `cost_to_add` ($, kept in code), `expiring_rescued`, `preference_score`, `time`, `recency`.
→ state: `candidates`; flag `insufficient_pool` if < slots after filtering.

### Step 2 — Generate the feasible frontier · `[code — the optimizer]`
Produce **K diverse feasible full-week plans**. Each plan:
- satisfies **all** hard aggregate caps (budget, and any others),
- fills `slots_needed` (coverage),
- optimizes a different blend: `cost-lean`, `time-lean`, `waste-lean`, `quality-lean`, `balanced`.

**Diversity guarantee:** dedupe; require each plan differ from the others by ≥2 recipes; if two weightings collapse to the same plan, drop and perturb. (If greedy can't produce spread, that's the trigger to move to CP-SAT — see §10.)

Code pre-assigns days by effort (high-effort → weekend).

Each plan carries a computed summary:
```json
{
  "plan_id": "p_cost",
  "recipes": ["r_9002", "r_7741", "r_8821"],
  "total_cost": 67.40,          // true de-duplicated union cost
  "total_time": 145,            // minutes across the week
  "waste_rescued": 2,           // expiring items used
  "variety_score": 0.78,        // diversity metric
  "to_buy": [ ... ],            // cached ingredient union (so re-measure is free)
  "distinguishing_note": "cheapest; 2 chicken dishes; uses spinach"
}
```
→ state: `frontier` (list of plans), `frontier_status` ∈ `feasible | infeasible | insufficient_pool`

### Step 3 — LLM picks the point · `[LLM — the one call, structured output]`
**Input:** user NL priorities + priority vector + the K plan summaries + the candidate pool.
**Output (structured):**
```json
{
  "plan_id": "p_balanced",
  "day_reassignment": { "r_8821": "Sat" },   // optional
  "swap": { "out": "r_7741", "in": "r_5520" }, // optional, at most ONE, from the candidate pool
  "reason": "Organic/variety lean per your priorities; $9 over cheapest but 4 cuisines."
}
```
The LLM **compares finished numbers** (no arithmetic), **cannot pick an invalid plan**, and may propose **at most one swap** to fix the fuzzy "samey" residue.

### Step 4 — Validate · `[code]`
- If a swap was proposed: re-check feasibility (all hard caps). Feasible → apply and re-measure. Infeasible → ignore the swap, keep the plan. **Bounded, no loop.**
- Re-measure the final `to_buy` + `true_cost` (authoritative number, from code).

### Step 5 — Persist · `[code]` (existing tables)
- `meal_plans`: `status='draft'`, `week_start`, `budget_target=budget_cap`, `budget_actual=true_cost`, `generated_by='ai'`.
- `meal_plan_items`: one per pick (`recipe_id`, `day_of_week`, `meal_type='dinner'`).
→ out: `meal_plan_id`, `to_buy` (→ Stage 3/4), `reason` + plan summaries (→ UI), flags.

> `meal_plans.status` `draft → active` (flipped by Stage 6 review) is the pending-state mechanism — **no separate `pending_carts` table needed.**

---

## 7. Constraint composition (multiple constraints at once)

Real users carry several hard constraints **and** several soft preferences simultaneously. They compose without per-factor branches:

- **Soft preferences (any number):** the single LLM call weighs them jointly via the priority profile/vector. More preferences = more context, not more branches.
- **Per-item hard constraints (any number):** AND all filters in Step 1 (intersection).
- **Aggregate hard caps (budget, total time, nutrition):** all funnel into the **one** generator (Step 2) and the **one** validation (Step 4).

**Conflict resolution rule:** soft always yields to hard. The LLM optimizes preferences *within* the feasible region code defines; it can never violate a hard cap.

The generator's *internal algorithm* scales with the number of hard **aggregate** caps (see §10) — the flow does not change.

---

## 8. Edge cases & fallbacks

| Case | Handling |
|---|---|
| **Infeasible** (no plan meets hard caps) | return closest + flag; surface to user: "can't fill N dinners under $X — drop a night or raise budget" |
| **Insufficient pool** (saved recipes < slots) | partial fill + flag "save more recipes" |
| **No `ANTHROPIC_API_KEY`** | deterministically pick the `balanced` plan — feature works headless |
| **Diversity collapse** (plans too similar) | min-distance dedupe in Step 2; if unfixable, trigger CP-SAT |
| **Invalid LLM output** | structured output prevents schema errors; an out-of-set `plan_id`/`swap` is rejected in Step 4 |

---

## 9. ADK mapping

`SequentialAgent` (fixed pipeline — **not** an LLM deciding the order):

```
SequentialAgent(
  load_context,        # tool → ToolContext.state
  build_candidates,    # tool (Step 1)
  generate_frontier,   # tool (Step 2)  — code: greedy ×K + dedupe
  pick_plan,           # LlmAgent(output_schema=PlanChoice)  ← only LLM node
  finalize_plan,       # tool (Steps 4–5: validate swap, re-measure, persist)
)
```

- All steps except `pick_plan` are plain tools sharing `ToolContext.state` (large payloads never enter LLM context).
- `pick_plan` is the **only** model call — `LiteLlm("anthropic/claude-sonnet-4-6")`, `output_schema=PlanChoice`.
- **No `LoopAgent`, no model-decided sequencing, no model-decided tool routing.**
- Conditional optimizer choice (greedy vs solver) is a plain `if` inside `generate_frontier`, not an LLM router.

### LLM call details
- Model: `claude-sonnet-4-6` (multi-criteria choice over K plans is well within Sonnet; matches existing codebase usage). Opus 4.8 available if quality later demands it.
- Use **structured outputs** (`output_schema` / `messages.parse`), **not** ```` ```json ```` fence-stripping. (Existing `analyze_missing_items` / `_claude_refine_cart` calls should migrate to this too, and drop any `budget_tokens` — deprecated on Sonnet 4.6; use `thinking={"type":"adaptive"}`.)
- Token budget: ~K plan summaries (~150 tok each) + candidate pool + priorities ≈ a few k input. One call.

---

## 10. Build path (don't over-build)

1. **v1 generator — multi-objective greedy.** Run a synergy-aware greedy (marginal-cost recompute after each pick) ~5× with different objective weightings + dedupe. Cheap, instant. Covers the realistic default: **1 hard aggregate cap (budget) + many per-item filters + many soft prefs.**
2. **Upgrade to CP-SAT / OR-Tools** *only* when:
   - a user stacks **multiple hard aggregate caps** (budget *and* weekly-time *and* nutrition), or
   - greedy can't produce a genuinely diverse frontier.
   Same contract ("K feasible plans"), nothing downstream changes.
3. **Proxies first.** variety = diversity score; preference = tag overlap. Escalate to LLM nuance only where the proxy visibly fails. Human review is the backstop.

---

## 11. Open risks

- **Plan diversity quality (primary).** The design hinges on Step 2 producing K plans that genuinely span the trade-off space. If greedy variants converge, the LLM's choice becomes cosmetic. Mitigation: min-distance dedupe; empirically eyeball frontiers on real data; move to CP-SAT if spread is poor.
- **Recipe-ingredient linkage completeness.** `recipe_ingredients.ingredient_id` is nullable (recipes parsed from URLs/OCR may be unlinked). Cost/overlap/shopping math degrade to name matching where unlinked. Mitigation: alias + embedding backfill (offline). **Verify** `pct_linked` before relying on exact matching.
- **Price reference cold-start.** No ingredient-level reference price exists yet; derive from `receipt_line_items` / `order_items` history. New users have no history → seed, or rank without cost until data accrues.
- **Unit normalization.** "1 cup" vs "200 g on hand" needs conversion (`ingredients.gram_weight` helps). v1: treat "have any" as "have it"; review catches shortfalls.

---

## 12. Schema dependencies (verified against `draft_schema.sql`)

Already present (no new tables needed for v1):
- `recipes.servings` ✅ (coverage math)
- `recipes.difficulty`, `prep_time_mins`, `cook_time_mins` ✅ (effort)
- `recipes.cuisine` + `recipe_tags` ✅ (preference/variety)
- `pantry_items.ingredient_id` **NOT NULL** ✅ (matching base)
- `ingredient_aliases`, `ingredients.embedding vector(384)` ✅ (fuzzy matching)
- `meal_plans` + `meal_plan_items` ✅ (output persistence; includes `budget_target`, `budget_actual`, `generated_by`, `day_of_week`, `meal_type`)
- `meal_plans.status` draft→active ✅ (pending-state mechanism)

Possible additions later:
- `ingredient_substitutions` (accretes from Stage 3 cached LLM resolutions)
- ingredient-level price reference (view over receipt/order history)
- natural-language priority profile field on `user_preferences` (v1 can infer)

---

## 13. Decision log (why it is this way)

- **No LLM orchestrator.** Sequencing fixed tools is deterministic; an orchestrator LLM only adds cost + step-skipping risk. Use `SequentialAgent`.
- **No explicit weights.** Stage 1 narrows via union-of-top-K (axis-based, weightless). The trade-off is the LLM's job, steered by NL priorities — not a tuned weighted sum.
- **No per-constraint / per-priority branches.** Filters AND; one generator takes all aggregate caps; one LLM weighs all soft prefs. Branch-per-factor is the over-engineering trap.
- **LLM does no arithmetic.** Cost is non-additive (shared ingredients) — even perfect summing of per-recipe costs is wrong. Only code computes the true union cost.
- **"Hard" → solver, not LLM.** Complicated-but-definable math is the solver's job and the LLM's weakness. Only undefinable judgment (trade-off weighting, coherence) goes to the LLM.
- **One LLM call, no repair loop.** Pre-generating feasible plans removes the need to converge on the budget via re-prompting.
- **Single feasibility seam.** greedy now, CP-SAT later, same "K feasible plans" contract — swappable without touching the rest of the flow.

---

## 14. Next implementation steps

1. `generate_frontier` — synergy-aware greedy ×K + diversity dedupe; emits plan summaries.
2. `PlanChoice` schema + `pick_plan` `LlmAgent` (structured output).
3. `finalize_plan` — validate swap, re-measure true cost, persist to `meal_plans` / `meal_plan_items`.
4. Per-factor formulas (cost union, variety score, recency penalty, preference proxy) — the generator's objective.
5. Verify `recipe_ingredients.ingredient_id` linkage rate; backfill via alias+embedding if low.
