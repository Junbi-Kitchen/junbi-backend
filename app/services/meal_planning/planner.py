"""
Planner glue shared by the ADK pipeline and the headless fallback: turn a
loaded context into engine facts, and a (validated) ranking into the bundle
dict that gets persisted and served. Pure functions over `load_context`
output — both paths produce byte-identical bundle shapes.
"""

from __future__ import annotations

from typing import Any

from app.services.meal_planning import engine

DEFAULT_SLOTS = 5
DEFAULT_BUSY_NIGHTS = 2
CALIBRATION_SIGNAL_THRESHOLD = 15  # fewer scored recipes than this => explore harder

# The ranker sees a *bounded* candidate pool, never the whole catalog. LLM
# context is the funnel's scarce resource; code pre-selects under quotas so
# the cap can't starve assembly (quick meals, rescuers, cheap options, probes).
POOL_CAP = 30
RESCUER_SEATS = 6
CHEAP_SEATS = 5
MIN_QUICK_SEATS = 6
EXPLORE_SEAT_FACTOR = 3


def build_facts(context: dict) -> list[engine.RecipeFacts]:
    return engine.compute_pool_facts(
        context["recipes"],
        context["pantry_ingredient_ids"],
        context["expiring"],
        context["prices"],
        context["affinities"],
    )


def build_constraints(context: dict, week_context: dict | None) -> engine.Constraints:
    wc = week_context or {}
    prefs = context.get("prefs") or {}
    budget = wc.get("budget_cap")
    if budget is None and prefs.get("weekly_budget") is not None:
        budget = float(prefs["weekly_budget"])
    explore = wc.get("explore_quota")
    if explore is None:
        explore = 2 if len(context.get("affinities", {})) < CALIBRATION_SIGNAL_THRESHOLD else 1
    return engine.Constraints(
        slots=int(wc.get("slots", DEFAULT_SLOTS)),
        budget_cap=budget,
        busy_nights=int(wc.get("busy_nights", DEFAULT_BUSY_NIGHTS)),
        explore_quota=int(explore),
    )


def select_candidates(
    facts: list[engine.RecipeFacts],
    constraints: engine.Constraints,
    uncertainty_ids: set[str],
    cap: int = POOL_CAP,
) -> list[engine.RecipeFacts]:
    """Deterministic pre-selection to a bounded candidate pool.

    Quota seats are reserved first so every assembly need survives the cut:
    waste rescuers, enough quick meals for the busy nights, cheap options for
    tight budgets, and low-confidence probes for the explore slot. Remaining
    seats fill by affinity with round-robin cuisine diversity, so the LLM's
    choice space stays broad rather than collapsing to one taste cluster.
    """
    if len(facts) <= cap:
        return facts

    chosen: dict[str, engine.RecipeFacts] = {}

    def take(candidates: list[engine.RecipeFacts], seats: int) -> None:
        for f in candidates:
            if seats <= 0 or len(chosen) >= cap:
                return
            if f.recipe_id not in chosen:
                chosen[f.recipe_id] = f
                seats -= 1

    take(
        sorted((f for f in facts if f.expiring_rescued > 0), key=lambda f: -f.expiring_rescued),
        RESCUER_SEATS,
    )
    take(
        sorted((f for f in facts if f.is_quick), key=lambda f: -f.affinity),
        max(constraints.busy_nights * 2, MIN_QUICK_SEATS),
    )
    take(sorted(facts, key=lambda f: f.cost_to_add), CHEAP_SEATS)
    if constraints.explore_quota and uncertainty_ids:
        take(
            sorted((f for f in facts if f.recipe_id in uncertainty_ids), key=lambda f: f.confidence),
            constraints.explore_quota * EXPLORE_SEAT_FACTOR,
        )

    # fill remaining seats: affinity-ranked, round-robin across cuisines
    from collections import defaultdict, deque

    ranked = sorted(
        (f for f in facts if f.recipe_id not in chosen),
        key=lambda f: (-f.affinity, -f.novelty, f.cost_to_add),
    )
    buckets: dict[str, deque] = defaultdict(deque)
    for f in ranked:
        buckets[f.cuisine].append(f)
    cuisines = sorted(buckets)
    while len(chosen) < cap and any(buckets.values()):
        for c in cuisines:
            if buckets[c] and len(chosen) < cap:
                f = buckets[c].popleft()
                chosen[f.recipe_id] = f

    return list(chosen.values())


def build_candidate_facts(
    context: dict, week_context: dict | None
) -> list[engine.RecipeFacts]:
    """Facts for the bounded candidate pool — what the ranker and assembly
    both operate on (same deterministic selection in both paths)."""
    facts = build_facts(context)
    constraints = build_constraints(context, week_context)
    cap = int((week_context or {}).get("pool_cap", POOL_CAP))
    return select_candidates(facts, constraints, context.get("uncertainty_ids") or set(), cap)


def compact_pool_summary(
    facts: list[engine.RecipeFacts],
    expiring: dict[str, tuple[float, int]] | None = None,
    ingredient_names: dict[str, str] | None = None,
) -> str:
    """Token-light per-recipe facts for the ranking prompt. Cost appears only
    as a band — the LLM never sees numbers it could be tempted to add up.
    Expiring pantry items are named so rationales can be specific
    ("uses the spinach before it turns")."""
    expiring = expiring or {}
    names = ingredient_names or {}
    lines = []
    for f in facts:
        rescue = ""
        if f.expiring_rescued > 0:
            items = [
                f"{names.get(i, 'pantry item')}({days}d)"
                for i, (_value, days) in expiring.items()
                if i in f.ingredient_ids
            ]
            if items:
                rescue = " | rescues:" + ",".join(sorted(items)[:3])
        lines.append(
            f"{f.recipe_id} | {f.title} | {f.cuisine}/{f.protein} | {f.total_time_mins}min"
            f" | cost:{f.cost_band}"
            + rescue
            + f" | affinity:{f.affinity:+.1f}(conf {f.confidence:.1f})"
            + f" | novelty:{f.novelty:.1f}"
            + (f" | {','.join(f.tags[:4])}" if f.tags else "")
        )
    return "\n".join(lines)


def household_summary(context: dict, week_context: dict | None) -> str:
    prefs = context.get("prefs") or {}
    wc = week_context or {}
    parts = [
        f"Household size: {prefs.get('household_size') or 1}.",
        f"Favorite cuisines: {', '.join(prefs.get('cuisines') or []) or 'unknown'}.",
        f"Busy nights this week: {wc.get('busy_nights', DEFAULT_BUSY_NIGHTS)} (they need quick meals).",
    ]
    if wc.get("notes"):
        parts.append(f"This week: {wc['notes']}")
    if context.get("narrative"):
        parts.append(f"What we've learned about them so far: {context['narrative']}")
    return " ".join(parts)


def validate_ranking(ranking: list[str] | None, facts: list[engine.RecipeFacts]) -> list[str]:
    """LLM output discipline: drop hallucinated ids, dedupe, never trust order blindly."""
    pool_ids = {f.recipe_id for f in facts}
    seen: set[str] = set()
    cleaned: list[str] = []
    for rid in ranking or []:
        if rid in pool_ids and rid not in seen:
            cleaned.append(rid)
            seen.add(rid)
    return cleaned or engine.deterministic_ranking(facts)


def _default_rationale(f: engine.RecipeFacts) -> str:
    if f.expiring_rescued > 0:
        return "Uses pantry items before they expire."
    if f.affinity > 0.5:
        return "A household favorite."
    if f.is_quick:
        return "Quick — fits a busy night."
    if f.novelty >= 1.0:
        return "Something you haven't tried yet."
    return "Balances the week."


def assemble_bundle(
    context: dict,
    week_context: dict | None,
    ranking: list[str],
    pairing_cautions: list[list[str]] | None = None,
    rationales: dict[str, str] | None = None,
    ranking_source: str = "llm",
) -> tuple[dict, dict]:
    """Run assembly and shape the result for persistence/API. Returns
    (bundle_dict, ranking_artifact)."""
    facts = build_candidate_facts(context, week_context)
    constraints = build_constraints(context, week_context)
    cleaned = validate_ranking(ranking, facts)
    cautions = [tuple(p) for p in (pairing_cautions or []) if len(p) == 2]

    bundle = engine.assemble(
        cleaned,
        facts,
        constraints,
        context["pantry_ingredient_ids"],
        context["prices"],
        pairing_cautions=cautions,
        uncertainty_ids=context.get("uncertainty_ids"),
    )

    rationales = rationales or {}
    items: list[dict[str, Any]] = []
    for item in bundle.items:
        f = item.facts
        items.append(
            {
                "recipe_id": f.recipe_id,
                "title": f.title,
                "cuisine": f.cuisine,
                "protein": f.protein,
                "total_time_mins": f.total_time_mins,
                "is_quick": f.is_quick,
                "is_probe": item.is_probe,
                "suggested_day": item.suggested_day,
                "alternates": item.alternates,
                "rationale": (
                    "Something new to try — tell us how it lands."
                    if item.is_probe
                    else rationales.get(f.recipe_id) or _default_rationale(f)
                ),
                "cost_to_add": f.cost_to_add,
                "cost_band": f.cost_band,
                "expiring_rescued": f.expiring_rescued,
            }
        )

    ing_names = context.get("ingredient_names", {})
    grocery = [
        {
            "ingredient_id": i,
            "name": ing_names.get(i, i),
            "price": context["prices"].get(i, 0.0),
        }
        for i in bundle.grocery_ingredient_ids
    ]

    bundle_dict = {
        "status": bundle.status,
        "items": items,
        "total_cost": bundle.total_cost,
        "budget_cap": constraints.budget_cap,
        "grocery_list": grocery,
        "budget_trade": bundle.budget_trade,
        "flags": bundle.flags,
    }
    artifact = {
        "ranking": cleaned,
        "pairing_cautions": [list(p) for p in cautions],
        "rationales": rationales,
        "source": ranking_source,
    }
    return bundle_dict, artifact
