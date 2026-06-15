# Meal Planning — Problem Statement & Focus

**Purpose of this doc:** a shared framing to gather and evaluate ideas from many sources (cofounders, teammates, other Claude sessions). It is **not** an implementation spec — it deliberately leaves *how* open. It defines the problem, what "best" means, the goals, and the principles any proposed solution should respect, so contributions aim at a coherent target.

**Read this first; then, if you want to go deeper into one approach:**
- `docs/plan-selection-strategy-B-rank-assemble.md` — current strategy for selecting one week's recipes (the per-week engine)
- `docs/plan-selection-strategy-A-frontier.md` — earlier alternative strategy + shared feasibility/formula detail
- `docs/meal-plan-selection-design.md` — the same in the context of the surrounding pipeline

This doc stands alone; the design docs are one *current* take on part of it, not the boundary of the problem.

---

## 1. The problem

People want to eat well without the daily grind of deciding what to cook, shopping for it, and not wasting money or food. "What's for dinner?" is a recurring tax — decision fatigue, repeated groceries, forgotten produce rotting in the fridge, blown budgets, meals nobody actually wanted.

We want a system that plans a household's meals for them — choosing recipes, producing the shopping list, ordering — such that the household **accepts the plan, actually cooks it, never has a constraint violated, and finds the system getting better the longer they use it.**

This document is about the hardest and most valuable part: **deciding what the week should be.**

---

## 2. What "success" means (judge every idea against these)

A meal planner is **best** if it:

1. **Accepted** — the user takes the plan with minimal editing.
2. **Executed** — they actually cook it, not abandon it mid-week.
3. **Safe** — it *never* violates a hard constraint (allergy, dietary lock, budget cap). Trust is binary; one violation loses it.
4. **Improving** — it visibly gets better the longer they use it.

If a feature doesn't move one of these four, it's decoration, not progress. **"Best" ≠ "most."** The maximal system is not the best system.

> Implication: we should **instrument** these (acceptance rate, edit rate, cook-through rate, week-over-week retention). "Best" is defined by the metrics, not by an armchair.

---

## 3. The reframe (the most important idea here)

It is tempting to treat meal planning as a **one-shot optimization**: produce the optimal week, this week. That is a local maximum.

The best meal planner is a **learning relationship**, not a one-shot planner. Each week is one turn in a loop:

```
know the user → plan → watch what they actually do → learn → plan better
```

A generic planner that is "optimal" this week and identical next week loses to a mediocre one that *learns you* by week six. **Personalization that compounds is where "best" is won.** Design for the loop, not the single plan.

---

## 4. The user's actual goals (what the household wants)

1. Eat food they genuinely **want** to eat (taste, cravings, mood).
2. Eat reasonably **healthily**.
3. Not **overspend**.
4. Not **waste** food — use what's already in the pantry.
5. **Fit their life** — time, energy, skill, a busy Tuesday vs. a free Sunday.
6. **Feed everyone** — households have multiple eaters with different (sometimes conflicting) preferences and constraints.
7. **Kill the decision fatigue** — remove the daily "what's for dinner" anguish. (Often the real product value.)
8. **Get better over time** — the system should learn them.

---

## 5. Where to focus — the levers, ranked by impact on "best"

This is the opinionated part. Invest in this order:

1. **Feasibility correctness — table stakes.** Plans must respect every hard constraint and every real number (cost, budget, allergies, coverage). Binary: get it perfect. Without it, nothing else matters — one budget blowout or allergen and trust is gone.
2. **The learning loop / user model — the differentiator (the moat).** A persistent, evolving model of the household (tastes, what flopped, repetition tolerance, effort/schedule, per-person preferences), updated each week from real behavior. This is where almost every meal app fails — they feel generic forever because they never learn you. **This is where "best" actually lives.**
3. **Fit-to-life + mid-week flexibility — drives execution.** Effort/schedule awareness (quick on busy nights), and graceful re-planning from "I skipped Tuesday." Huge for cook-through; barely anyone does it well.
4. **Trust & control — drives acceptance.** Clear reasons for the plan, and frictionless edits — and edits feed lever 2 (an edit is the strongest possible signal).
5. **Household multi-stakeholder handling — conditional.** Real, but scales with household size/conflict. Likely handled simply for 1–2 people, with explicit per-member negotiation only when conflict is real.

> Note: heavier machinery (multi-agent, solvers, etc.) is a *means*, and tends to live at lever 1 (solvers for hard feasibility math) or lever 5 (negotiation for conflicting households) — **not** at levers 2–3, where the wins actually are. Don't let "interesting technique" pull focus from "what moves the metric."

---

## 6. Design principles (the rules of the game)

Any proposed solution should respect these. They are the disciplined core; ignore them and ideas tend to drift into expensive, brittle, or untrustworthy territory.

1. **Formalize what's formalizable; use the LLM only for what isn't.** Three buckets:
   - formula, easy → **code**
   - formula, hard (combinatorial/non-additive) → **solver, still code**
   - no correct formula exists for anyone (taste, trade-off weighting, "coherence") → **LLM**
2. **"Hard" ≠ "fuzzy."** Complicated-but-definable math is a job for a better algorithm, and the thing LLMs are *worst* at. Only genuinely undefinable judgment goes to the LLM.
3. **Code measures, the LLM weighs.** The LLM never does arithmetic and never produces the number the user sees (cost, budget). Those are computed by code from real data.
4. **Hard constraints are inviolable.** Allergies, dietary locks, budget caps are enforced by code. The LLM optimizes *preferences* only *within* the feasible region code defines — it can never pick something that breaks a hard rule.
5. **Prefer the fewest LLM calls that do the job.** No LLM orchestrating the pipeline, no LLM deciding tool order, no LLM doing math. Reach for more LLM calls (or agents) only when there's a *real* second stakeholder or a learning-over-time loop — not for decomposition's own sake.
6. **The human review/edit step is both a safety backstop and a signal.** It catches fuzzy mistakes *and* teaches the system. Treat edits as first-class learning input.
7. **Design for the loop.** Each plan is one step in an ongoing relationship; the system should consume a user model and emit signals back into it.

---

## 7. What's already reasoned-through (stable) vs. open

**Reasonably settled (don't re-litigate unless you have a strong reason):**
- The per-week engine: **code generates feasible candidate plans; the LLM picks/judges among them** (it doesn't compute or sequence). See `plan-selection-strategy-B-rank-assemble.md` (and `-A-frontier.md` for the alternative).
- Code owns feasibility, cost math, and hard constraints; the LLM owns only unformalizable judgment.
- Start simple (greedy + one LLM call); add machinery (solvers, agents) only when data shows it's needed.

**Open — we want ideas here:**
- **The learning loop / user model:** what should it hold? what signals feed it? how does it update? (Lever 2 — the moat; least designed.)
- **Fit-to-life & mid-week adaptation:** how to model effort/schedule and re-plan gracefully when life deviates. (Lever 3.)
- **Household multi-stakeholder:** when is explicit negotiation worth it vs. just prompting with everyone's preferences? (Lever 5.)
- **Frontier diversity:** how to guarantee the candidate plans genuinely span the trade-off space (so the LLM's choice is real, not cosmetic).
- **Taste/preference modeling:** tags and cuisine are crude; is there a better (e.g. embedding-based, or learned-from-behavior) representation of "what this person likes"?
- **Metrics & instrumentation:** what exactly do we measure to know we're winning on §2?
- **Cold start:** how good can week 1 be before the system has learned anything?

---

## 8. Non-goals / out of scope (for now)

- Re-deciding the surrounding pipeline (pantry loading, Kroger pricing/cart, checkout) — that exists; this is about *what the week should be*.
- Recipe content creation/scraping quality (assume a recipe pool exists).
- Replacing human review — keep the user in the loop; don't aim for zero-touch autonomy yet.
- Adding techniques (multi-agent, solvers) for their own sake — only if they move a §2 metric.

---

## 9. How to evaluate any idea (a quick rubric)

For any proposal, ask:
1. **Which of the four success metrics (§2) does it move, and how would we measure that?**
2. **Which lever (§5) is it — and is that where the wins are, or are we polishing a low lever?**
3. **Does it respect the principles (§6)?** In particular: is it putting hard math or hard constraints in the LLM's hands? Is it adding LLM calls/agents without a real stakeholder or learning need?
4. **Is it the simplest version that works,** or can we get 80% of it with less?
5. **Does it serve the one-shot, or the learning loop (§3)?** Prefer the loop.

If an idea moves a top-two lever, respects the principles, and is the simplest version that works — it's probably worth doing. If it's clever but doesn't move a metric, park it.

---

*This is a living framing. The problem (§1), success metrics (§2), reframe (§3), and principles (§6) are the stable spine — argue with them only with strong reasons. Everything in §7-"open" is genuinely up for grabs.*
