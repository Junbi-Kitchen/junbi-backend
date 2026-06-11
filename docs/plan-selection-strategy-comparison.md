# Plan Selection — Comparing Strategies A, B, C

**Status:** analysis. C (`plan-selection-strategy-C-portfolio-loop.md`) was written from the problem statement alone, without reading A or B — so where it agrees with them, that's independent convergence (evidence the idea is forced by the problem), and where it disagrees, that's a genuine fork to test.

**One-line summaries:**
- **A (frontier):** code generates K diverse feasible week-plans via preset-weighted greedy; one LLM call picks a plan (+ at most one validated swap); rigid day-assigned schedule out.
- **B (rank-assemble):** one LLM call ranks the candidate pool by preference; code walks the ranking and assembles a feasible bundle; output is a flexible meal *set* + soft rhythm, not a schedule; costed variants only when the trade-off is live.
- **C (portfolio-loop):** A-like mechanism (diverse candidate weeks by construction → LLM judge with pre-validated per-slot alternates), wrapped in a fully specified learning loop: tri-layer user model, explore slot, weekly learning step, mid-week adaptation, cold start, metrics.

---

## 1. Where all three independently agree (treat as settled)

- Code owns feasibility, every number, and hard constraints; the LLM does judgment only, no arithmetic, in **one hot-path call**.
- The LLM can never produce an infeasible result *by construction* (A: pick-from-validated + revalidate swap; B: code assembles; C: pre-validated alternates + re-verify).
- Edits/swaps at review are the strongest learning signal and must be captured as structured data.
- Diversity needs an explicit distance check, not sampler luck (A's `min_recipe_distance` + perturb ≈ C's pairwise plan-distance + perturb — nearly identical, written independently).
- Greedy assembly first; solver only when data demands it.

C re-derived A's architecture from the problem statement without seeing it. That convergence is meaningful: "generate feasible options in code, let the LLM judge complete options with real numbers attached" appears to be where principle-respecting designs land by default. B is the deliberate departure, and that makes B's mechanism bet the most interesting open question.

---

## 2. The real fork: what the LLM judges

| | A | B | C |
|---|---|---|---|
| LLM sees | K complete costed weeks | the recipe pool, per-recipe facts | K complete costed weeks + per-slot alternates |
| LLM emits | plan choice + ≤1 swap | a full preference ranking | plan choice + slot-level swaps from pre-validated lists |
| Feasibility via | pre-validation + revalidate | assembly walks the ranking | pre-validation + re-verify composition |
| Failure mode | **frontier collapse** — presets converge, choice is cosmetic | **cost-blind, set-blind ranking** — LLM never sees a complete week or an aggregate number | same as A, mitigated by alternates |

**B's case against A/C:** if the K plans collapse to near-identical menus, the LLM's "choice" is fake; rank-and-assemble isn't bounded by pre-baked plans at all. A itself flags frontier collapse as its highest risk (A §13.1), so this critique has teeth.

**A/C's case against B:** a week is a *set* with non-additive properties — cost synergy, variety, "coherence," the very things the problem statement says only the LLM can weigh (samey-feel) or only code can compute (union cost). A per-recipe ranking judges none of them: the LLM expresses preference over recipes *in isolation*, and B itself flags the consequence (B §8: ranking is cost-blind; does assembly honor cost tolerance?). B patches this with the variants mechanism — which quietly *is* generate-K, demoted to "when the trade-off is live." So B contains A as its own fallback, and the honest question is how often that fallback fires. If it fires most weeks (tight budgets make trade-offs live by default), B converges back to A with extra steps.

**C's middle ground is a real third option:** judge complete weeks (so set-level trade-offs are visible and numbers are attached), but give the LLM slot-level compositional freedom over pre-validated alternates (so a collapsed or almost-right frontier is fixable — addressing A's ≤1-swap rigidity, A §13.6, without B's unbounded ranking). It costs more pre-computation, all cheap set-ops.

**Verdict:** this is empirically decidable and B already frames it as its primary bet (B §8). Test order: B's ranking-quality risk is checkable offline (does cost-aware assembly of an LLM ranking match what the LLM would pick given full costed weeks?), cheaper than waiting on acceptance metrics.

## 3. B's output-shape insight — the one thing C missed

B's strongest contribution is not the mechanism, it's the reframe of the *output*: grocery delivery batch-commits the **shopping**, not the **schedule** — so ship a committed meal set + consolidated grocery list, with day assignment as a non-binding suggested rhythm.

C attacked the same brittleness from the other side (precomputed per-slot alternates, instant code-level reshuffle when Tuesday is skipped) — i.e., C makes rescheduling *fast*, B makes the schedule *not exist as a commitment*. B's move is strictly simpler: it deletes the failure mode rather than handling it well. C should adopt it. Note they compose cleanly: C's perishables timeline and per-weekday effort budgets survive as inputs to B's *soft* rhythm and to C's mid-week reshuffle — they just stop being hard outputs.

One caution to validate: some users want the schedule decided (decision-fatigue is goal #7). B covers this with the soft rhythm; measure whether "soft" reads as "decided" to those users.

## 4. What C adds that A and B leave open

A is the per-week engine only, by scope. B gestures at the loop ("reads the user model and emits signals," a "Personalization Agent") but specs none of it. The problem statement (§7) lists the loop as the least-designed, highest-value open area — lever 2, the moat. C's contribution is mostly there:

- **Tri-layer user model** — declared constraints (never learned, so learning can't corrupt safety) / numeric affinity + rhythm scores (code-owned, Elo-style updates with explicit signal weights, embedding nearest-neighbor generalization to unseen recipes) / LLM-maintained capped narrative memory (versioned, no numbers, no constraints). This also answers A §13.3's "variety formula is a guess" and the problem statement's "tags are crude" — replace tag-overlap `pref(r)` and entropy-based variety with learned affinity + embedding distance as data accrues.
- **Explore slot** — one deliberate uncertainty-sampling meal per week, honestly labeled. Without it the loop converges on week-3 taste and "Improving" flatlines. Neither A nor B has any exploration mechanism.
- **Weekly learning step** — code updates scores from the event log; one LLM call redistills the narrative memory. The whole loop is arithmetic + one call/week.
- **Cold start** — constraint intake + a swipe deck spanning the embedding space + raised explore quota for "calibration weeks." (B's cold-start row — "stated prefs + popularity, learn from interaction" — is compatible; C just makes week 1 start warm.)
- **Mid-week adaptation, household blending with a negotiation gate, instrumented metrics** (headline: edits-per-plan slope over tenure).

None of this conflicts with B's engine. The user-model layers feed B's Step 1 facts and Step 2 prompt; B's reactions feed the learning step.

## 5. Recommended synthesis

Keep B's two genuine improvements, C's loop, and decide the mechanism fork with data:

1. **Output shape: B.** Flexible bundle + consolidated grocery list + soft rhythm. C's alternates/perishables machinery feeds the soft rhythm and mid-week reshuffles instead of a hard schedule.
2. **Learning loop: C, wholesale.** Tri-layer model, signal-weighted updates, embedding generalization, weekly learning step, explore slot (the explore meal slots naturally into a bundle — arguably *better* than into a schedule, since it carries no day commitment), cold-start swipe deck, metrics. This is the lever-2 moat and currently the least-covered area in A/B.
3. **Interaction wrapping: B.** One proposed bundle, 2–3-tap reactions; every reaction is a learning event consumed by C's update step.
4. **Mechanism: run the cheap offline test before committing.** B (rank→assemble) is the simpler default; A/C (judge complete weeks) is the fallback if ranking proves cost/set-blind in practice. If B's variant path turns out to fire most weeks, switch the default to C's variant of A (frontier + pre-validated slot alternates, which also resolves A §13.1 and §13.6). Either way the contract around it — feasibility by construction, one call, numbers from code — is identical, so the swap is contained.
5. **Carry A forward as the formula library.** Its cost-union/marginal-cost math, urgency-weighted waste rescue, feasibility checks, and edge-case table are the shared substrate every variant uses (B already imports them by reference; C assumed equivalents).

**Per the problem statement's rubric:** the synthesis moves Accepted (B's bundle + interaction), Executed (B's non-binding schedule + C's instant adaptation), Safe (unchanged code-owned constraints, plus C's "learning never touches layer 1"), and Improving (C's loop + explore + metrics). Hot-path LLM budget stays at one call; the loop adds one call per household per week.
