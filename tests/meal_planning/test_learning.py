"""Planted-pattern persona tests for the learning replay — pure, no DB.

Each test scripts a synthetic household's event stream and asserts the model
learns the planted pattern. This is how the loop's machinery is proven
without waiting weeks for real users.
"""

from datetime import datetime, timezone

from app.services.meal_planning.learning import (
    FEATURE_DEFAULTS,
    _validate_narrative,
    replay_events,
)

NOW = datetime(2026, 6, 10, tzinfo=timezone.utc)


def ev(event_type, recipe_id=None, is_probe=False, payload=None, plan_id="p1"):
    return {
        "event_type": event_type,
        "recipe_id": recipe_id,
        "is_probe": is_probe,
        "payload": payload or {},
        "plan_id": plan_id,
        "created_at": NOW,
    }


def test_always_swaps_fish_out_persona():
    """Planted: fish gets swapped out every week, replacement gets cooked."""
    events = []
    for week in range(3):
        events += [
            ev("item_swapped_out", "fish-recipe", payload={"replacedBy": "chicken-recipe"}),
            ev("item_swapped_in", "chicken-recipe"),
            ev("marked_cooked", "chicken-recipe"),
        ]
    result = replay_events(events)
    fish = result.affinities["fish-recipe"]
    chicken = result.affinities["chicken-recipe"]
    assert fish.score < -1.5, "fish affinity should fall hard"
    assert chicken.score > 2.0, "replacement should rise"
    assert chicken.confidence > fish.confidence


def test_never_show_is_the_strongest_negative():
    result = replay_events(
        [ev("never_show", "hated"), ev("swipe_pass", "meh"), ev("item_swapped_out", "disliked")]
    )
    a = result.affinities
    assert a["hated"].score < a["disliked"].score < a["meh"].score < 0


def test_probe_outcomes_are_weighted_up_when_cooked():
    cooked_probe = replay_events([ev("marked_cooked", "x", is_probe=True)])
    cooked_plain = replay_events([ev("marked_cooked", "x")])
    assert cooked_probe.affinities["x"].score > cooked_plain.affinities["x"].score


def test_skipped_probe_is_weak_evidence():
    skipped_probe = replay_events([ev("present_unopened", "x", is_probe=True)])
    skipped_plain = replay_events([ev("present_unopened", "x")])
    assert abs(skipped_probe.affinities["x"].score) < abs(skipped_plain.affinities["x"].score)


def test_budget_trade_persona_moves_budget_strictness():
    taker = replay_events([ev("budget_trade_taken", "t") for _ in range(3)])
    decliner = replay_events([ev("budget_trade_declined", "t") for _ in range(3)])
    assert taker.features["budget_strictness"] < FEATURE_DEFAULTS["budget_strictness"]
    assert decliner.features["budget_strictness"] > FEATURE_DEFAULTS["budget_strictness"]


def test_midweek_swap_credits_replacement_and_quick_need():
    result = replay_events(
        [ev("midweek_swap", "planned", payload={"replacedBy": "quick-alt"})]
    )
    assert result.affinities["planned"].score < 0
    assert result.affinities["quick-alt"].score > 0
    assert result.features["quick_meal_need"] > FEATURE_DEFAULTS["quick_meal_need"]


def test_plan_accepted_credits_each_recipe():
    result = replay_events(
        [ev("plan_accepted", payload={"recipeIds": ["a", "b", "c"]})]
    )
    assert all(result.affinities[r].score > 0 for r in ("a", "b", "c"))


def test_replay_is_deterministic_and_score_clamped():
    events = [ev("rated_up", "fav") for _ in range(50)]
    r1, r2 = replay_events(events), replay_events(events)
    assert r1.affinities["fav"].score == r2.affinities["fav"].score == 5.0  # clamp
    assert r1.affinities["fav"].confidence == 1.0


def test_narrative_validation_blocks_money_and_caps_length():
    assert _validate_narrative("They love quick comfort meals.") is not None
    assert _validate_narrative("Their budget is $90 a week.") is None  # numbers live in L2
    assert _validate_narrative("") is None
    assert len(_validate_narrative("x" * 5000)) <= 800
