"""Candidate pre-selection (pool cap) tests — the bounded funnel stage
between L1 filtering and the LLM ranker."""

import random

from app.services.meal_planning.engine import Constraints, RecipeFacts
from app.services.meal_planning.planner import POOL_CAP, select_candidates


def make_facts(n=60, seed=2):
    rng = random.Random(seed)
    cuisines = ["italian", "mexican", "thai", "indian", "korean", "french"]
    facts = []
    for i in range(n):
        facts.append(
            RecipeFacts(
                recipe_id=f"r{i}",
                title=f"Recipe {i}",
                cuisine=cuisines[i % len(cuisines)],
                protein="chicken",
                total_time_mins=rng.choice([20, 25, 45, 70]),
                servings=4,
                ingredient_ids=frozenset({f"i{i}a", f"i{i}b"}),
                cost_to_add=round(rng.uniform(3, 30), 2),
                cost_band="med",
                expiring_rescued=0.0,
                affinity=round(rng.uniform(-2, 2), 2),
                confidence=round(rng.uniform(0.4, 1.0), 2),
                novelty=1.0,
            )
        )
    return facts


def constraints(**kw):
    return Constraints(
        slots=5,
        budget_cap=kw.pop("budget_cap", 90.0),
        busy_nights=kw.pop("busy_nights", 2),
        explore_quota=kw.pop("explore_quota", 1),
    )


def test_cap_is_enforced():
    facts = make_facts(60)
    out = select_candidates(facts, constraints(), set())
    assert len(out) == POOL_CAP


def test_small_pools_pass_through_untouched():
    facts = make_facts(12)
    out = select_candidates(facts, constraints(), set())
    assert out == facts


def test_waste_rescuers_survive_the_cap_despite_low_affinity():
    facts = make_facts(60)
    rescuer = RecipeFacts(
        recipe_id="rescuer", title="Rescuer", cuisine="other", protein="pork",
        total_time_mins=50, servings=4, ingredient_ids=frozenset({"x"}),
        cost_to_add=20.0, cost_band="high", expiring_rescued=6.5,
        affinity=-3.0, confidence=0.9, novelty=0.1,
    )
    out = select_candidates(facts + [rescuer], constraints(), set())
    assert any(f.recipe_id == "rescuer" for f in out)


def test_enough_quick_meals_survive_for_busy_weeks():
    facts = make_facts(60)
    out = select_candidates(facts, constraints(busy_nights=4), set())
    assert sum(1 for f in out if f.is_quick) >= 4


def test_uncertain_probes_survive_the_cap():
    facts = make_facts(60)
    probe = RecipeFacts(
        recipe_id="probe", title="Probe", cuisine="other", protein="fish",
        total_time_mins=40, servings=4, ingredient_ids=frozenset({"y"}),
        cost_to_add=15.0, cost_band="med", expiring_rescued=0.0,
        affinity=0.0, confidence=0.01, novelty=1.0,
    )
    out = select_candidates(facts + [probe], constraints(explore_quota=1), {"probe"})
    assert any(f.recipe_id == "probe" for f in out)


def test_selection_is_deterministic():
    facts = make_facts(60)
    a = select_candidates(facts, constraints(), set())
    b = select_candidates(facts, constraints(), set())
    assert [f.recipe_id for f in a] == [f.recipe_id for f in b]


def test_cuisine_diversity_in_fill_seats():
    facts = make_facts(60)
    out = select_candidates(facts, constraints(), set())
    assert len({f.cuisine for f in out}) >= 4
