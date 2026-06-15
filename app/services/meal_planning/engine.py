"""
Meal-planning engine core — pure Python, no DB, no LLM.

Implements the computation side of docs/meal-planning-final-strategy.md §3:
pool facts (marginal cost, waste rescue, novelty), ranked-walk assembly with
budget/variety caps enforced by construction, the explore slot, per-slot
alternates, the budget trade, and the soft rhythm.

Cost model (v1): prices are per purchase pack; buying an ingredient covers
every recipe that uses it ("have any = covered"). Shared ingredients are
therefore counted once — set math, never per-recipe sums.
"""

from __future__ import annotations

from dataclasses import dataclass, field

QUICK_MINS = 30
DEFAULT_MAX_SAME_PROTEIN = 2
ALTERNATES_PER_SLOT = 3
# suggested_day: 0=Mon .. 6=Sun
WEEKEND_DAYS = [6, 5]
WEEKDAYS = [1, 2, 3, 0, 4]  # Tue, Wed, Thu, Mon, Fri — perishable-rescuers first


@dataclass(frozen=True)
class RecipeFacts:
    recipe_id: str
    title: str
    cuisine: str
    protein: str
    total_time_mins: int
    servings: int
    ingredient_ids: frozenset[str]
    cost_to_add: float        # marginal vs pantry alone
    cost_band: str            # low | med | high
    expiring_rescued: float   # $ value of expiring pantry stock this recipe uses
    affinity: float
    confidence: float
    novelty: float            # 1.0 = never cooked, decays toward 0 for recent repeats
    tags: tuple[str, ...] = ()

    @property
    def is_quick(self) -> bool:
        return self.total_time_mins <= QUICK_MINS


@dataclass
class Constraints:
    slots: int
    budget_cap: float | None = None
    busy_nights: int = 0
    max_same_protein: int = DEFAULT_MAX_SAME_PROTEIN
    explore_quota: int = 0


@dataclass
class BundleItem:
    facts: RecipeFacts
    is_probe: bool = False
    alternates: list[str] = field(default_factory=list)
    suggested_day: int | None = None


@dataclass
class Bundle:
    status: str  # feasible | infeasible | insufficient_pool
    items: list[BundleItem]
    total_cost: float
    grocery_ingredient_ids: list[str]
    budget_trade: dict | None = None  # {"recipe_id", "title", "delta"}
    flags: list[str] = field(default_factory=list)


def urgency(days_left: int | None) -> float:
    """How urgently an expiring pantry item should be rescued (0..1)."""
    if days_left is None:
        return 0.0
    if days_left <= 1:
        return 1.0
    if days_left <= 2:
        return 0.9
    if days_left <= 4:
        return 0.6
    if days_left <= 7:
        return 0.3
    return 0.1


def compute_pool_facts(
    recipes: list[dict],
    pantry_ingredient_ids: set[str],
    expiring: dict[str, tuple[float, int]],  # ingredient_id -> (pack value $, days_left)
    prices: dict[str, float],                # ingredient_id -> pack price $
    affinities: dict[str, tuple[float, float]],  # recipe_id -> (score, confidence)
) -> list[RecipeFacts]:
    """Measure every recipe in the (already L1-filtered) pool.

    Each recipe dict needs: id, title, cuisine, protein, total_time_mins,
    servings, ingredient_ids (iterable), novelty (0..1), tags.
    """
    raw: list[dict] = []
    for r in recipes:
        ing = frozenset(r["ingredient_ids"])
        cost = round(sum(prices.get(i, 0.0) for i in ing - pantry_ingredient_ids), 2)
        rescued = round(
            sum(value * urgency(days) for i, (value, days) in expiring.items() if i in ing), 2
        )
        raw.append({**r, "ingredient_ids": ing, "cost_to_add": cost, "expiring_rescued": rescued})

    costs = sorted(x["cost_to_add"] for x in raw) or [0.0]
    t1 = costs[len(costs) // 3]
    t2 = costs[(2 * len(costs)) // 3]

    facts = []
    for x in raw:
        score, conf = affinities.get(x["id"], (0.0, 0.0))
        band = "low" if x["cost_to_add"] <= t1 else "med" if x["cost_to_add"] <= t2 else "high"
        facts.append(
            RecipeFacts(
                recipe_id=x["id"],
                title=x["title"],
                cuisine=x.get("cuisine") or "other",
                protein=x.get("protein") or "other",
                total_time_mins=x.get("total_time_mins") or 0,
                servings=x.get("servings") or 4,
                ingredient_ids=x["ingredient_ids"],
                cost_to_add=x["cost_to_add"],
                cost_band=band,
                expiring_rescued=x["expiring_rescued"],
                affinity=score,
                confidence=conf,
                novelty=x.get("novelty", 1.0),
                tags=tuple(x.get("tags") or ()),
            )
        )
    return facts


def deterministic_ranking(facts: list[RecipeFacts]) -> list[str]:
    """Headless fallback ranking: affinity, then waste rescue, novelty, cheapness."""
    ordered = sorted(
        facts,
        key=lambda f: (-f.affinity, -f.expiring_rescued, -f.novelty, f.cost_to_add),
    )
    return [f.recipe_id for f in ordered]


def union_cost(
    items: list[RecipeFacts], pantry: set[str], prices: dict[str, float]
) -> float:
    need = set().union(*(f.ingredient_ids for f in items)) - pantry if items else set()
    return round(sum(prices.get(i, 0.0) for i in need), 2)


def _marginal(facts: RecipeFacts, bought: set[str], prices: dict[str, float]) -> float:
    return round(sum(prices.get(i, 0.0) for i in facts.ingredient_ids - bought), 2)


def _cautioned(recipe_id: str, selected: list[RecipeFacts], caution_pairs: set[frozenset]) -> bool:
    return any(frozenset((recipe_id, s.recipe_id)) in caution_pairs for s in selected)


def assemble(
    ranking: list[str],
    facts: list[RecipeFacts],
    constraints: Constraints,
    pantry_ingredient_ids: set[str],
    prices: dict[str, float],
    pairing_cautions: list[tuple[str, str]] | None = None,
    uncertainty_ids: set[str] | None = None,
) -> Bundle:
    """Walk the ranking and build a feasible bundle by construction.

    Hard rules enforced here (never the LLM's job): budget cap via union cost,
    protein variety cap, quick-meal count >= busy_nights, explore quota.
    Pairing cautions are soft: skipped in the first pass, allowed if coverage
    can't otherwise be met.
    """
    by_id = {f.recipe_id: f for f in facts}
    ordered = [by_id[r] for r in ranking if r in by_id]
    # anything in the pool the ranker didn't mention goes last, deterministic order
    mentioned = set(ranking)
    ordered += [by_id[r] for r in deterministic_ranking(facts) if r not in mentioned]

    cautions = {frozenset(p) for p in (pairing_cautions or []) if len(set(p)) == 2}
    uncertainty = uncertainty_ids or set()
    cap = constraints.budget_cap
    flags: list[str] = []

    if len(ordered) < constraints.slots:
        flags.append("insufficient_pool")

    def try_assemble(respect_cautions: bool) -> tuple[list[RecipeFacts], list[dict]]:
        selected: list[RecipeFacts] = []
        skipped_for_budget: list[dict] = []
        bought = set(pantry_ingredient_ids)
        total = 0.0
        probes_needed = min(constraints.explore_quota, 1 if uncertainty else 0)
        quick_have = 0

        for f in ordered:
            slots_left = constraints.slots - len(selected)
            if slots_left == 0:
                break
            quick_needed = max(0, constraints.busy_nights - quick_have)
            is_uncertain = f.recipe_id in uncertainty

            # reserve tail slots for unmet quotas
            if quick_needed >= slots_left and not f.is_quick:
                continue
            if probes_needed >= slots_left and not is_uncertain and quick_needed < slots_left:
                continue
            if respect_cautions and _cautioned(f.recipe_id, selected, cautions):
                continue
            if sum(1 for s in selected if s.protein == f.protein) >= constraints.max_same_protein:
                continue
            delta = _marginal(f, bought, prices)
            if cap is not None and total + delta > cap:
                skipped_for_budget.append({"facts": f, "rank": len(skipped_for_budget)})
                continue

            selected.append(f)
            bought |= f.ingredient_ids
            total = round(total + delta, 2)
            if f.is_quick:
                quick_have += 1
            if is_uncertain and probes_needed > 0:
                probes_needed -= 1
        return selected, skipped_for_budget

    selected, skipped_for_budget = try_assemble(respect_cautions=True)
    if len(selected) < constraints.slots:
        retry, retry_skips = try_assemble(respect_cautions=False)
        if len(retry) > len(selected):
            selected, skipped_for_budget = retry, retry_skips

    status = "feasible"
    if len(selected) < constraints.slots:
        status = "insufficient_pool" if "insufficient_pool" in flags else "infeasible"
        if status == "infeasible":
            flags.append("budget_too_tight")

    total_cost = union_cost(selected, pantry_ingredient_ids, prices)

    # explore slot: mark the highest-uncertainty selected recipe as the probe
    probe_id = None
    if constraints.explore_quota > 0 and uncertainty:
        in_bundle = [f for f in selected if f.recipe_id in uncertainty]
        if in_bundle:
            probe_id = min(in_bundle, key=lambda f: f.confidence).recipe_id

    items = [BundleItem(facts=f, is_probe=(f.recipe_id == probe_id)) for f in selected]

    _assign_alternates(items, ordered, constraints, pantry_ingredient_ids, prices, cautions, cap)
    _assign_days(items, constraints)

    budget_trade = _compute_trade(
        items, skipped_for_budget, pantry_ingredient_ids, prices, cap
    )

    return Bundle(
        status=status,
        items=items,
        total_cost=total_cost,
        grocery_ingredient_ids=sorted(
            set().union(*(i.facts.ingredient_ids for i in items)) - pantry_ingredient_ids
            if items else set()
        ),
        budget_trade=budget_trade,
        flags=flags,
    )


def _assign_alternates(
    items: list[BundleItem],
    ordered: list[RecipeFacts],
    constraints: Constraints,
    pantry: set[str],
    prices: dict[str, float],
    cautions: set[frozenset],
    cap: float | None,
) -> None:
    """Per slot: next-ranked substitutes that keep the bundle feasible if swapped in."""
    in_bundle = {i.facts.recipe_id for i in items}
    for item in items:
        rest = [i.facts for i in items if i is not item]
        alts: list[str] = []
        for cand in ordered:
            if len(alts) >= ALTERNATES_PER_SLOT:
                break
            if cand.recipe_id in in_bundle or cand.recipe_id in alts:
                continue
            if item.facts.is_quick and not cand.is_quick:
                continue
            if _cautioned(cand.recipe_id, rest, cautions):
                continue
            if (
                sum(1 for s in rest if s.protein == cand.protein)
                >= constraints.max_same_protein
            ):
                continue
            if cap is not None and union_cost(rest + [cand], pantry, prices) > cap:
                continue
            alts.append(cand.recipe_id)
        item.alternates = alts


def _assign_days(items: list[BundleItem], constraints: Constraints) -> None:
    """Soft rhythm: perishable-rescuers early in the week, high effort on weekends."""
    by_effort = sorted(items, key=lambda i: -i.facts.total_time_mins)
    weekend, weekday = [], []
    for i in by_effort:
        if not i.facts.is_quick and len(weekend) < len(WEEKEND_DAYS):
            weekend.append(i)
        else:
            weekday.append(i)
    for i, day in zip(weekend, WEEKEND_DAYS):
        i.suggested_day = day
    weekday.sort(key=lambda i: -i.facts.expiring_rescued)
    for i, day in zip(weekday, WEEKDAYS):
        i.suggested_day = day


def _compute_trade(
    items: list[BundleItem],
    skipped_for_budget: list[dict],
    pantry: set[str],
    prices: dict[str, float],
    cap: float | None,
) -> dict | None:
    """If the cap forced a high-ranked meal out, offer it back as a one-tap trade:
    swap it for the least-preferred bundle item and report the code-computed delta."""
    if not skipped_for_budget or not items or cap is None:
        return None
    dropped = skipped_for_budget[0]["facts"]
    base = union_cost([i.facts for i in items], pantry, prices)
    swapped = union_cost([i.facts for i in items[:-1]] + [dropped], pantry, prices)
    delta = round(swapped - base, 2)
    if delta <= 0:
        return None
    return {
        "recipe_id": dropped.recipe_id,
        "title": dropped.title,
        "swap_out_recipe_id": items[-1].facts.recipe_id,
        "delta": delta,
    }
