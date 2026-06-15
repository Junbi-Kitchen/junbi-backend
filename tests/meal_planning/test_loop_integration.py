"""End-to-end loop integration test against the real Postgres.

Creates a throwaway user, runs week 1 (deterministic ranking — no API key
needed), reacts (never_show, swap, cook-the-probe), closes the week through
the learning step, generates week 2, and asserts the loop's consequences:
never_show is gone, the allergen never appears, affinities moved.

Skips when no real DATABASE_URL is configured (e.g. CI without secrets).
Requires the catalog seed (make seed-catalog).
"""

import uuid

import pytest

_skip_reason = ""
try:
    from app.config import settings

    if not (settings.DATABASE_URL or "").startswith("postgres"):
        # show only the scheme, never credentials
        _scheme = (settings.DATABASE_URL or "<empty>").split("://")[0]
        _skip_reason = f"DATABASE_URL is not a Postgres URL (scheme: {_scheme!r})"
except Exception as exc:  # missing .env entirely
    _skip_reason = f"settings unavailable: {type(exc).__name__}: {exc}"

pytestmark = pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason)

TEST_USER = f"test-loop-{uuid.uuid4().hex[:8]}"


async def _setup_user():
    from app.agents.ingredient_resolver.resources import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO user_profiles (id, email, display_name) VALUES (%s, %s, %s)",
                (TEST_USER, f"{TEST_USER}@test.local", "Loop Test"),
            )
            await cur.execute(
                """
                INSERT INTO user_preferences
                    (user_id, dietary_tags, allergies, cuisines, household_size, weekly_budget)
                VALUES (%s, '{}', %s, '{}', 2, 90.00)
                """,
                (TEST_USER, ["shellfish"]),
            )


async def _teardown_user():
    from app.agents.ingredient_resolver.resources import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_profiles WHERE id = %s", (TEST_USER,))


async def test_full_loop_week1_to_week2():
    from datetime import date

    from app.agents.meal_planner.runner import run_week_planner
    from app.services.meal_planning import learning, queries

    await _setup_user()
    try:
        # --- week 1 ---
        result = await run_week_planner(TEST_USER, {"slots": 5, "busy_nights": 2})
        bundle = result["bundle"]
        assert bundle["status"] == "feasible"
        assert len(bundle["items"]) == 5
        assert bundle["budget_cap"] == 90.0
        assert bundle["total_cost"] <= 90.0

        titles_w1 = [i["title"].lower() for i in bundle["items"]]
        assert not any("shrimp" in t or "pad thai" in t for t in titles_w1)

        plan_id = await queries.persist_bundle(
            TEST_USER, date(2026, 6, 8), bundle, result["ranking_artifact"], 90.0
        )

        # --- reactions: the planted week-1 behavior ---
        hated = bundle["items"][1]
        await queries.log_event(
            TEST_USER, "never_show", plan_id=plan_id, recipe_id=hated["recipe_id"]
        )
        probe = next((i for i in bundle["items"] if i["is_probe"]), None)
        if probe:
            await queries.log_event(
                TEST_USER, "marked_cooked", plan_id=plan_id,
                recipe_id=probe["recipe_id"], is_probe=True,
            )
        loved = bundle["items"][0]
        await queries.log_event(
            TEST_USER, "marked_cooked", plan_id=plan_id, recipe_id=loved["recipe_id"]
        )

        # --- close week: learning ---
        await queries.update_plan_status(TEST_USER, plan_id, "closed")
        learned = await learning.run_weekly_update(TEST_USER, plan_id)
        assert learned["recipesScored"] >= 2

        snapshot = await learning.model_snapshot(TEST_USER)
        scores = {r["recipeId"]: r["score"] for r in snapshot["topRecipes"] + snapshot["bottomRecipes"]}
        assert scores.get(hated["recipe_id"], 0) < 0
        assert scores.get(loved["recipe_id"], 0) > 0

        # --- week 2: consequences visible ---
        result2 = await run_week_planner(TEST_USER, {"slots": 5, "busy_nights": 2})
        bundle2 = result2["bundle"]
        ids_w2 = {i["recipe_id"] for i in bundle2["items"]}
        assert hated["recipe_id"] not in ids_w2, "never_show leaked into week 2"
        titles_w2 = [i["title"].lower() for i in bundle2["items"]]
        assert not any("shrimp" in t or "pad thai" in t for t in titles_w2)
        assert bundle2["total_cost"] <= 90.0
    finally:
        await _teardown_user()
