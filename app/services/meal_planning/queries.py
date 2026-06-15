"""
Data access for the meal-planning loop. All SQL lives here.

L1 hard-constraint filtering (allergies, dislikes, dietary locks, never-show)
happens in `load_context`'s pool query — recipes that violate a constraint
never reach the engine or the LLM. Connections come from the shared
pool-provider seam so this works in-process (FastAPI) and standalone
(adk web) alike.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

# Shared host/standalone pool seam (see its docstring); graduate to a neutral
# module when a third agent needs it.
from app.agents.ingredient_resolver.resources import get_pool

logger = logging.getLogger(__name__)

MEAL_TYPE = "dinner"
UNCERTAINTY_CONFIDENCE = 0.3
EXPIRY_WINDOW_DAYS = 7

# Allergy / dislike terms -> ingredient-name ILIKE patterns. An allergy filters
# the pool by construction; an unknown term falls back to its own name.
ALLERGEN_PATTERNS: dict[str, list[str]] = {
    "shellfish": ["%shrimp%", "%crab%", "%lobster%", "%clam%", "%mussel%", "%oyster%", "%scallop%", "%prawn%"],
    "peanuts": ["%peanut%"],
    "peanut": ["%peanut%"],
    "tree nuts": ["%cashew%", "%almond%", "%walnut%", "%pecan%", "%pistachio%", "%hazelnut%", "%macadamia%"],
    "nuts": ["%cashew%", "%almond%", "%walnut%", "%pecan%", "%pistachio%", "%hazelnut%", "%peanut%"],
    "fish": ["%salmon%", "%cod %", "%cod", "%tilapia%", "%tuna%", "%anchov%", "%sardine%", "%fish sauce%", "%fish fillet%"],
    "dairy": ["%milk%", "%cream%", "%cheese%", "%butter%", "%yogurt%"],
    "eggs": ["%egg%"],
    "egg": ["%egg%"],
    "sesame": ["%sesame%", "%tahini%"],
    "soy": ["%soy%", "%tofu%", "%edamame%", "%miso%"],
    "gluten": ["%flour%", "%pasta%", "%spaghetti%", "%penne%", "%rigatoni%", "%noodle%", "%bread%", "%bun%", "%pita%", "%tortilla%", "%couscous%", "%panko%", "%baguette%", "%flatbread%"],
}

VEGETARIAN_OK_PROTEINS = ("protein:vegetarian", "protein:tofu")


def _patterns_for(terms: list[str]) -> list[str]:
    patterns: list[str] = []
    for term in terms or []:
        t = term.strip().lower()
        if not t:
            continue
        patterns.extend(ALLERGEN_PATTERNS.get(t, [f"%{t}%"]))
    return patterns


def _novelty(last_cooked: datetime | None) -> float:
    if last_cooked is None:
        return 1.0
    days = (datetime.now(timezone.utc) - last_cooked).days
    return round(min(1.0, max(0.0, days / 28.0)), 3)


async def load_context(user_id: str) -> dict[str, Any]:
    """Everything planning needs: prefs, L1-filtered pool, pantry, prices, L2/L3."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT dietary_tags, allergies, cuisines, dislikes,
                       household_size, weekly_budget
                FROM user_preferences WHERE user_id = %s
                """,
                (user_id,),
            )
            prefs = await cur.fetchone() or {}

            await cur.execute(
                """
                SELECT DISTINCT recipe_id FROM plan_events
                WHERE user_id = %s AND event_type = 'never_show' AND recipe_id IS NOT NULL
                """,
                (user_id,),
            )
            never_show = [str(r["recipe_id"]) for r in await cur.fetchall()]

            blocked = _patterns_for((prefs.get("allergies") or []) + (prefs.get("dislikes") or []))
            dietary = [t.lower() for t in (prefs.get("dietary_tags") or [])]

            pool_sql = """
                SELECT r.id::text AS id, r.title, r.cuisine,
                       COALESCE(r.prep_time_mins, 0) + COALESCE(r.cook_time_mins, 0) AS total_time_mins,
                       r.servings,
                       array_agg(DISTINCT ri.ingredient_id::text) AS ingredient_ids,
                       COALESCE(array_agg(DISTINCT t.tag) FILTER (WHERE t.tag IS NOT NULL), '{}') AS tags
                FROM recipes r
                JOIN recipe_ingredients ri ON ri.recipe_id = r.id
                LEFT JOIN recipe_tags t ON t.recipe_id = r.id
                WHERE (r.is_global OR r.created_by = %(user_id)s)
                  AND NOT (r.id = ANY(%(never_show)s::uuid[]))
                  AND NOT EXISTS (
                      SELECT 1 FROM recipe_ingredients ri2
                      JOIN ingredients i ON i.id = ri2.ingredient_id
                      WHERE ri2.recipe_id = r.id
                        AND (i.name ILIKE ANY(%(blocked)s) OR ri2.display_text ILIKE ANY(%(blocked)s))
                  )
                GROUP BY r.id
            """
            await cur.execute(
                pool_sql,
                {
                    "user_id": user_id,
                    "never_show": never_show or ["00000000-0000-0000-0000-000000000000"],
                    "blocked": blocked or ["\x00-never-matches"],
                },
            )
            rows = await cur.fetchall()

            if "vegetarian" in dietary or "vegan" in dietary:
                rows = [
                    r for r in rows
                    if any(t in VEGETARIAN_OK_PROTEINS for t in r["tags"])
                    or ("vegan" in r["tags"] if "vegan" in dietary else False)
                ]
            if "gluten-free" in dietary:
                gluten = _patterns_for(["gluten"])
                await cur.execute(
                    """
                    SELECT DISTINCT ri.recipe_id::text AS id
                    FROM recipe_ingredients ri JOIN ingredients i ON i.id = ri.ingredient_id
                    WHERE i.name ILIKE ANY(%s)
                    """,
                    (gluten,),
                )
                gluten_recipes = {r["id"] for r in await cur.fetchall()}
                rows = [r for r in rows if r["id"] not in gluten_recipes]

            await cur.execute(
                """
                SELECT recipe_id::text AS id, max(created_at) AS last_cooked
                FROM plan_events
                WHERE user_id = %s AND event_type = 'marked_cooked' AND recipe_id IS NOT NULL
                GROUP BY recipe_id
                """,
                (user_id,),
            )
            last_cooked = {r["id"]: r["last_cooked"] for r in await cur.fetchall()}

            recipes = []
            for r in rows:
                protein = next(
                    (t.removeprefix("protein:") for t in r["tags"] if t.startswith("protein:")),
                    "other",
                )
                recipes.append(
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "cuisine": r["cuisine"],
                        "protein": protein,
                        "total_time_mins": r["total_time_mins"],
                        "servings": r["servings"],
                        "ingredient_ids": r["ingredient_ids"],
                        "tags": [t for t in r["tags"] if not t.startswith("protein:")],
                        "novelty": _novelty(last_cooked.get(r["id"])),
                    }
                )

            pool_ing_ids = sorted({i for r in recipes for i in r["ingredient_ids"]})
            prices: dict[str, float] = {}
            ing_names: dict[str, str] = {}
            if pool_ing_ids:
                await cur.execute(
                    """
                    SELECT id::text AS id, name, estimated_price
                    FROM ingredients WHERE id = ANY(%s::uuid[])
                    """,
                    (pool_ing_ids,),
                )
                for r in await cur.fetchall():
                    ing_names[r["id"]] = r["name"]
                    if r["estimated_price"] is not None:
                        prices[r["id"]] = float(r["estimated_price"])

            await cur.execute(
                """
                SELECT p.ingredient_id::text AS id, p.expiry_date, p.name,
                       i.estimated_price
                FROM pantry_items p JOIN ingredients i ON i.id = p.ingredient_id
                WHERE p.user_id = %s AND p.is_active
                """,
                (user_id,),
            )
            pantry_ids: set[str] = set()
            expiring: dict[str, tuple[float, int]] = {}
            today = date.today()
            for r in await cur.fetchall():
                pantry_ids.add(r["id"])
                if r["expiry_date"] is not None:
                    days_left = (r["expiry_date"] - today).days
                    if days_left <= EXPIRY_WINDOW_DAYS:
                        value = float(r["estimated_price"] or 2.0)
                        expiring[r["id"]] = (value, max(days_left, 0))

            await cur.execute(
                "SELECT recipe_id::text AS id, score, confidence FROM user_recipe_affinity WHERE user_id = %s",
                (user_id,),
            )
            affinities = {
                r["id"]: (float(r["score"]), float(r["confidence"])) for r in await cur.fetchall()
            }

            await cur.execute(
                """
                SELECT narrative FROM user_model_narratives
                WHERE user_id = %s ORDER BY version DESC LIMIT 1
                """,
                (user_id,),
            )
            row = await cur.fetchone()
            narrative = row["narrative"] if row else ""

            await cur.execute(
                "SELECT feature_key, value, confidence FROM user_model_features WHERE user_id = %s",
                (user_id,),
            )
            features = {
                r["feature_key"]: {"value": float(r["value"]), "confidence": float(r["confidence"])}
                for r in await cur.fetchall()
            }

    # Affinity priors for unscored recipes via embedding neighbors — low
    # confidence on purpose, so they stay eligible for the explore slot.
    unscored = [r["id"] for r in recipes if r["id"] not in affinities]
    if affinities and unscored:
        for rid, prior in (await embedding_priors(user_id, unscored)).items():
            affinities[rid] = (round(prior, 3), 0.1)

    uncertainty_ids = {
        r["id"]
        for r in recipes
        if affinities.get(r["id"], (0.0, 0.0))[1] < UNCERTAINTY_CONFIDENCE
    }

    return {
        "prefs": dict(prefs),
        "recipes": recipes,
        "prices": prices,
        "ingredient_names": ing_names,
        "pantry_ingredient_ids": pantry_ids,
        "expiring": expiring,
        "affinities": affinities,
        "uncertainty_ids": uncertainty_ids,
        "narrative": narrative,
        "features": features,
    }


async def persist_bundle(
    user_id: str,
    week_start: date,
    bundle: dict,
    ranking_artifact: dict,
    budget_target: float | None,
) -> str:
    """Write the proposed plan; replaces any existing plan for the same week."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM meal_plans WHERE user_id = %s AND week_start = %s",
                (user_id, week_start),
            )
            await cur.execute(
                """
                INSERT INTO meal_plans
                    (user_id, week_start, status, budget_target, generated_by,
                     ranking_artifact, computed_cost, budget_trade)
                VALUES (%s, %s, 'proposed', %s, 'ai', %s, %s, %s)
                RETURNING id::text AS id
                """,
                (
                    user_id,
                    week_start,
                    budget_target,
                    _json(ranking_artifact),
                    bundle["total_cost"],
                    _json(bundle.get("budget_trade")),
                ),
            )
            plan_id = (await cur.fetchone())["id"]
            for idx, item in enumerate(bundle["items"]):
                await cur.execute(
                    """
                    INSERT INTO meal_plan_items
                        (meal_plan_id, recipe_id, suggested_day, meal_type, sort_order,
                         is_probe, alternates, rationale, added_via, computed_cost)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'planner', %s)
                    """,
                    (
                        plan_id,
                        item["recipe_id"],
                        item.get("suggested_day"),
                        MEAL_TYPE,
                        idx,
                        item.get("is_probe", False),
                        _json(item.get("alternates", [])),
                        item.get("rationale"),
                        item.get("cost_to_add"),
                    ),
                )
    return plan_id


async def get_plan(user_id: str, plan_id: str | None = None) -> dict | None:
    """Fetch a plan (or the latest) with its items and recipe titles."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if plan_id:
                await cur.execute(
                    "SELECT * FROM meal_plans WHERE id = %s AND user_id = %s",
                    (plan_id, user_id),
                )
            else:
                await cur.execute(
                    "SELECT * FROM meal_plans WHERE user_id = %s ORDER BY week_start DESC LIMIT 1",
                    (user_id,),
                )
            plan = await cur.fetchone()
            if not plan:
                return None
            await cur.execute(
                """
                SELECT mpi.*, r.title, r.cuisine,
                       COALESCE(r.prep_time_mins,0)+COALESCE(r.cook_time_mins,0) AS total_time_mins
                FROM meal_plan_items mpi LEFT JOIN recipes r ON r.id = mpi.recipe_id
                WHERE mpi.meal_plan_id = %s ORDER BY mpi.sort_order
                """,
                (plan["id"],),
            )
            items = await cur.fetchall()
    plan = dict(plan)
    plan["items"] = [dict(i) for i in items]
    return plan


async def log_event(
    user_id: str,
    event_type: str,
    *,
    plan_id: str | None = None,
    recipe_id: str | None = None,
    is_probe: bool = False,
    payload: dict | None = None,
) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO plan_events (user_id, meal_plan_id, recipe_id, event_type, is_probe, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, plan_id, recipe_id, event_type, is_probe, _json(payload)),
            )


async def update_plan_status(user_id: str, plan_id: str, status: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE meal_plans
                SET status = %s, updated_at = now(),
                    closed_at = CASE WHEN %s = 'closed' THEN now() ELSE closed_at END
                WHERE id = %s AND user_id = %s
                """,
                (status, status, plan_id, user_id),
            )


async def swap_item(
    user_id: str,
    plan_id: str,
    out_recipe_id: str,
    in_recipe_id: str,
    added_via: str = "user_swap",
) -> bool:
    """Replace one bundle item; returns False if the item wasn't found."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE meal_plan_items
                SET recipe_id = %s, added_via = %s, is_probe = false, rationale = NULL
                WHERE meal_plan_id = %s AND recipe_id = %s
                  AND meal_plan_id IN (SELECT id FROM meal_plans WHERE user_id = %s)
                RETURNING id
                """,
                (in_recipe_id, added_via, plan_id, out_recipe_id, user_id),
            )
            return await cur.fetchone() is not None


async def recompute_plan_cost(user_id: str, plan_id: str) -> float:
    """Authoritative union cost of the plan's current recipes, minus pantry.
    Always code-recomputed after any mutation — never trusted from a client."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH need AS (
                    SELECT DISTINCT ri.ingredient_id
                    FROM meal_plan_items mpi
                    JOIN recipe_ingredients ri ON ri.recipe_id = mpi.recipe_id
                    WHERE mpi.meal_plan_id = %s
                    EXCEPT
                    SELECT ingredient_id FROM pantry_items
                    WHERE user_id = %s AND is_active
                )
                SELECT COALESCE(SUM(i.estimated_price), 0) AS total
                FROM need JOIN ingredients i ON i.id = need.ingredient_id
                """,
                (plan_id, user_id),
            )
            total = float((await cur.fetchone())["total"])
            await cur.execute(
                "UPDATE meal_plans SET computed_cost = %s, updated_at = now() WHERE id = %s AND user_id = %s",
                (total, plan_id, user_id),
            )
    return round(total, 2)


async def set_trade_taken(user_id: str, plan_id: str, taken: bool) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE meal_plans
                SET budget_trade = budget_trade || jsonb_build_object('taken', %s::boolean),
                    updated_at = now()
                WHERE id = %s AND user_id = %s AND budget_trade IS NOT NULL
                """,
                (taken, plan_id, user_id),
            )


async def get_recipe_titles(recipe_ids: list[str]) -> dict[str, str]:
    if not recipe_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text AS id, title FROM recipes WHERE id = ANY(%s::uuid[])",
                (recipe_ids,),
            )
            return {r["id"]: r["title"] for r in await cur.fetchall()}


async def reset_user_loop_state(user_id: str) -> None:
    """Wipe one user's loop state (events, plans, learned model). Catalog,
    pantry, and preferences are untouched. Used by the demo reset."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for table in (
                "plan_events",
                "meal_plans",  # items cascade
                "user_recipe_affinity",
                "user_model_features",
                "user_model_narratives",
                "model_update_log",
            ):
                await cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))


# ------------------------------------------------------------ learning I/O


async def fetch_all_events(user_id: str) -> list[dict]:
    """The full signal log, oldest first — learned state replays from this."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id::text AS id, meal_plan_id::text AS plan_id,
                       recipe_id::text AS recipe_id, event_type, is_probe,
                       payload, created_at
                FROM plan_events WHERE user_id = %s ORDER BY created_at, id
                """,
                (user_id,),
            )
            return [dict(r) for r in await cur.fetchall()]


async def replace_affinities(user_id: str, affinities: dict[str, dict]) -> None:
    """Full rewrite of the learned projection (events are the source of truth)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM user_recipe_affinity WHERE user_id = %s", (user_id,))
            await cur.executemany(
                """
                INSERT INTO user_recipe_affinity
                    (user_id, recipe_id, score, confidence, last_signal_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (user_id, rid, a["score"], a["confidence"], a["last_signal_at"])
                    for rid, a in affinities.items()
                ],
            )


async def upsert_feature(
    user_id: str, feature_key: str, value: float, confidence: float,
    *, source: str, delta: float, evidence: str | None = None,
) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_model_features (user_id, feature_key, value, confidence, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id, feature_key)
                DO UPDATE SET value = EXCLUDED.value, confidence = EXCLUDED.confidence, updated_at = now()
                """,
                (user_id, feature_key, value, confidence),
            )
            await cur.execute(
                """
                INSERT INTO model_update_log (user_id, source, feature_key, delta, evidence)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, source, feature_key, delta, evidence),
            )


async def insert_narrative(user_id: str, narrative: str) -> int:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_model_narratives (user_id, version, narrative)
                VALUES (%s, COALESCE(
                    (SELECT max(version) + 1 FROM user_model_narratives WHERE user_id = %s), 1
                ), %s)
                RETURNING version
                """,
                (user_id, user_id, narrative),
            )
            return (await cur.fetchone())["version"]


async def affinity_snapshot(user_id: str, limit: int = 5) -> dict:
    """Top/bottom learned recipes with titles, plus features + narrative."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.recipe_id::text AS recipe_id, a.score, a.confidence, r.title
                FROM user_recipe_affinity a JOIN recipes r ON r.id = a.recipe_id
                WHERE a.user_id = %s ORDER BY a.score DESC
                """,
                (user_id,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.execute(
                "SELECT feature_key, value, confidence, updated_at FROM user_model_features WHERE user_id = %s",
                (user_id,),
            )
            features = [dict(r) for r in await cur.fetchall()]
            await cur.execute(
                """
                SELECT narrative, version, created_at FROM user_model_narratives
                WHERE user_id = %s ORDER BY version DESC LIMIT 1
                """,
                (user_id,),
            )
            narrative = await cur.fetchone()
    return {
        "top": rows[:limit],
        "bottom": rows[-limit:][::-1] if len(rows) > limit else [],
        "features": features,
        "narrative": dict(narrative) if narrative else None,
    }


async def get_swipe_deck(user_id: str, size: int = 15) -> list[dict]:
    """Onboarding deck: one recipe per cuisine first (spread), random fill."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id::text AS id, title, cuisine, description FROM (
                    SELECT id, title, cuisine, description,
                           row_number() OVER (PARTITION BY cuisine ORDER BY random()) AS rn,
                           random() AS rnd
                    FROM recipes WHERE is_global AND embedding IS NOT NULL
                ) t
                ORDER BY rn, rnd LIMIT %s
                """,
                (size,),
            )
            return [dict(r) for r in await cur.fetchall()]


async def embedding_priors(user_id: str, recipe_ids: list[str]) -> dict[str, float]:
    """Affinity prior for unscored recipes: similarity-weighted mean of the
    5 nearest scored neighbors in embedding space."""
    if not recipe_ids:
        return {}
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT r.id::text AS id,
                       SUM(a.score * sim.w) / NULLIF(SUM(sim.w), 0) AS prior
                FROM recipes r
                JOIN LATERAL (
                    SELECT a2.recipe_id, 1 - (r.embedding <=> r2.embedding) AS w
                    FROM user_recipe_affinity a2
                    JOIN recipes r2 ON r2.id = a2.recipe_id
                    WHERE a2.user_id = %s AND r2.embedding IS NOT NULL
                    ORDER BY r.embedding <=> r2.embedding LIMIT 5
                ) sim ON true
                JOIN user_recipe_affinity a ON a.recipe_id = sim.recipe_id AND a.user_id = %s
                WHERE r.id = ANY(%s::uuid[]) AND r.embedding IS NOT NULL
                GROUP BY r.id
                """,
                (user_id, user_id, recipe_ids),
            )
            return {r["id"]: float(r["prior"]) for r in await cur.fetchall() if r["prior"] is not None}


def _json(value) -> str | None:
    import json

    return json.dumps(value) if value is not None else None
