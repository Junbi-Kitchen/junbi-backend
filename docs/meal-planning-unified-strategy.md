# Meal Planning — Unified Strategy (D)

> **SUPERSEDED** by `meal-planning-final-strategy.md` — the canonical, self-contained final strategy. This draft is kept as the working trail (synthesis + audit) behind it.

**Status:** synthesis of strategies A (frontier), B (rank-assemble), C (portfolio-loop), and `system-architecture.md`, judged against `meal-planning-problem-statement.md`. This makes the calls — each section says what was taken from where, and §10 logs what was *rejected* and why. Supersedes the A/B/C fork as the working strategy; A remains the formula library, and §9 defines the test that could re-open the one contested decision.

**The strategy in one paragraph:** Code maintains a three-layer household model and computes every number. One LLM call per week ranks the feasible recipe pool by preference; code assembles a flexible **bundle** (a committed meal set + grocery list, not a schedule) from that ranking, reserving one **explore slot** for a high-uncertainty probe. The ranking is kept as a persistent artifact that powers everything else in the week — alternates, mid-week swaps, budget trades — with zero further LLM calls. Every reaction, edit, cook, and skip is a weighted signal; an async weekly step updates the model (code for numbers, one LLM reflection call for narrative). Hot path: 1 LLM call. Loop: 1 LLM call per household per week.

---

## 1. Architecture frame (from `system-architecture.md`, unchanged)

Two components on different clocks, decoupled through the user model — Planning reads it, Personalization writes it, they never call each other:

```
            ┌────────────── USER MODEL (membrane) ──────────────┐
            │ L1 constraints · L2 scores+confidence · L3 narrative │
            └──────▲──────────────────────────────┬─────────────┘
                   │ writes (async)               │ reads (sync, incl. uncertainty)
      ┌────────────┴───────────┐      ┌───────────▼────────────┐
      │ PERSONALIZATION (async)│      │ PLANNING (sync, weekly) │
      │ store → stats →        │      │ LLM ranks → code        │
      │ weekly LLM reflection  │      │ assembles bundle        │
      └────────────▲───────────┘      └───────────┬────────────┘
                   │ tagged signals               │ bundle + ranking artifact
                   └──── household reacts / cooks / skips / edits ◀──┘
```

One refinement to the original diagram: the membrane is read **including uncertainty**. Planning consumes the model's confidence values, not just its beliefs — that is what makes exploration (§4) possible without breaking the "Personalization never decides a plan" rule.

## 2. The user model — three layers (C, mapped onto the architecture's schema)

**Layer 1 — Constraints. Declared, never learned.** Allergies, dietary locks, budget cap, equipment, per-member hard vetoes. Enforced exclusively by code as filters. The learning system has no write access to this layer — a model that *infers* an allergy is a model that can forget one. (C's rule, made explicit; the architecture's user model previously didn't separate this.)

**Layer 2 — Structured features. Learned, code-owned.** The architecture's "structured features + confidence/recency," given C's concrete update machinery:
- Per-recipe affinity, Elo/Bayesian-style updates from weighted signals: cooked-and-rated-up > cooked > accepted-unedited > present-but-skipped > swapped-out at review > explicitly rejected. A swap down-weights the removed recipe *and* up-weights its replacement.
- Generalization to unseen recipes via recipe embeddings (ingredients + cuisine + technique): a new recipe's prior is the similarity-weighted average of scored neighbors. Nearest-neighbor arithmetic, no training infra. Replaces tag-overlap `pref(r)` and entropy-variety from A as data accrues (answers A §13.3 and the problem statement's "tags are crude").
- Rhythm parameters: repetition tolerance, per-weekday effort budget (learned from which nights get skipped/swapped), variety appetite, **budget behavior** (learned from budget-trade taps, §3 step 4).
- **Confidence + recency per belief** — decay keeps the model current; *low confidence is the input to exploration* (§4).
- Per-member sub-profiles where signals are attributable; household blend otherwise (architecture: household is structure in the model, not an agent).

**Layer 3 — Narrative summary. LLM-maintained, LLM-consumed.** A short, capped, **versioned** document of distilled observations ("swaps out fish every time," "Sunday is the ambitious night," "kid eats mushrooms blended, not visible"). Rewritten weekly by the reflection pass. Contains no numbers and no constraints — those live in L1/L2 where code enforces them.

## 3. The per-week engine — rank-and-assemble (B), upgraded

**Output shape (B, adopted outright):** a committed set of ~N meals + consolidated grocery list (the part grocery delivery forces us to batch-commit) + a **soft, non-binding suggested rhythm** (effort → free evenings, perishables → early). No rigid Mon–Sun schedule — that brittleness failure mode is deleted, not handled.

**Step 1 — Pool facts** `[code]`. Upstream-filtered feasible pool (~15–25; L1 already applied). Per recipe: marginal `cost_to_add` + a coarse **cost band** (low/med/high), `expiring_rescued`, effort/time, L2 affinity + confidence, novelty/recency. A §4's formulas are the substrate.

**Step 2 — Rank** `[the one LLM call]`. Input: the pool with per-recipe facts (including cost band and affinity — mitigates B's cost-blindness without giving the LLM arithmetic), L3 narrative, an L2 structured summary, this week's context (calendar notes, stated cravings). Output (structured schema): a full preference ranking; **pairing cautions** — pairs of candidates that would feel samey in the same week (the set-coherence judgment, made where the LLM can see the whole pool); and a one-line rationale per top pick (becomes the trust UI's "why", lever 4, nearly free).

**Step 3 — Assemble** `[code]`. Walk the ranking; take the next-preferred recipe that keeps the bundle feasible (budget cap, variety cap, coverage — A's marginal-cost/union math), treating pairing cautions as soft penalties and ingredient synergy as the tie-break among near-equal ranks. Rhythm is enforced **set-wise**: a week with *k* busy nights needs ≥ *k* quick meals in the bundle — no day binding. **One slot is reserved for the explore probe (§4).** Feasibility is guaranteed by construction; the LLM never computed anything.

**Step 4 — Budget trade affordance, when the cap binds** `[code]`. The bundle always ships as **one** proposal. If assembly had to drop a high-ranked meal at the budget cap, the bundle carries an inline, code-computed trade: *"adding the salmon back is +$9.40"* (marginal cost, A's formula). One tap accepts it; the tap — or its absence — is a direct training signal for L2's budget behavior. This surfaces the cost/quality trade-off only when it's real, as a single edit-shaped decision the learning loop already consumes, never as a choice among parallel plans.

**Step 5 — Finalize** `[code]`. Consolidated grocery list (ingredient union − pantry, live prices at order time), soft rhythm, and **per-slot alternates** = the next-ranked feasible, pairing-caution-checked substitutes for each chosen meal, cached for mid-week (§5).

**Why rank-and-assemble won over frontier-judge (A/C):** the deciding argument is that **the ranking is a persistent artifact, not a consumed answer.** One call buys the bundle, the cost variants, every per-slot alternate, and every mid-week swap for the whole week — frontier-judge's output (a choice among K) is spent the moment it's made, and mid-week flexibility would need new machinery. Secondary: no frontier-collapse risk (A's own top risk, §13.1), and simpler (no diversity presets, distance checks, or perturbation loops). The known weakness — the LLM ranks without seeing aggregate consequences — is mitigated three ways (cost bands in the prompt, cost-aware assembly, the budget-trade affordance when the cap binds) and is falsifiable offline before launch (§9). The other thing frontier-judge uniquely offered — set-coherence judgment over a complete week — is recovered by the pairing cautions: the LLM judges samey-ness where it can see everything (the pool), code enforces it where it acts (assembly), and human review remains the backstop. If it falsifies, the fallback is C's frontier + pre-validated-alternates judge behind the same contract; nothing else changes.

## 4. The explore slot (C — the piece nothing else had)

The loop as previously drawn was exploit-only: plan from beliefs → observe → update → plan again. It only ever learns about meals it already chose to serve; confidence *decay* re-opens questions but never answers them. Exploration is the missing read-direction of the membrane.

- **Code reserves one slot per bundle** for a probe: the highest-LLM-ranked recipe from the *high-uncertainty* region of L2 (low confidence / few scored neighbors). Uncertainty picks the candidate set; the ranking picks within it — so probes are informative *and* plausible.
- **Explore budget is governed by overall model confidence:** 2 slots during calibration weeks (§7), tapering to 1, re-raised when decay lowers confidence. Code decides the quota; no LLM involved.
- **Honestly labeled in the UI** ("something new to try — tell us"), converting a potential planning miss into a feature.
- **The signal is tagged as a probe** so reflection weights it correctly: a skipped probe is weak evidence; a cooked-and-rated probe is exactly the high-information signal the slot existed to buy.

This is one extra term in assembly plus one tag on the signal — no new component, consistent with the architecture's "Personalization never decides a plan" and "multi-agent is a means, never the aim."

## 5. Mid-week adaptation (C's machinery, made nearly trivial by B's output)

Because the bundle is a set, "I skipped Tuesday" is not a failure — nothing was bound to Tuesday. What remains:

- **"Swap tonight, I'm exhausted"** → the cached per-slot alternates (next-ranked feasible, from §3 step 5): one tap, zero latency, zero LLM.
- **Perishables** → the soft rhythm orders perishable-consuming meals early; a skip triggers a code-level reorder of the suggestion.
- **Genuinely novel situations** ("guests turned vegetarian") → one LLM call with the same contract: judge over code-validated options only.
- **Every adaptation event is a signal** (a Tuesday swap-to-quick is evidence about Tuesday's real effort budget).

## 6. Interaction wrapping (B) + the learning step (C)

**Review:** present **one** bundle, always — budget trades appear as inline affordances on it, never as parallel plans (decision fatigue is the product). Reactions are 2–3 taps: swap a meal, "not fish this week," "love it." Every edit is captured **structurally** (removed X, added Y), never as free text — it's training data, and an edit is the strongest signal we get.

**Weekly learning step (async, after the week closes):**
1. `[code]` Ingest the event log (acceptance, edits, cooks, skips, ratings, mid-week swaps, budget-trade taps, probe outcomes) → update L2 affinities, rhythm parameters, budget behavior, confidences — the §2 signal weights.
2. `[one LLM reflection call]` Read the event log + current L3 → rewrite the narrative: add distilled observations (including the architecture's "latent taste" reads — *"they say variety but cook the same 3 comfort meals on weeknights"*), drop stale ones, stay under the size cap. Versioned, so a bad distillation rolls back.

That is the entire learning machinery: arithmetic plus one call per household per week.

## 7. Cold start

- Onboarding: L1 intake + a 60-second swipe deck of ~15 recipes spanning the embedding space — each swipe seeds L2, so week 1 ranks from a coarse-but-real model, not zeros. Optional accelerant: "name 5 dinners your family already loves."
- Weeks 1–3 are **calibration weeks**: explore quota of 2, framed in-product as calibration — sets expectations and makes early misses feel like progress.
- Until L2 has signal, ranking leans on stated preferences + popularity (B's fallback row).

## 8. Households (lever 5, gated)

Per-member L1 vetoes are absolute. L2 keeps per-member affinity where attributable. Code maintains a fairness counter (each member's favorites appear over a rolling window) as a soft assembly term; the ranking prompt sees per-member L3 notes. Explicit negotiation machinery is built **only** when a measured conflict trigger fires (sustained veto/edit conflict rate) — structure in the model, not a third agent.

## 9. Validation plan (what could change this design)

| Question | Test | If it fails |
|---|---|---|
| **Is rank-and-assemble cost/set-blind?** (the contested call) | Offline, pre-launch: same pool + model → (a) rank→assemble vs (b) frontier→judge; compare bundles on cost fit, variety, affinity; human-judge disagreements | Swap mechanism to frontier + pre-validated slot alternates (C); same contract, nothing else changes |
| How often does the budget trade fire — and is it taken? | Count §3-step-4 triggers + tap rate; check L2 budget-behavior convergence | If it fires nearly always: budget caps are set too tight or the pool is too expensive — fix upstream before touching the mechanism |
| Do pairing cautions actually reduce "samey" edits? | Compare samey-flavored swap rate with cautions on/off | If no effect: drop them (decoration); if coherence edits persist: that's the signal to reconsider judging complete weeks |
| Does the loop move metrics? | **Headline: edits-per-plan slope over tenure** (cohort curve); plus acceptance rate, cook-through, retention, probe hit rate | Flat slope = L2/L3 not learning; debug reflection quality before adding machinery |
| Is reflection useful or noise? | Eyeball L3 diffs weekly on real signal (it's versioned) | Tighten reflection prompt; reduce rewrite frequency |
| Safety | Constraint violations alarmed; target literally 0; budget-overrun rate | Any violation is a stop-ship bug in code, by definition not the LLM |
| Does the soft rhythm read as "decided" to decision-fatigue users? | Survey + rhythm-follow rate | Offer an opt-in hard schedule view (presentation, not planning, change) |

## 10. Decision log — what was taken, what was rejected

| Decision | Source | Rejected alternative & why |
|---|---|---|
| Flexible bundle + soft rhythm | B | A/C's rigid schedule — brittleness deleted rather than handled |
| Rank-and-assemble, one call | B | A/C frontier-judge — ranking is a reusable week-long artifact; no collapse risk; falsifiable + swappable per §9 |
| Cost bands + affinity in the ranking prompt | new (B §8 fix) | Raw prices in prompt — invites arithmetic; full cost-blindness — proven risk |
| Single bundle + inline budget trade | new (audit of B + C) | B's 2–3 cost variants — a vestige of A's generate-K that contradicts the one-proposal principle; the inline trade surfaces the same trade-off as one tap and yields the same budget-behavior signal. Always-K plans (A) — decision fatigue. LLM picks the trade — wastes a call and a signal; it's the user's money |
| Pairing cautions in the ranking output, enforced by assembly | new (closes B §8 set-blindness) | A second LLM call on the assembled bundle — violates fewest-calls; frontier-judge solely for coherence — too much machinery for one judgment. Without either, set-coherence falls entirely on human review, which the problem statement assigns to the LLM |
| Set-wise rhythm (≥k quick meals for k busy nights) | new (audit of C) | C's per-weekday effort budgets as a planning constraint — schedule-thinking that B's bundle output obsoleted; weekday-level learning stays in L2, but the bundle only commits to counts |
| Tri-layer model; L1 unlearnable | C | Single learned model — learning must never be able to corrupt safety |
| Elo-style weighted signals + embedding neighbors | C | Tag-overlap prefs (A) — too crude to compound; full ML pipeline — not the simplest thing that works |
| Explore slot, uncertainty-driven, code-quotaed, probe-tagged | C | No exploration (A, B, architecture) — exploit-only loop flatlines "Improving"; exploration inside Personalization — breaks the membrane |
| Alternates from the ranking; adaptation is code | B artifact + C need | LLM-mediated re-planning — nobody waits 20s at 6pm |
| Weekly reflection = stats (code) + one narrative rewrite (LLM) | architecture + C | Live personalization agent — only the behavior→taste read is LLM-shaped |
| Household as model structure, negotiation gated | architecture + C | Negotiation agent now — lever 5 before its data |
| A's formulas (union cost, marginal cost, urgency-weighted rescue, feasibility) | A | — retained wholesale as the computation substrate |

**LLM budget:** 1 sync call/week (rank) + 1 async call/week (reflection) + rare adaptation calls. No orchestration, no math, no tool-sequencing in any of them.

**Against the problem statement's rubric:** moves Accepted (bundle + one-proposal interaction + "why" lines), Executed (non-binding schedule + instant swaps), Safe (code-only constraints; learning locked out of L1), Improving (weighted signals, explore slot, reflection, and the headline edits-slope metric to prove it). Levers 1–4 funded; lever 5 gated. Every formalizable piece is code; both LLM calls sit squarely in "no formula exists" territory.
