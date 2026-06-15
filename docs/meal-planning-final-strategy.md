# Meal Planning — FINAL STRATEGY

**Status: canonical.** This is the decided strategy for "what should the week be," superseding `meal-planning-unified-strategy.md` (draft D) and resolving the A/B/C fork. It is self-contained: implement from this document plus the formula library in `plan-selection-strategy-A-frontier.md` §3–4 (cost-union, marginal cost, rescue value, feasibility), which remains normative for the math. The problem framing, success metrics, and principles in `meal-planning-problem-statement.md` are the constitution this strategy answers to. Re-open decisions only via the kill-criteria in §12.

**The strategy in one paragraph:** Code owns a three-layer household model, all feasibility, and every number. Once a week, one LLM call ranks the feasible recipe pool by preference and flags samey pairings; code assembles a flexible **bundle** — a committed meal set + consolidated grocery list, never a schedule — reserving one slot for an uncertainty-driven **explore probe** and attaching a one-tap **budget trade** when the cap forced something out. The ranking persists as the week's artifact, powering every alternate and mid-week swap with zero further LLM calls. Signals are captured as exhaust from actions the user already takes; an async weekly step turns them into model updates — code for numbers, one LLM reflection call for narrative, with reflection only *proposing* numeric changes that code applies bounded. **LLM budget: 1 sync call + 1 async call per household per week.**

---

## 1. System shape

Two components on different clocks, decoupled through a persisted user model. They never call each other.

```
            ┌────────────── USER MODEL (membrane) ───────────────┐
            │ L1 constraints · L2 features+confidence · L3 narrative│
            └──────▲───────────────────────────────┬──────────────┘
                   │ writes (async, bounded)       │ reads (sync, incl. uncertainty)
      ┌────────────┴───────────┐       ┌───────────▼────────────┐
      │ PERSONALIZATION (async)│       │ PLANNING (sync, weekly) │
      │ store → stats →        │       │ LLM ranks → code        │
      │ weekly LLM reflection  │       │ assembles bundle        │
      └────────────▲───────────┘       └───────────┬────────────┘
                   │ tagged signal exhaust          │ bundle + ranking artifact
                   └──── household reacts / cooks / skips / orders ◀──┘
```

Planning reads the model **including its confidence values** — uncertainty flowing forward is what makes exploration possible without Personalization ever deciding a plan.

## 2. The user model — three layers

**L1 — Constraints. Declared, never learned, code-enforced.** Allergies, dietary locks, budget cap, equipment, per-member hard vetoes. The learning system has **no write path** to this layer — a model that can infer an allergy is a model that can forget one. Enforced upstream as pool filters and re-checked at finalize. Violations are stop-ship bugs, by definition in code, never the LLM.

**L2 — Structured features. Learned, code-owned.**
- `recipe_affinity[recipe_id] → {score, confidence, last_signal_at}` — updated by the signal weights in §6. Unseen recipes get a prior from embedding nearest-neighbors (text embedding over ingredients + cuisine + technique; similarity-weighted average of scored neighbors — arithmetic, no training infra).
- Rhythm: `repetition_tolerance`, `variety_appetite`, quick-meal demand (driven by the week's busy-night count).
- `budget_behavior` — cap strictness + budget-trade take-rate (learned from §4 trade taps).
- Per-member sub-profiles where signals are attributable; household blend otherwise. Household structure lives *here*, not in a separate agent.
- **Confidence + recency on every belief.** Decay keeps the model current; low confidence is the explore slot's input.

**L3 — Narrative. LLM-written, LLM-read.** A short, capped, **versioned** document of distilled observations ("swaps out fish every time," "Sunday is the ambitious night," "says variety, cooks the same three comfort meals on weeknights"). Rewritten weekly by reflection (§7). Contains **no numbers and no constraints** — those live where code enforces them.

## 3. The weekly engine

**Output shape:** a committed set of ~N meals + consolidated grocery list (grocery delivery batch-commits the *shopping*, never the *schedule*) + a soft, non-binding suggested rhythm (effort → free evenings, perishables → early). The rigid-schedule failure mode is deleted, not handled. Users who want the schedule decided get the rhythm rendered as one — a presentation choice, not a planning commitment.

**Step 1 — Pool facts** `[code]`. Upstream-filtered feasible pool (~15–25, L1 applied). Per recipe: marginal `cost_to_add` + coarse **cost band** (low/med/high), `expiring_rescued`, effort minutes, L2 affinity + confidence, novelty/recency.

**Step 2 — Rank** `[the one sync LLM call]`.
- *Input:* the pool with per-recipe facts (cost band and affinity included — never raw prices to sum), L3 narrative, a compact L2 summary, week context (busy-night count, stated cravings, guests).
- *Output (structured schema):* `ranking` (full preference order), `pairing_cautions` (pairs that would feel samey in one week — the set-coherence judgment, made where the LLM sees the whole pool), `rationales` (one line per top pick → the trust UI's "why").
- *Fallback (no API key / call failure):* deterministic rank by L2 affinity. The system works headless.

**Step 3 — Assemble** `[code]`. Walk the ranking; take the next-preferred recipe that keeps the bundle feasible (budget cap, variety cap, coverage — the A-library math), with pairing cautions as soft penalties and ingredient synergy as the tie-break among near-equal ranks. Rhythm is **set-wise**: a week with *k* busy nights gets ≥ *k* quick meals — no day binding. One slot is reserved for the probe (§5). Feasibility holds by construction; the LLM computed nothing.

**Step 4 — Budget trade, when the cap binds** `[code]`. The bundle always ships as **one** proposal. If assembly dropped a high-ranked meal at the cap, the bundle carries an inline, code-computed trade — *"adding the salmon back is +$9.40"* (marginal cost). One tap accepts; the tap or its absence trains `budget_behavior`. The cost/quality trade-off is surfaced only when real, as a single edit-shaped decision, never as parallel plans — and it goes to the user, whose judgment about their own money beats any model's.

**Step 5 — Finalize** `[code]`. Consolidated grocery list (ingredient union − pantry, live prices at order time); soft rhythm; **per-slot alternates** = next-ranked feasible, caution-checked substitutes per meal, cached for the week.

**Why this mechanism (decided, with its escape hatch):** the ranking is a *persistent artifact, not a consumed answer* — one call funds the bundle, every alternate, every mid-week swap, and the budget trade for the whole week. It has no frontier-collapse risk and no diversity machinery. Its known weakness — ranking without seeing aggregate consequences — is covered by cost bands, cost-aware assembly, the budget trade, and pairing cautions (which restore the one judgment frontier-judge uniquely had: set-coherence). The §12 kill-criteria say exactly what evidence would swap the mechanism for frontier-plus-validated-alternates behind the same contract; nothing else would change.

## 4. Interaction (one proposal, reactions as data)

Present **one** bundle. Reactions are 2–3 taps: swap a meal (alternates are precomputed — instant), "not fish this week," "love it," take the budget trade. Every edit is captured **structurally** (removed X, added Y, reason chip if offered) — never free text — because an edit is the single strongest training signal that exists. Rationale lines from Step 2 ride along as the "why" (trust, lever 4, nearly free).

## 5. The explore slot (active learning — the loop's missing arm)

A passive loop only learns about meals it already chose to serve; confidence decay re-opens questions but never answers them. So:

- **Code reserves one slot** for a probe: the highest-*ranked* recipe from the *high-uncertainty* region of L2 (low confidence / few scored neighbors). Uncertainty picks the candidate set; the LLM's ranking picks within it — informative *and* plausible.
- **Quota is governed by model confidence, by code:** 2 during calibration weeks (§9), tapering to 1, re-raised when decay lowers global confidence. A user-facing "adventurousness" toggle can scale it — control is a trust lever.
- **Honestly labeled** in the UI ("something new — tell us"), converting a potential miss into a feature.
- **The signal is probe-tagged** so learning weights it correctly: a cooked-and-rated probe is exactly the high-information event the slot exists to buy; a skipped probe is weak evidence, not a strike.

One extra assembly term plus one tag — no new component, no new call, and Personalization still never decides a plan.

## 6. Signals — exhaust, not homework (the rule that keeps the moat fed)

The loop starves without behavioral data, and any signal that requires a chore will not be produced. **Hard rule: every signal must be a side effect of an action the user already wants to take.** Explicit ratings are optional gravy, never load-bearing.

| Signal | Captured from (exhaust of) | L2 weight (starting point, tunable) |
|---|---|---|
| Swapped out at review / swapped in | the edit itself | −2 / +2 (the strongest signal pair) |
| Explicit "never show" | a blocklist tap | −5 + blocklist |
| Budget trade taken / declined | the trade tap | +1 to meal; trains `budget_behavior` |
| Cooked (proxy) | opening tonight's recipe on the "what's tonight" surface; one-tap "made it" | +2 |
| Cooked + thumbs-up | optional one-tap after "made it" | +3 |
| Accepted unedited | plan confirmation | +1 per meal |
| Present but never opened | absence of the above | −0.5 |
| Mid-week swap to a quick meal | the swap | effort evidence for that night |
| Probe outcomes | probe tag | cooked ×1.5, skipped ×0.5 |
| Ingredients actually ordered | the grocery order | corroborates the committed set |

The "what's tonight" surface matters strategically: it serves the user (decision fatigue, goal #7) *and* is the cook-through sensor. Build it as part of this strategy, not as UI garnish.

## 7. The weekly learning step (async)

1. `[code]` Ingest the week's event log → update L2 affinities, rhythm, budget behavior, confidences per the §6 weights; apply recency decay.
2. `[one LLM reflection call]` Read the event log + current L3. Output (structured): the rewritten **narrative** (capped) + optional **proposals** for L2 — `{field, direction, bounded magnitude, evidence}` — for latent-taste reads no formula captures ("says variety, cooks comfort"). **Code applies proposals with capped step sizes and logs them; reflection never writes numbers directly, and nothing writes L1.** L3 is versioned; a bad distillation rolls back.

That is the entire learning machinery: arithmetic plus one call per household per week.

## 8. Mid-week (instant, code-only in the common case)

The bundle is a set, so "skipped Tuesday" is not a failure — nothing was bound to Tuesday.
- **"Swap tonight"** → cached alternates: one tap, zero latency, zero LLM.
- **Perishables** → soft rhythm orders them early; a skip triggers a code reorder of the *suggestion*.
- **Genuinely novel situations** ("guests turned vegetarian") → one LLM call, same contract: judgment over code-validated options only.
- Every adaptation event is §6 signal. Nobody waits for a model at 6pm.

## 9. Cold start

- Onboarding: L1 intake + a 60-second swipe deck of ~15 recipes spanning the embedding space (each swipe seeds L2 — week 1 ranks from a coarse-but-real model) + optional "name 5 dinners your family loves."
- Weeks 1–3 are **calibration weeks**: explore quota 2, framed as calibration in-product — early misses read as progress, not failure.
- Until L2 has signal, ranking leans on stated preferences + recipe popularity.

## 10. Households (gated)

Per-member L1 vetoes are absolute. L2 keeps per-member affinity where attributable. Code maintains a fairness counter (each member's favorites surface over a rolling window) as a soft assembly term; the ranking prompt sees per-member L3 notes. Explicit negotiation machinery is built **only** when a measured conflict trigger fires (sustained cross-member veto/edit conflict). Structure in the model, never a third agent.

## 11. Edge cases

| Case | Handling |
|---|---|
| Infeasible budget (can't fill N under cap) | closest cheaper bundle + flag: "raise budget or drop a meal" |
| Insufficient pool | partial bundle + flag: "save more recipes" |
| Ranking call fails / no key | deterministic affinity rank; assemble as normal |
| Malformed LLM output | structured schema; unknown ids dropped; cautions/rationales best-effort |
| Diversity/variety degenerate pool | variety cap forces spread; flag if unsatisfiable |
| User ignores soft rhythm entirely | irrelevant by design — only the set was committed |

## 12. Metrics & kill-criteria (what would change this design)

**Headline metric: edits-per-plan slope over household tenure.** If that curve doesn't fall, L2/L3 aren't learning and nothing else matters. Plus: acceptance rate, cook-through (via §6 proxies), week-over-week retention, probe hit-rate, constraint violations (**alarmed, literally 0**).

| Decision at risk | Evidence that re-opens it | Response |
|---|---|---|
| Rank-and-assemble mechanism | Offline pre-launch test: same pool + model → rank-assemble vs frontier-judge bundles; human-judge the diffs. Post-launch: persistent coherence/cost-fit edits despite cautions + trade | Swap to frontier + pre-validated slot alternates; same contract, nothing else moves |
| Pairing cautions | No reduction in samey-swaps with cautions on | Drop them (decoration) |
| Budget trade | Fires nearly every week | Caps set too tight or pool too expensive — fix upstream, not the mechanism |
| Explore slot | Probe hit-rate ≈ random; edits-slope unaffected | Reduce quota; check uncertainty estimates before abandoning |
| Reflection | L3 diffs are noise on eyeball review (it's versioned — review them weekly at first) | Tighten prompt, lower frequency; proposals already bounded so damage is capped |
| Soft rhythm | Decision-fatigue users report wanting it decided | Render rhythm as a schedule view — presentation fix only |

## 13. Build order (signal capture comes early — data is the moat)

- **M0 — Substrate.** A-library formulas, pool facts, feasibility, deterministic-rank assembly, grocery-list finalize. Works headless, testable without any LLM.
- **M1 — The week.** Ranking call (+ cautions, rationales), bundle + budget trade + review UI with structured edit capture, "what's tonight" surface. **Start logging every §6 signal now**, before anything consumes them — week-1 data trains week-10 models.
- **M2 — The model.** L2 updates from the logged signals, embedding priors, cold-start swipe deck, explore slot.
- **M3 — Reflection.** L3 narrative + bounded proposals; calibration-week framing.
- **M4 — Gated.** Household negotiation (on conflict trigger), solver upgrade (on multiple hard caps), richer taste models — each only when its §12 evidence demands it.

## 14. Final decision log

| Decision | Why it won |
|---|---|
| Flexible bundle, soft rhythm (B) | Deletes the schedule-brittleness failure mode instead of handling it; shopping is what delivery batch-commits |
| Rank-and-assemble, one call (B, hardened) | Ranking is a week-long reusable artifact; no collapse risk; weaknesses covered by bands/trade/cautions; falsifiable + swappable per §12 |
| Pairing cautions (new) | Restores LLM set-coherence judgment without a second call or pre-baked plans |
| Single bundle + inline budget trade (new) | B's variants were generate-K in disguise and broke the one-proposal principle; the trade yields the same signal in one tap, and the user outranks the LLM on their own money |
| Tri-layer model, L1 unlearnable (C) | Learning must be structurally unable to corrupt safety |
| Weighted signal exhaust + embedding priors (C, hardened) | Compounding personalization from data users produce for free; no signal may be homework |
| Explore slot, code-quotaed, probe-tagged (C) | An exploit-only loop flatlines "Improving"; this is the cheapest active-learning arm that doesn't break the membrane |
| Reflection proposes, code applies (new) | The LLM reads between the lines; it never holds the pen on numbers |
| Set-wise rhythm (audit) | Day-level commitments were schedule-thinking the bundle obsoleted |
| Households as model structure, negotiation gated (architecture) | Lever 5 before its data is machinery for its own sake |
| A's formulas retained as the math library | Correct, non-additive cost math is the substrate everything stands on |

**Against the constitution:** Accepted — one proposal, rationales, frictionless edits. Executed — nothing bound to a day, instant swaps, quick-meal guarantees. Safe — constraints live only in code, learning locked out of L1, violations alarmed at zero. Improving — weighted exhaust signals, explore probes, bounded reflection, and a headline metric that falsifies the whole bet if it doesn't move. Two LLM calls a week, both squarely in no-formula territory; everything else is arithmetic.
