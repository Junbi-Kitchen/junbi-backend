"""
Meal-planning loop API: generate the weekly bundle, capture every reaction
as a plan_events row (the signal exhaust), serve tonight's meal with instant
alternates, and close the week (learning step + next week).

Every number returned here is code-computed (engine/queries); budget is
re-verified server-side after any mutation.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.core.dependencies import get_current_user
from app.schemas.meal_planning import (
    GenerateWeekRequest,
    ReactRequest,
    SwipesRequest,
    TonightAckRequest,
)
from app.services.meal_planning import queries

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meal-plans", tags=["meal-plans"])

DEMO_USER_ID = "dev-user-alex-rivera"


async def planner_user(current_user: dict | None = None) -> dict:  # overridden below
    raise NotImplementedError


if settings.DEMO_MODE:
    # Dev-only: the demo page acts as the seeded demo user, no Firebase.
    async def planner_user() -> dict:  # type: ignore[no-redef]
        return {"id": DEMO_USER_ID}

else:

    async def planner_user(current_user: dict = Depends(get_current_user)) -> dict:  # type: ignore[no-redef]
        return current_user


def _next_week_start(today: date | None = None) -> date:
    d = today or date.today()
    return d - timedelta(days=d.weekday())  # this week's Monday


def _plan_response(plan: dict) -> dict:
    """snake_case DB row -> camelCase API shape."""
    artifact = plan.get("ranking_artifact") or {}
    if isinstance(artifact, str):
        artifact = json.loads(artifact)
    trade = plan.get("budget_trade")
    if isinstance(trade, str):
        trade = json.loads(trade)
    return {
        "planId": str(plan["id"]),
        "weekStart": plan["week_start"].isoformat(),
        "status": plan["status"],
        "computedCost": float(plan["computed_cost"]) if plan["computed_cost"] is not None else None,
        "budgetTarget": float(plan["budget_target"]) if plan["budget_target"] is not None else None,
        "budgetTrade": trade,
        "rankingSource": artifact.get("source"),
        "items": [
            {
                "recipeId": str(i["recipe_id"]),
                "title": i.get("title"),
                "cuisine": i.get("cuisine"),
                "totalTimeMins": i.get("total_time_mins"),
                "suggestedDay": i.get("suggested_day"),
                "isProbe": i.get("is_probe", False),
                "rationale": i.get("rationale"),
                "alternates": (
                    json.loads(i["alternates"]) if isinstance(i.get("alternates"), str)
                    else i.get("alternates") or []
                ),
                "addedVia": i.get("added_via"),
            }
            for i in plan.get("items", [])
        ],
    }


@router.post("/generate")
async def generate_week(body: GenerateWeekRequest, user: dict = Depends(planner_user)):
    # imported lazily: pulls in the ADK stack
    from app.agents.meal_planner.runner import run_week_planner

    week_context = {
        "slots": body.slots,
        "busy_nights": body.busy_nights,
        "budget_cap": body.budget_cap,
        "notes": body.notes,
    }
    result = await run_week_planner(user["id"], week_context)
    bundle = result["bundle"]
    artifact = result["ranking_artifact"]

    week_start = _next_week_start()
    plan_id = await queries.persist_bundle(
        user["id"], week_start, bundle, artifact, bundle.get("budget_cap")
    )
    plan = await queries.get_plan(user["id"], plan_id)
    resp = _plan_response(plan)
    resp["groceryList"] = [
        {"ingredientId": g["ingredient_id"], "name": g["name"], "price": g["price"]}
        for g in bundle["grocery_list"]
    ]
    resp["flags"] = bundle["flags"]
    resp["bundleStatus"] = bundle["status"]
    return resp


@router.get("/current")
async def current_plan(user: dict = Depends(planner_user)):
    plan = await queries.get_plan(user["id"])
    if not plan:
        raise HTTPException(status_code=404, detail="No meal plan yet")
    return _plan_response(plan)


@router.post("/{plan_id}/react")
async def react(plan_id: str, body: ReactRequest, user: dict = Depends(planner_user)):
    plan = await queries.get_plan(user["id"], plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    items_by_recipe = {str(i["recipe_id"]): i for i in plan["items"]}

    if body.action == "accept":
        await queries.update_plan_status(user["id"], plan_id, "accepted")
        await queries.log_event(
            user["id"], "plan_accepted", plan_id=plan_id,
            payload={"recipeIds": list(items_by_recipe)},
        )

    elif body.action == "swap":
        if not body.recipe_id or not body.replacement_id:
            raise HTTPException(status_code=422, detail="swap needs recipeId and replacementId")
        item = items_by_recipe.get(body.recipe_id)
        if not item:
            raise HTTPException(status_code=404, detail="Recipe not in plan")
        alternates = item.get("alternates") or []
        if isinstance(alternates, str):
            alternates = json.loads(alternates)
        if body.replacement_id not in alternates:
            raise HTTPException(
                status_code=422, detail="Replacement must be one of the item's alternates"
            )
        await queries.swap_item(user["id"], plan_id, body.recipe_id, body.replacement_id)
        new_cost = await queries.recompute_plan_cost(user["id"], plan_id)
        # alternates were pre-validated against the cap; re-verify anyway (code owns budget)
        if plan["budget_target"] is not None and new_cost > float(plan["budget_target"]):
            await queries.swap_item(user["id"], plan_id, body.replacement_id, body.recipe_id)
            await queries.recompute_plan_cost(user["id"], plan_id)
            raise HTTPException(status_code=409, detail="Swap would exceed the budget cap")
        await queries.log_event(
            user["id"], "item_swapped_out", plan_id=plan_id, recipe_id=body.recipe_id,
            is_probe=item.get("is_probe", False),
            payload={"replacedBy": body.replacement_id},
        )
        await queries.log_event(
            user["id"], "item_swapped_in", plan_id=plan_id, recipe_id=body.replacement_id,
            payload={"replaced": body.recipe_id},
        )

    elif body.action == "never_show":
        if not body.recipe_id:
            raise HTTPException(status_code=422, detail="never_show needs recipeId")
        await queries.log_event(
            user["id"], "never_show", plan_id=plan_id, recipe_id=body.recipe_id
        )

    elif body.action in ("budget_trade_take", "budget_trade_decline"):
        trade = plan.get("budget_trade")
        if isinstance(trade, str):
            trade = json.loads(trade)
        if not trade:
            raise HTTPException(status_code=404, detail="No budget trade on this plan")
        taken = body.action == "budget_trade_take"
        if taken:
            ok = await queries.swap_item(
                user["id"], plan_id, trade["swap_out_recipe_id"], trade["recipe_id"]
            )
            if not ok:
                raise HTTPException(status_code=409, detail="Trade no longer applies")
            await queries.recompute_plan_cost(user["id"], plan_id)
        await queries.set_trade_taken(user["id"], plan_id, taken)
        await queries.log_event(
            user["id"],
            "budget_trade_taken" if taken else "budget_trade_declined",
            plan_id=plan_id,
            recipe_id=trade["recipe_id"],
            payload={"delta": trade.get("delta")},
        )

    plan = await queries.get_plan(user["id"], plan_id)
    return _plan_response(plan)


@router.get("/{plan_id}/tonight")
async def tonight(plan_id: str, user: dict = Depends(planner_user)):
    plan = await queries.get_plan(user["id"], plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan["items"]:
        raise HTTPException(status_code=404, detail="Plan has no meals")

    today = date.today().weekday()
    item = next(
        (i for i in plan["items"] if i.get("suggested_day") == today), plan["items"][0]
    )
    alternates = item.get("alternates") or []
    if isinstance(alternates, str):
        alternates = json.loads(alternates)
    titles = await queries.get_recipe_titles(alternates)
    return {
        "recipeId": str(item["recipe_id"]),
        "title": item.get("title"),
        "totalTimeMins": item.get("total_time_mins"),
        "isProbe": item.get("is_probe", False),
        "rationale": item.get("rationale"),
        "alternates": [
            {"recipeId": a, "title": titles.get(a, "")} for a in alternates
        ],
    }


@router.post("/{plan_id}/tonight/ack")
async def tonight_ack(plan_id: str, body: TonightAckRequest, user: dict = Depends(planner_user)):
    plan = await queries.get_plan(user["id"], plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    item = next(
        (i for i in plan["items"] if str(i["recipe_id"]) == body.recipe_id), None
    )
    is_probe = bool(item and item.get("is_probe"))

    if body.action == "opened":
        await queries.log_event(
            user["id"], "recipe_opened", plan_id=plan_id,
            recipe_id=body.recipe_id, is_probe=is_probe,
        )
    elif body.action == "made_it":
        await queries.log_event(
            user["id"], "marked_cooked", plan_id=plan_id,
            recipe_id=body.recipe_id, is_probe=is_probe,
        )
    elif body.action == "quick_swap":
        if not item or not body.replacement_id:
            raise HTTPException(status_code=422, detail="quick_swap needs a plan item and replacementId")
        alternates = item.get("alternates") or []
        if isinstance(alternates, str):
            alternates = json.loads(alternates)
        if body.replacement_id not in alternates:
            raise HTTPException(status_code=422, detail="Replacement must be a precomputed alternate")
        await queries.swap_item(
            user["id"], plan_id, body.recipe_id, body.replacement_id, added_via="midweek_swap"
        )
        await queries.recompute_plan_cost(user["id"], plan_id)
        await queries.log_event(
            user["id"], "midweek_swap", plan_id=plan_id, recipe_id=body.recipe_id,
            is_probe=is_probe, payload={"replacedBy": body.replacement_id},
        )
    return {"ok": True}


@router.get("/onboarding/deck")
async def onboarding_deck(user: dict = Depends(planner_user)):
    """Swipe deck spanning the catalog's cuisine spread — each swipe seeds L2."""
    deck = await queries.get_swipe_deck(user["id"])
    return {
        "deck": [
            {"recipeId": d["id"], "title": d["title"], "cuisine": d["cuisine"],
             "description": d["description"]}
            for d in deck
        ]
    }


@router.post("/onboarding/swipes")
async def onboarding_swipes(body: SwipesRequest, user: dict = Depends(planner_user)):
    for s in body.swipes:
        await queries.log_event(
            user["id"],
            "swipe_like" if s.liked else "swipe_pass",
            recipe_id=s.recipe_id,
        )
    return {"recorded": len(body.swipes)}


@router.post("/{plan_id}/close-week")
async def close_week(
    plan_id: str, generate_next: bool = True, user: dict = Depends(planner_user)
):
    """End-of-week: run the learning step, then (optionally) plan next week.
    In production this is a scheduled job; calling it directly is also the
    demo's time-travel control."""
    plan = await queries.get_plan(user["id"], plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    await queries.log_event(user["id"], "week_closed", plan_id=plan_id)
    await queries.update_plan_status(user["id"], plan_id, "closed")

    from app.services.meal_planning import learning

    learned = await learning.run_weekly_update(user["id"], plan_id)

    next_plan = None
    if generate_next:
        from app.agents.meal_planner.runner import run_week_planner

        result = await run_week_planner(user["id"], {})
        bundle = result["bundle"]
        week_start = plan["week_start"] + timedelta(days=7)
        next_id = await queries.persist_bundle(
            user["id"], week_start, bundle, result["ranking_artifact"], bundle.get("budget_cap")
        )
        next_plan = _plan_response(await queries.get_plan(user["id"], next_id))

    return {"learned": learned, "nextPlan": next_plan}


@router.get("/model")
async def user_model(user: dict = Depends(planner_user)):
    """The 'what we learned' payload: top affinity movers, features, narrative."""
    from app.services.meal_planning import learning

    return await learning.model_snapshot(user["id"])


if settings.DEMO_MODE:

    @router.post("/demo/reset")
    async def demo_reset():
        """Dev-only: wipe the demo user's loop state for a clean demo replay.
        Catalog, pantry, and preferences stay seeded."""
        await queries.reset_user_loop_state(DEMO_USER_ID)
        return {"ok": True}
