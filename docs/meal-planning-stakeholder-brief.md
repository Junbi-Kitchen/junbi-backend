# Meal Planning — Stakeholder Brief

**Audience:** stakeholders deciding whether and how to fund this. Non-technical by design.
**The full spec behind this brief:** `meal-planning-final-strategy.md` (canonical), `system-architecture.md`, `meal-planning-problem-statement.md`.

---

## The problem we're solving

"What's for dinner?" is a tax every household pays seven times a week. The symptoms are decision fatigue, produce rotting in the fridge, blown grocery budgets, and meals nobody wanted. Plenty of apps generate meal plans; almost all of them feel generic in week one and *identical* in week ten — and that's why they get abandoned. The product opportunity isn't "generate a plan." It's a planner that **knows your household better every single week**.

## The core bet

We are not building a plan generator. We're building a **learning relationship**. Every week is one turn of a loop: plan → watch what the household actually does → learn → plan better. The plan generator is table stakes; the compounding personalization is the moat, because it's the one thing a competitor can't copy — they can copy our features, but not our six weeks of *your* family's behavior.

One consequence of that bet runs through everything: the system deliberately spends a little of this week's optimality to be smarter next week.

## What the user experiences

**Sunday.** One proposal arrives — not three options to compare, one. It's a *bundle*: "here are your 5 dinners this week and the grocery order for them," with a one-line reason on each ("uses the spinach before it turns," "quick — you have a busy Tuesday"). One of the five is honestly labeled *"something new to try — tell us."* If the budget forced a tough call, there's a single tap available: *"adding the salmon back is +$9.40."* The user swaps anything they don't like in two taps, approves, and the groceries are ordered.

**Crucially, we commit them to a shopping cart, not a calendar.** There's a suggested rhythm — ambitious meal Sunday, quick ones on work nights — but it's advice, not a contract. Real life moves; the plan doesn't break when Tuesday goes sideways. Skipped a night? Nothing fails. Too tired to cook the planned thing? One tap shows pre-computed alternates, instantly — no AI spinner at 6pm.

**And quietly, everything they just did taught us something.** The swap is the strongest signal we get. Taking or ignoring the +$9.40 tells us their real budget flexibility, not their stated one. Opening tonight's recipe tells us they cooked it. We never ask them to "log" anything — every signal is exhaust from actions they wanted to take anyway. A system that requires homework to learn never learns, because nobody does the homework.

## How it works (the two-minute version)

Three pieces, deliberately boring:

1. **A household model, in three layers.** Hard constraints (allergies, diet, budget cap) — declared by the user, enforced by code, and the learning system *cannot write to them*, ever. Learned numbers (what they like, how much repetition they tolerate, their real budget behavior) — updated weekly by arithmetic from the behavior log. And a short narrative the AI maintains — "says they want variety, actually cooks the same three comfort meals on weeknights" — the genuinely fuzzy stuff no formula captures.

2. **A weekly planning step.** Code filters recipes to what's safe and feasible and computes every real number — costs, what pantry items get rescued, time. Then **exactly one AI call**: rank these recipes for *this* household, flag any that'd feel repetitive together. Code assembles the bundle from that ranking, enforcing the budget cap mathematically. The AI never does arithmetic, never sees a number the user will see, and structurally *cannot* produce a plan that violates a constraint.

3. **A weekly learning step.** After the week closes: arithmetic updates the scores, and one AI call re-distills the narrative. The AI's suggestions about the numbers are proposals that code applies within strict bounds — it reads between the lines, but never holds the pen.

Total AI cost: **two calls per household per week**. This thing costs pennies to run and answers instantly, because the latency-sensitive paths are all plain code.

## Why this is safe enough to trust

Trust here is binary — one allergen in a cart and we've lost the customer forever. So safety isn't a model behavior we hope for; it's an architecture property. Constraints are enforced in code before the AI ever sees the options, re-verified after, and the learning system has no write-path to them. A constraint violation is, by construction, an ordinary software bug — findable, testable, fixable — never a model hallucination.

## How we'll know it's working

One headline number: **edits per plan, by week of customer tenure.** If we're learning, a household that made four swaps in week one makes one in week eight — that falling curve *is* the product working, and it's also our retention story. Around it: acceptance rate, cook-through rate, and constraint violations alarmed at literally zero. Every design decision in the strategy doc has a written kill-criterion — the specific evidence that would make us change it — so we argue with data, not opinions.

## Risks, honestly

- **The biggest risk isn't the algorithm — it's the recipe pool.** The system ranks recipes the household could cook; a new user has saved none. We need a seeded catalog or real recipe import before any of this matters. This is the first scoping decision.
- **Signals need the app's cooperation.** The swap UI, the "what's tonight" screen, the one-tap trade — that's frontend work, and the learning loop starves without it.
- **The learning loop can be built now but only *proven* with real households over weeks.** We've structured for that: signal logging ships in the first milestone, before anything consumes it — so the data moat starts accruing from day one of usage.

## The roadmap

| Milestone | What ships | Notes |
|---|---|---|
| **M0** | The math substrate — feasibility, costs, assembly | Pure code, testable immediately, no AI required |
| **M1** | The first real week — ranking call, bundle, review flow, **signal log turned on** | Data starts accruing before anything consumes it |
| **M2** | The learning model + the 60-second onboarding taste quiz | Week one starts warm, not cold |
| **M3** | The weekly reflection | The "reads between the lines" layer |
| **M4** | Gated extras — household negotiation, solvers | Built only when data demands them |

Backend-wise, M0–M1 are a few focused working sessions on infrastructure we already have: the database, the Kroger price feed, the ingredient work, the AI plumbing. Nothing in the plan is research — the riskiest pieces have written fallbacks.

---

**The one-sentence version:** code guarantees the plan is safe and affordable, one AI call a week makes it personal, and every tap the user makes teaches the system — so week ten is visibly better than week one, and *that's* the product nobody else has.
