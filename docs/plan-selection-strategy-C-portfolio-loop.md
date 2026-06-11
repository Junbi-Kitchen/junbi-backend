# Plan Selection — Strategy C: Scored Pool → Diverse Portfolios → LLM Judge → Weekly Learning Step

**Status:** proposal, written from the problem statement only (`meal-planning-problem-statement.md`), deliberately without reading strategies A or B first. Intended as an independent data point for comparison.

**TLDR:** Treat each week as one move in a bandit-style learning loop. Code maintains a three-layer household model (declared constraints / learned numeric taste scores / LLM-maintained narrative memory), generates a handful of complete, feasible, *deliberately different* candidate weeks, and attaches real numbers to each. One LLM call picks and explains the week, including one intentional "explore" meal. Every edit, skip, and cook event flows back as a weighted training signal in a weekly learning step. Total LLM usage: one call to plan, one call to learn, occasional calls to adapt mid-week.

---

## 1. The user model (lever 2 — the moat)

Three layers, each with a different owner and update rule:

**Layer 1 — Constraints (declared, never learned).** Allergies, dietary locks, budget cap, equipment, hard vetoes per household member. Stored as structured data, enforced exclusively by code as filters. Learning never touches this layer — a learned system that *infers* an allergy is a system that can forget one.

**Layer 2 — Numeric taste & rhythm scores (learned, owned by code).** For each household:

- A per-recipe affinity score, updated Elo/Bayesian-style from behavioral events with explicit weights, roughly: cooked-and-rated-up > cooked > accepted-unedited > present-but-skipped > swapped out at review > explicitly rejected. An edit is the strongest signal (principle 6) — a swap simultaneously down-weights the removed recipe and up-weights what replaced it.
- Generalization beyond seen recipes via recipe embeddings (off-the-shelf text embedding of ingredients + cuisine + technique): a recipe's prior score is a similarity-weighted average of scored neighbors. Behavior trains scores; embeddings spread them to unseen recipes. No model-training infrastructure — it's nearest-neighbors arithmetic. (Answers the "tags are crude" open question.)
- Rhythm parameters: repetition tolerance (how soon a repeat is welcome — estimated from how repeats are received), per-weekday effort budget (learned from *which* days get skipped or swapped to quicker meals), variety appetite.

**Layer 3 — Narrative memory (owned by the LLM, consumed by the LLM).** A short, capped document of distilled observations: "swaps out fish every time it appears," "Sunday is the ambitious-cooking day," "kid tolerates mushrooms blended, not visible." Updated once a week (§5), fed into the planning prompt. This is the genuinely unformalizable residue — exactly the bucket principle 1 assigns to the LLM. It must never contain numbers or constraints (those live in layers 1–2, where code enforces them).

## 2. The per-week engine

**Step A — Filter (code).** Recipe pool → feasible set: layer-1 constraints, ingredient availability, equipment, per-meal time ceilings from the rhythm model. Anything that survives is *safe by construction*; nothing downstream can reintroduce an unsafe recipe.

**Step B — Score (code).** Each feasible recipe gets a vector of measured numbers: real cost (live prices), pantry-utilization (which expiring items it consumes), active/total time, nutrition summary, affinity score from layer 2, novelty (recency-discounted), per-member predicted reception.

**Step C — Assemble diverse candidate weeks (code).** Build ~4–6 *complete* weeks via greedy/beam construction, each optimizing a different weighting of the score vector: cheapest week, pantry-max week, lowest-effort week, adventurous week, balanced week. Every candidate independently satisfies the budget cap and slot-level rules (quick meals on busy weekdays, perishables consumed in freshness order). Diversity is guaranteed *by construction* (distinct objective weightings + a minimum pairwise plan-distance check), not hoped for from sampler randomness. If two candidates land too similar, perturb weights and rebuild — cheap, since it's all code. (Answers the frontier-diversity open question.)

**Step D — Judge (one LLM call).** Input: the candidate weeks with their computed numbers attached, the layer-3 memory, this week's context (calendar notes, "we have guests Friday," cravings the user typed). The LLM picks a week — or composes one by slot-level swaps *restricted to a pre-validated alternates list per slot* (code pre-computes 2–3 feasible substitutes per slot, so any composition the LLM emits is still feasible by construction; code re-verifies the budget sum after composition anyway). It also writes the one-line "why" per meal — trust lever 4, nearly free since the reasoning is already in context. The LLM never sees a raw price it must add up and never outputs a number the user sees; numbers are echoed from the score vectors by code.

**Step E — Explore slot (code decides quota, LLM picks the probe).** Each week, exactly one slot is an information-gathering meal: a feasible recipe with high *uncertainty* in layer 2 (few scored neighbors, wide posterior), not high expected score. This is the bandit move that makes the loop compound — without deliberate exploration the system converges on week-3 taste forever and "improving" (success metric 4) flatlines. The explore meal is labeled honestly in the UI ("something new to try — tell us"), converting it from a planning miss into a feature.

**Step F — Review.** User accepts/edits. Every edit is captured structurally (removed X, added Y, reordered days) — not as free text — because it's training data.

## 3. Mid-week adaptation (lever 3)

The plan ships with its flexibility precomputed: per-slot alternates (from Step D) and a perishables timeline (which meals consume which expiring items). Then:

- **"Skipped Tuesday"** → pure code: reshuffle remaining days so perishable-consuming meals move earlier, drop or freeze-flag the orphaned meal, recompute the numbers. No LLM for the common case.
- **"Swap tonight, I'm exhausted"** → code offers the precomputed quick alternates for that slot; one tap, zero latency, zero LLM.
- Only genuinely novel situations ("guests turned vegetarian") trigger an LLM call, with the same judge-over-feasible-options contract as Step D.
- Every adaptation event is also a layer-2 signal (a Tuesday skip is evidence about Tuesday's real effort budget).

Re-planning stays instant and safe, which is what cook-through actually needs — nobody waits 20 seconds for an agent at 6pm.

## 4. Cold start

- Onboarding = constraints (layer 1) + a 60-second swipe deck of ~15 real recipes spanning the embedding space. Each swipe is a layer-2 signal, so week 1 starts with a coarse but real taste model rather than zeros.
- Weeks 1–3 run a higher explore quota (2 slots instead of 1) and the judge prompt says so. Framed in-product as "calibration weeks," which sets expectations and makes early misses feel like progress.
- Optional accelerant: "name 5 dinners your family already loves" — seeds affinity directly and gives the embedding neighborhood anchors.

## 5. The weekly learning step (closing the loop)

When a week closes, one batch job:

1. **Code:** ingest the event log (acceptance, edits, cooks, skips, ratings, mid-week swaps) → update layer-2 scores and rhythm parameters with the signal weights from §1.
2. **One LLM call:** given the event log and current layer-3 memory, rewrite the memory — add distilled observations, drop stale ones, keep it under a size cap. The memory is versioned so a bad distillation can be rolled back.

That's the entire learning machinery: arithmetic plus one LLM call per household per week. No agents, no orchestration.

## 6. Household handling (lever 5, kept minimal)

Per-member layer-1 vetoes are absolute. Layer 2 keeps per-member affinity where signals are attributable (ratings, "kid didn't eat it") and a household blend otherwise. Code maintains a fairness counter (each member's top-affinity recipes should appear over a rolling window) as a soft scoring term, and the judge sees per-member notes in layer 3. Explicit negotiation machinery is gated behind a measured trigger — e.g., sustained veto/edit conflict rate above a threshold — per lever 5: don't build it until a household's data demands it.

## 7. Metrics (success criteria, instrumented)

| Success metric | Instrument |
|---|---|
| Accepted | acceptance rate; edits per plan |
| Executed | cook-through rate (confirmations, with grocery-purchase + "mark cooked" as proxies) |
| Safe | constraint violations — alarmed, target literally 0; plus budget-overrun rate |
| Improving | edits-per-plan slope over tenure (cohort curve), explore-meal hit rate, retention |

The single headline number: **edits per plan by week of tenure**. If the architecture works, that curve slopes down per household; if it's flat, layers 2–3 aren't learning and nothing else matters.

## 8. Rubric self-check (problem statement §9)

- Moves levers 1–4 directly; lever 5 deliberately deferred behind a data trigger.
- LLM call budget: 1 plan + 1 learn per week, occasional adapt calls — within principle 5.
- The LLM never computes, never sequences tools, never escapes the feasible region (compositions are restricted to pre-validated alternates and re-verified).
- Simplest version that works: greedy assembly, nearest-neighbor embeddings, no solver, no multi-agent — each upgradeable independently if the metrics demand it.
- Everything serves the loop: explore slots, structured edits, weekly learning step, and the tri-layer model exist *for* week six, not week one.

The most opinionated bets, versus what a one-shot planner would do: the **explore slot** (sacrifice a little week-1 optimality to make week-6 dramatically better), the **tri-layer split of the user model** (so learning can never corrupt safety), and **diversity by construction** in candidate weeks (so the LLM's choice is real).
