"""Engine core tests — pure Python, no DB, no LLM, no API keys.

The safety-critical assertions live here: the budget cap and variety rules
can never be violated by construction, regardless of what ranking the LLM
produces.
"""

import random

from app.services.meal_planning.engine import (
    Bundle,
    Constraints,
    assemble,
    compute_pool_facts,
    deterministic_ranking,
    union_cost,
)


def make_pool(n=12, seed=1):
    """Synthetic pool with overlapping ingredients and varied costs/proteins."""
    rng = random.Random(seed)
    proteins = ["chicken", "beef", "pork", "vegetarian", "fish"]
    shared = [f"ing-shared-{i}" for i in range(6)]
    recipes = []
    for i in range(n):
        own = [f"ing-{i}-{j}" for j in range(4)]
        recipes.append(
            {
                "id": f"r{i}",
                "title": f"Recipe {i}",
                "cuisine": rng.choice(["italian", "mexican", "thai"]),
                "protein": proteins[i % len(proteins)],
                "total_time_mins": rng.choice([20, 25, 40, 60]),
                "servings": 4,
                "ingredient_ids": own + rng.sample(shared, 2),
                "novelty": 1.0,
                "tags": [],
            }
        )
    all_ids = {i for r in recipes for i in r["ingredient_ids"]}
    prices = {i: round(rng.uniform(1.0, 8.0), 2) for i in all_ids}
    return recipes, prices


def make_facts(recipes, prices, pantry=None, expiring=None, affinities=None):
    return compute_pool_facts(
        recipes, pantry or set(), expiring or {}, prices, affinities or {}
    )


def assemble_simple(facts, prices, ranking=None, **kw):
    constraints = Constraints(
        slots=kw.pop("slots", 5),
        budget_cap=kw.pop("budget_cap", None),
        busy_nights=kw.pop("busy_nights", 0),
        explore_quota=kw.pop("explore_quota", 0),
    )
    return assemble(
        ranking or deterministic_ranking(facts),
        facts,
        constraints,
        kw.pop("pantry", set()),
        prices,
        **kw,
    )


# ---------------------------------------------------------------- safety


def test_budget_cap_never_exceeded_across_random_pools():
    for seed in range(20):
        recipes, prices = make_pool(seed=seed)
        facts = make_facts(recipes, prices)
        for cap in (15.0, 30.0, 60.0, 100.0):
            bundle = assemble_simple(facts, prices, budget_cap=cap)
            assert bundle.total_cost <= cap, f"seed={seed} cap={cap}"


def test_adversarial_ranking_cannot_break_budget():
    """Even a worst-case (most-expensive-first) ranking stays under the cap."""
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    worst = [f.recipe_id for f in sorted(facts, key=lambda f: -f.cost_to_add)]
    bundle = assemble_simple(facts, prices, ranking=worst, budget_cap=40.0)
    assert bundle.total_cost <= 40.0


def test_unknown_ids_in_ranking_are_ignored():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    ranking = ["hallucinated-1"] + deterministic_ranking(facts) + ["hallucinated-2"]
    bundle = assemble_simple(facts, prices, ranking=ranking)
    pool_ids = {f.recipe_id for f in facts}
    assert all(i.facts.recipe_id in pool_ids for i in bundle.items)


def test_infeasible_budget_flagged_with_partial_bundle():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    bundle = assemble_simple(facts, prices, budget_cap=5.0)
    assert bundle.status == "infeasible"
    assert "budget_too_tight" in bundle.flags
    assert bundle.total_cost <= 5.0


def test_insufficient_pool_flagged():
    recipes, prices = make_pool(n=3)
    facts = make_facts(recipes, prices)
    bundle = assemble_simple(facts, prices, slots=5)
    assert bundle.status == "insufficient_pool"
    assert len(bundle.items) == 3


# ------------------------------------------------------------ composition


def test_coverage_and_variety_cap():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    bundle = assemble_simple(facts, prices, slots=5)
    assert bundle.status == "feasible"
    assert len(bundle.items) == 5
    proteins = [i.facts.protein for i in bundle.items]
    assert all(proteins.count(p) <= 2 for p in proteins)


def test_quick_meal_rule_for_busy_nights():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    bundle = assemble_simple(facts, prices, slots=5, busy_nights=3)
    quick = sum(1 for i in bundle.items if i.facts.is_quick)
    assert quick >= 3


def test_cost_synergy_shared_ingredients_counted_once():
    recipes = [
        {"id": "a", "title": "A", "cuisine": "x", "protein": "chicken",
         "total_time_mins": 20, "servings": 4, "ingredient_ids": ["i1", "i2"], "novelty": 1},
        {"id": "b", "title": "B", "cuisine": "x", "protein": "beef",
         "total_time_mins": 20, "servings": 4, "ingredient_ids": ["i2", "i3"], "novelty": 1},
    ]
    prices = {"i1": 2.0, "i2": 3.0, "i3": 4.0}
    facts = make_facts(recipes, prices)
    assert union_cost(facts, set(), prices) == 9.0  # i2 once, not twice
    bundle = assemble_simple(facts, prices, slots=2)
    assert bundle.total_cost == 9.0


def test_pantry_items_are_free():
    recipes, prices = make_pool()
    pantry = set(recipes[0]["ingredient_ids"])
    facts = make_facts(recipes, prices, pantry=pantry)
    f0 = next(f for f in facts if f.recipe_id == "r0")
    assert f0.cost_to_add == 0.0


def test_waste_rescue_prefers_expiring_in_deterministic_rank():
    recipes, prices = make_pool()
    target_ing = recipes[3]["ingredient_ids"][0]
    expiring = {target_ing: (4.0, 1)}  # $4 pack, expires tomorrow
    facts = make_facts(recipes, prices, expiring=expiring)
    ranking = deterministic_ranking(facts)
    assert ranking[0] == "r3"


# ------------------------------------------------------- explore & extras


def test_explore_slot_marks_one_probe():
    recipes, prices = make_pool()
    affinities = {f"r{i}": (1.0, 0.9) for i in range(12)}
    affinities["r7"] = (0.0, 0.05)  # the uncertain one
    facts = make_facts(recipes, prices, affinities=affinities)
    bundle = assemble_simple(
        facts, prices, explore_quota=1, uncertainty_ids={"r7"}
    )
    probes = [i for i in bundle.items if i.is_probe]
    assert len(probes) == 1
    assert probes[0].facts.recipe_id == "r7"


def test_no_probe_without_uncertainty():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    bundle = assemble_simple(facts, prices, explore_quota=1, uncertainty_ids=set())
    assert not any(i.is_probe for i in bundle.items)


def test_alternates_keep_bundle_feasible_when_swapped():
    recipes, prices = make_pool(n=16)
    facts = make_facts(recipes, prices)
    by_id = {f.recipe_id: f for f in facts}
    cap = 60.0
    bundle = assemble_simple(facts, prices, budget_cap=cap)
    for item in bundle.items:
        rest = [i.facts for i in bundle.items if i is not item]
        for alt in item.alternates:
            assert union_cost(rest + [by_id[alt]], set(), prices) <= cap


def test_budget_trade_offered_and_delta_correct():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    # rank the most expensive first so the cap forces it out
    ranking = [f.recipe_id for f in sorted(facts, key=lambda f: -f.cost_to_add)]
    mid_cap = sorted(f.cost_to_add for f in facts)[len(facts) // 2] * 4
    bundle = assemble_simple(facts, prices, ranking=ranking, budget_cap=mid_cap)
    if bundle.budget_trade:  # offered only when something was actually dropped
        by_id = {f.recipe_id: f for f in facts}
        trade = bundle.budget_trade
        kept = [i.facts for i in bundle.items if i.facts.recipe_id != trade["swap_out_recipe_id"]]
        new_cost = union_cost(kept + [by_id[trade["recipe_id"]]], set(), prices)
        assert abs(new_cost - (bundle.total_cost + trade["delta"])) < 0.011
        assert trade["delta"] > 0


def test_pairing_cautions_avoided_when_possible():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    ranking = deterministic_ranking(facts)
    cautioned = (ranking[0], ranking[1])
    bundle = assemble_simple(
        facts, prices, ranking=ranking, pairing_cautions=[cautioned]
    )
    ids = {i.facts.recipe_id for i in bundle.items}
    assert not (cautioned[0] in ids and cautioned[1] in ids)


def test_soft_rhythm_assigns_days():
    recipes, prices = make_pool()
    facts = make_facts(recipes, prices)
    bundle = assemble_simple(facts, prices)
    days = [i.suggested_day for i in bundle.items]
    assert all(d is None or 0 <= d <= 6 for d in days)
    assert len([d for d in days if d is not None]) == len(bundle.items)
    # high-effort meals land on weekend days when present
    for i in bundle.items:
        if i.suggested_day in (5, 6):
            assert not i.facts.is_quick or all(not x.facts.is_quick for x in bundle.items)


def test_deterministic_ranking_orders_by_affinity():
    recipes, prices = make_pool()
    affinities = {"r5": (3.0, 0.8), "r2": (2.0, 0.7)}
    facts = make_facts(recipes, prices, affinities=affinities)
    ranking = deterministic_ranking(facts)
    assert ranking[0] == "r5" and ranking[1] == "r2"
