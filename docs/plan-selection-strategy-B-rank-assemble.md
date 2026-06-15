# Plan Selection — Strategy (revised)

**Status:** Current best strategy for the per-week selection. Revises the *mechanism* and the *output shape* of `docs/plan-selection-strategy-A-frontier.md`; the feasibility/formula details there still hold. Iterable — validate on real metrics.

**Why this revision:** re-examining the earlier "generate K feasible plans → LLM picks one" against `meal-planning-problem-statement.md` surfaced three changes that better serve the success metrics (Accepted, Executed, Safe, Improving):
1. **Output should be a flexible bundle, not a rigid schedule.**
2. **Mechanism should be rank-and-assemble, not generate-K-and-pick** (avoids the frontier-collapse risk).
3. **Selection should be wrapped in light interaction** (for acceptance + learning signal).

The spine is unchanged: **code owns feasibility and all numbers; the LLM owns preference judgment; one LLM call in the hot path; the whole thing wrapped in the learning loop.**

---

## 1. Output — a flexible weekly bundle

We do **not** output a rigid Mon–Sun schedule. We output:

- **A committed set of ~N meals** for the week + their **consolidated grocery list**. This is the part that gets *ordered* — grocery delivery forces us to batch-commit the shopping.
- **A soft, swappable suggested rhythm** (which night, by effort — elaborate on the weekend). **Non-binding.**

**Why:** grocery delivery forces committing the *shopping* in a batch, but **not** the *schedule*. A rigid schedule is brittle — it breaks the moment Tuesday goes sideways, and cook-through (the *Executed* metric) collapses with it. A flexible bundle:
- survives real-life deviation (serves *Executed* + fit-to-life),
- still offers a suggested rhythm for those who want zero decisions (serves decision-fatigue),
- lowers the stakes of getting the selection exactly "right" — it's a **set**, not a schedule.

> This also retires the day-assignment / effort-distribution work as a *hard* output — it becomes a soft suggestion, which is less to over-engineer.

---

## 2. Mechanism — rank-and-assemble (primary path)

**Step 1 · Candidate pool** `[code, upstream]`
Filtered, feasible recipes (per-item hard filters already applied), each with computed facts: `cost_to_add`, `expiring_rescued`, `effort`, and preference signals drawn from the **user model**.

**Step 2 · LLM ranks the pool** `[LLM — the one call]`
The LLM outputs the candidate recipes **in preference order** for *this household*, using the user model + any priorities stated for this week. More recipes than needed; pure judgment; **no math**. (Optionally grouped: must-have / good / filler.)

**Step 3 · Code assembles a feasible bundle** `[code]`
Walk the ranking; take the next most-preferred recipe that keeps the bundle feasible; stop when coverage is met:
```
bundle = []; bought = pantry
for r in llm_ranking:                       # most-preferred first
    if coverage(bundle) >= slots: break
    if feasible(bundle + r):                # budget, variety cap, etc.
        bundle.append(r); bought |= ingredients(r)
    # else skip r, continue down the ranking
# cost/synergy tie-break: among near-equal-rank candidates, prefer cheaper / shared-ingredient ones
```
Feasibility (budget cap, variety cap, coverage) is enforced **by code as it assembles** — the LLM never computes anything and the result is always feasible.

**Step 4 · Code finalizes** `[code]`
Consolidated grocery list = ingredient union − pantry (live prices at order time); soft suggested rhythm (effort → days); the committed bundle.

**Result:** one LLM call (ranking), feasibility guaranteed by assembly, **no frontier-collapse risk** (not bounded by K pre-baked plans), flexible output.

---

## 3. Variants — the shrunken "K" (only when the trade-off is live)

The one thing generate-K did better was let the LLM **knowingly choose the cost/quality trade-off** from complete costed options. We keep that, but **only when it's actually needed**:

- If the household's cost tolerance is ambiguous, **or** assembly hits the budget cap and must drop preferred meals → code produces **2–3 bundle variants at different cost points** (e.g. "cheaper / balanced / premium").
- Then either the **LLM picks** the variant matching the user model's cost tolerance, or the **2–3 are surfaced to the user** as a light choice.

So "K" collapses from "always 5 plans" to "**2–3 variants, only when the trade-off is genuinely live.**" Most weeks: a single assembled bundle.

---

## 4. Wrapped in interaction (for Accepted + Improving)

The bundle is a **conversation-starter, not a finished deliverable**:

- Present **one** proposed bundle (decision fatigue → don't make them choose among many).
- Frictionless reactions: **swap a meal**, **"not fish this week"**, **"love it"** — 2–3 taps.
- **Every reaction is a first-class learning signal** → fed to the Personalization Agent.

Co-creation raises *Accepted*; reactions raise *Improving*. A swap is the strongest preference signal we get — treat it as such.

---

## 5. Ownership (the boundary — unchanged)

| Job | Owner |
|---|---|
| Rank recipes by preference; interpret reactions | **LLM** (judgment, no math) |
| Feasibility, assembly, cost, grocery list, suggested rhythm | **code** |
| Hard constraints (allergy/dietary/budget) | **code** (inviolable) |

The LLM expresses preference richly (a full ranking); code enforces feasibility and computes every number.

---

## 6. Edge cases & fallbacks

| Case | Handling |
|---|---|
| Insufficient pool (can't fill N) | assemble what's feasible + flag "save more recipes" |
| Infeasible budget | produce the closest cheaper variant + surface "raise budget or drop a meal" |
| Cold start (no user model yet) | rank by stated prefs + popularity; lean on the interaction to learn fast |
| No `ANTHROPIC_API_KEY` | deterministic rank by a simple preference score; assemble as normal |

---

## 7. What this revises vs. `plan-selection-strategy-A-frontier.md`

- **Output:** rigid Mon–Sun schedule → **flexible bundle + soft rhythm**.
- **Mechanism:** generate-K-frontier → LLM-picks → **LLM-ranks → code-assembles**; generate-K **demoted** to "variants, only when the trade-off is live."
- **Adds:** the interaction/signal wrapping.
- **Day assignment:** core output → **soft, swappable suggestion**.

Unchanged: code owns feasibility + all numbers; LLM owns preference; one LLM call; reads the user model and emits signals (the learning loop).

---

## 8. Open / validate (don't assume — A/B on real metrics)

- **Does rank-and-assemble beat generate-K** on bundle quality? (Primary mechanism bet.)
- **How often is a variant choice actually needed** — i.e. how often does the cost/quality trade-off genuinely go live?
- **Does the flexible bundle (vs. a schedule) improve cook-through?** Measure *Executed*.
- **Reaction UX:** how light can the swap/react flow be while still producing useful learning signal?
- **Ranking quality vs. cost-blindness:** the LLM ranks without seeing aggregate cost; does code's cost-aware assembly + the variant mechanism honor cost tolerance well enough, or does the LLM need a coarse cost hint per recipe?

---

*Spine is stable: code feasibility + LLM preference + the learning loop. What changed is the output shape (bundle, not schedule) and the mechanism (rank-and-assemble, not generate-K). Both are judgment calls to validate against Accepted / Executed / Improving — re-open them if the data disagrees.*
