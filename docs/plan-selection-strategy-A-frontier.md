# Plan Selection — Detailed Design

**Status:** Design, iterable. This is the crucial core of the Smart Grocery feature; expect to refine the formulas and the generator as we test on real data. Sections marked **⚠ improvable** are the most likely to change.

**Scope:** *Only* the selection of which recipes make up a user's week.
- **Inputs** arrive from upstream (loaded context + a filtered candidate pool).
- **Output** is a chosen, day-assigned, feasible plan + its shopping list, handed downstream.
Everything else (pantry loading, Kroger pricing, cart, review UI) is out of scope here.

---

## 1. The one principle

> Formalize everything that has a correct formula — however hard (use a solver if needed). Hand the LLM **only** what has no correct formula for anyone.

Three buckets:
- **formula, easy** → code
- **formula, hard** (combinatorial / non-additive) → **solver, still code** (LLMs are worst at this)
- **no formula exists** (trade-off weighting, "coherence") → **LLM**

**Hard ≠ fuzzy.** "Complicated" → better algorithm. "Undefinable" → LLM. The LLM never does arithmetic and never produces the number the user sees.

---

## 2. Architecture in one line

The cost/time/waste/quality trade-off is a **Pareto frontier** of feasible week-plans. **Code generates a few diverse feasible plans; the LLM picks the point on the frontier that fits the user's fuzzy priorities.**

```
candidates ──▶ GENERATE feasible frontier ──▶ LLM picks a plan ──▶ validate ──▶ output
  [code]          [code: greedy / solver]        [LLM: 1 call]      [code]
```

Consequences (all by construction): no budget repair loop, no LLM arithmetic, no cost-band hacks, no invalid/hallucinated selections, no per-constraint branching.

---

## 3. Data objects (notation)

**Candidate recipe `r`** (from the upstream filtered pool, ~15–25):
- `cov(r)` — slots it fills (defined below)
- `ingredients(r)` — set of `(ingredient_id, qty, unit)`
- `t(r)` — `prep_time_mins + cook_time_mins`
- `cuisine(r)`, `protein(r)`, `tags(r)`, `macros(r)`
- `last_cooked(r)`, `times_cooked(r)`

**Pantry `P`** — ingredient_ids on hand (+qty); `E ⊆ P` the expiring subset (freshness/expiry within window).

**Globals** — `H` household size, `N` slots_needed, `price(i)` coarse $ per ingredient.

**Hard constraints**
- per-item filters (allergy, dietary lock, max-time-per-recipe, disliked ingredients) — *already applied upstream to form the candidate pool*
- aggregate caps — `budget_cap`, optional `time_cap`, optional `nutrition_target`
- variety cap — e.g. ≤2 recipes sharing a protein

**Soft preferences** — `priority_profile` (NL) + `priority_vector` `{cost, time, waste, variety, quality} ∈ {high,med,low}`.

---

## 4. Per-factor formulas (the generator's objective)

All terms normalized to `[0,1]` over the candidate set's observed range so weights are comparable. **⚠ improvable:** normalization scheme and the exact variety formula are first cuts.

### Coverage
```
cov(r)  = max(1, round(servings(r) / H))
A plan S is slot-complete  ⇔  Σ_{r∈S} cov(r) ≥ N
```

### Cost of a set — the non-additive one
```
need(S)  = ( ⋃_{r∈S} ingredients(r) )  −  pantry_have      # union, then subtract pantry
cost(S)  = Σ_{i ∈ need(S)} price(i) · qty_needed(i, S)
```
Shared ingredients are counted **once** ⇒ `cost(S) ≤ Σ_r cost({r})`. This is why cost can't be summed per-recipe, and why the LLM must never compute it.

**Marginal cost** (drives greedy):
```
Δcost(r | bought) = Σ_{i ∈ ingredients(r) − bought} price(i) · qty_needed(i)
```
i.e. only the *new* ingredients `r` adds beyond what's already being bought. After picking `r`, `bought ∪= ingredients(r)`, so the next recipe's shared ingredients become free.

> v1 `pantry_have` / `qty_needed` use **"have any = covered"** (ignore quantity). **⚠ improvable:** quantity-aware matching with unit conversion via `ingredients.gram_weight` later.

### Waste rescue (a *benefit*, not a cost — rescued items are already owned)
```
rescued(S) = Σ_{p ∈ E used by some r∈S}  value(p)
value(p)   = price(p) · qty(p) · urgency(days_left(p))     # urgency↑ as days_left↓
```

### Time (additive)
```
time(S) = Σ_{r∈S} t(r)
```

### Variety (a *set* property) — **⚠ improvable**
```
variety(S) = α · cuisineEntropyNorm(S)        # normalized Shannon entropy of cuisine mix
           + β · proteinDiversity(S)          # distinct proteins / picks
           − γ · repeatPenalty(S)             # soft penalty for near-dupes
```
The hard "≤2 same protein" is a *constraint* (checked in feasibility), not part of this score.

### Recency penalty (per recipe; discourages monotony)
```
recencyPen(r) = w1 · [cooked within last D days] + w2 · min(times_cooked(r), cap)/cap   # lower = better
```

### Preference proxy (per recipe; the formalizable part of "do they want it")
```
pref(r) = overlap( tags(r) ∪ {cuisine(r)},  user_cuisines ∪ user_soft_dietary )  ∈ [0,1]
```
(Hard dietary/allergy already removed upstream.) The *unformalizable* part of preference — taste, mood, "samey feel" — is left to the LLM.

### Plan objective (parameterized by a weighting `w`)
```
score_w(S) =  w.pref  · mean_r pref(r)
            + w.var   · variety(S)
            + w.waste · rescuedNorm(S)
            − w.cost  · costNorm(S)
            − w.time  · timeNorm(S)
            − w.rec   · mean_r recencyPen(r)
  subject to:  slot-complete,  cost(S) ≤ budget_cap,  time(S) ≤ time_cap,
               variety cap,  (nutrition_target if set)
```

---

## 5. The generator — synergy-aware greedy, run K times

### Single greedy pass (one weighting `w`)
```
greedy(w):
    S = ∅;  bought = pantry_have
    while coverage(S) < N:
        best = argmax over r ∈ C\S with feasible(S ∪ {r}) of:
                 w.pref  · pref(r)
               + w.var   · Δvariety(r | S)
               + w.waste · Δrescued(r | S, bought)
               − w.cost  · Δcost(r | bought)        # ← marginal, synergy-aware
               − w.time  · timeNorm(r)
               − w.rec   · recencyPen(r)
        if best is None:                # nothing keeps it feasible
            return backtrack_or_fail(S)
        S.add(best);  bought |= ingredients(best)
    return S
```
`feasible(S ∪ {r})` prunes as it goes: projected `cost(bought ∪ ingredients(r)) ≤ budget_cap`, time cap, variety cap. This keeps every returned plan within all hard caps.

**Backtrack (one attempt):** if no feasible recipe completes coverage, swap the highest-`Δcost` pick already in `S` for the cheapest feasible alternative, then continue. If still stuck → mark this weighting's plan infeasible. **⚠ improvable:** this is a deliberately simple heuristic; a real solver replaces it (see §10).

### Frontier (diverse plans)
```
presets = {
  cost:     w heavy on −cost
  time:     w heavy on −time
  waste:    w heavy on +waste
  quality:  w heavy on +pref/+var
  balanced: even
}
plans = []
for w in presets:
    S = greedy(w)
    if feasible(S) and min_recipe_distance(S, plans) ≥ 2:   # symmetric-diff of recipe sets
        plans.append(summarize(S))
# collapse guard: if < 3 distinct plans, perturb weights / add a no-good cut excluding an
# existing plan's signature and re-run a few times.
```

### Plan summary (what each plan carries downstream)
```json
{
  "plan_id": "p_balanced",
  "recipes": ["r_9002","r_7741","r_8821"],
  "day_assignment": {"r_8821":"Sat", "...":"..."},   // code pre-assigns: high-effort → weekend
  "total_cost": 67.40,        // true union cost (authoritative)
  "total_time": 145,
  "waste_rescued": 2,
  "variety_score": 0.78,
  "to_buy": [ /* cached ingredient union, so re-measure is free */ ],
  "note": "balanced; 4 cuisines; uses spinach before it spoils"
}
```

### Status the generator must return
- `feasible` — ≥1 valid plan produced (normal)
- `infeasible` — no plan meets the hard caps (e.g. budget too low for N dinners) → return closest + flag
- `insufficient_pool` — candidates can't fill N slots → partial + flag

---

## 6. The LLM call — pick the point

**Model:** `claude-sonnet-4-6` (choosing among K complete plans is well within Sonnet; matches codebase). **Structured outputs** (`output_schema` / `messages.parse`) — never ```` ```json ```` fence-stripping. No `budget_tokens` (deprecated on 4.6; use `thinking={"type":"adaptive"}` if any).

**Input (compact):**
```json
{
  "slots_needed": 7,
  "household_size": 2,
  "priority_profile": "Organic/quality first, then variety. Budget flexible. Quick weeknights.",
  "priority_vector": {"cost":"low","time":"med","waste":"med","variety":"high","quality":"high"},
  "plans": [ <plan summaries from §5> ],
  "candidate_pool": [ <id + title + key fields, for an optional single swap> ]
}
```

**Instruction (essence):** "Each plan is complete and within all hard limits. Pick the one whose trade-offs best match the user's priorities. You may reassign days and propose at most ONE swap from the candidate pool. Do not compute totals — they're given. Explain your choice."

**Output (schema):**
```json
{
  "plan_id": "p_quality",
  "day_reassignment": {"r_8821": "Sat"},          // optional
  "swap": {"out": "r_7741", "in": "r_5520"},      // optional, at most one, from candidate_pool
  "reason": "Variety+organic lean per priorities; $9 over cheapest but 4 cuisines & rescues spinach."
}
```

The LLM **compares finished numbers** (no arithmetic), **can't pick an invalid plan**, and the single swap lets it fix the fuzzy "these two feel samey" residue without unbounded freedom.

---

## 7. Validate & finalize · `[code]`

```
plan = frontier[choice.plan_id]
apply day_reassignment
if choice.swap:
    S' = (plan.recipes − {out}) ∪ {in}
    if feasible(S') and slot-complete(S'):  plan = recompute(S')   # re-measure cost/caps
    else:                                   ignore swap            # keep original plan
true_cost, to_buy = remeasure(plan)        # authoritative, code-computed
```
No loop: at most one swap, validated once, fall back to the feasible plan if it doesn't hold.

**Output of Stage 2:** chosen plan (recipes + day assignment), `to_buy`, `true_cost`, `reason`, plan summaries (for UI), status flags. Persistence detail (which tables) is handled at the pipeline boundary, not here.

---

## 8. How multiple constraints compose (no per-factor branches)

- **Soft preferences (any number):** the one LLM call weighs them jointly. More prefs = more context, not more branches.
- **Per-item hard constraints (any number):** AND'd upstream into the candidate pool (intersection).
- **Aggregate hard caps (budget/time/nutrition):** all enforced inside the **one** generator's `feasible()` and re-checked in finalize.
- **Conflict rule:** soft yields to hard — the LLM optimizes preferences only *within* the feasible region code defines.

Only the generator's *internal algorithm* scales with the number of hard aggregate caps (greedy → solver, §10); the flow is unchanged.

---

## 9. Edge cases & fallbacks

| Case | Handling |
|---|---|
| Infeasible (no plan within caps) | return closest + flag → user drops a night or raises budget |
| Insufficient pool | partial fill + flag "save more recipes" |
| No `ANTHROPIC_API_KEY` | deterministically return the `balanced` plan — works headless |
| Diversity collapse | min-distance dedupe + perturb/no-good-cut; if still poor → trigger solver |
| Bad LLM output | structured output prevents schema errors; out-of-set `plan_id`/`swap` rejected in finalize |

---

## 10. Build path

1. **Now:** synergy-aware **greedy ×K** + dedupe. Trivial cost (|C|≈25, ~7 picks, ~5 presets → a few hundred cheap set-ops, sub-millisecond) + **one LLM call**. Covers the realistic default: 1 hard aggregate cap (budget) + per-item filters + many soft prefs.
2. **Upgrade to CP-SAT / OR-Tools** *only* when (a) a user stacks **multiple hard aggregate caps**, or (b) greedy can't produce a diverse frontier. Same contract ("K feasible plans") → nothing else changes.
3. **Proxies first** (variety score, preference overlap); escalate to LLM nuance only where a proxy visibly fails.

---

## 11. Complexity & cost

- Generator: `O(presets · picks · |C|)` marginal evaluations over small sets — effectively instant.
- LLM: one call, ~K plan summaries + pool ≈ a few k tokens in, tiny out.
- No loops over the model.

---

## 12. ADK mapping

`SequentialAgent`: `generate_frontier` (tool, code) → **`pick_plan` (`LlmAgent`, `output_schema=PlanChoice`)** → `finalize_plan` (tool, code). Data flows via `ToolContext.state`; the LLM only ever sees plan summaries. No `LoopAgent`, no model-decided sequencing, no model-as-router.

---

## 13. Open questions / where this can improve

These are known soft spots — revisit as we test:

1. **Frontier diversity (highest risk).** Does greedy ×K actually span the trade-off space, or do presets collapse to near-identical menus? If collapse is common, the LLM's choice is cosmetic → move to CP-SAT enumeration of Pareto-diverse solutions. *Validate empirically on real users first.*
2. **Normalization of objective terms.** Per-candidate-range normalization is a first cut; it makes weights comparable but can distort when the candidate set is skewed. Consider global/historical normalization.
3. **Variety formula.** Entropy + protein-diversity is a guess. May need a learned or embedding-based "samey" measure rather than cuisine/protein tags.
4. **Quantity-aware matching.** v1 treats "have any" as covered. Real "do I have enough" needs unit conversion (`gram_weight`) — affects both cost and waste accuracy.
5. **Greedy backtrack.** The single-swap backtrack is a heuristic; tight budgets may need the solver sooner than expected.
6. **Should the LLM get more than one swap?** One keeps it bounded and safe, but may be too restrictive for fixing a genuinely poor frontier. Could allow up to *k* swaps with code re-validation if testing shows the frontier is often "almost right."
7. **Priority vector derivation.** How `priority_profile` (NL) → `priority_vector` and → preset weights is unspecified here; it likely deserves its own small spec (and may itself be a tiny LLM classification or a deterministic mapping).

---

*This is a starting design, not a final one. If a formula proves wrong or a step proves brittle in practice, re-open it — the architecture (generate feasible frontier → LLM picks the point) is the stable part; the formulas and the generator internals are meant to evolve.*
