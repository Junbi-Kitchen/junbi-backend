"""
The weekly learning step (async, never in the planning hot path).

Design rules from docs/meal-planning-final-strategy.md §6-7:
- plan_events is the source of truth; learned state is a *full replay* of it,
  so re-tuning weights later rewrites history consistently and double-counting
  is impossible.
- Code owns every number. The LLM reflection (M3) only proposes feature
  deltas, which are clamped here and audited in model_update_log; it never
  writes Layer 1 (constraints) at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.meal_planning import queries

logger = logging.getLogger(__name__)

# Per-event affinity weights (strategy §6 table; starting points, tunable —
# replay makes retuning retroactive).
SIGNAL_WEIGHTS: dict[str, float] = {
    "swipe_like": 1.0,
    "swipe_pass": -0.5,
    "plan_accepted": 0.0,       # per-recipe credit handled via payload below
    "item_swapped_out": -2.0,
    "item_swapped_in": 2.0,
    "never_show": -5.0,
    "budget_trade_taken": 1.0,
    "budget_trade_declined": 0.0,
    "recipe_opened": 0.5,
    "marked_cooked": 2.0,
    "rated_up": 3.0,
    "rated_down": -3.0,
    "midweek_swap": -1.0,       # the meal that got swapped away tonight
    "present_unopened": -0.5,   # synthesized at week close
}
ACCEPTED_PER_RECIPE = 1.0
MIDWEEK_SWAP_IN = 1.0

# Probe outcomes are the high-information events the explore slot buys.
PROBE_POSITIVE_MULT = 1.5
PROBE_NEGATIVE_MULT = 0.5

STEP = 0.4               # learning rate per signal
SCORE_CLAMP = 5.0
CONFIDENCE_STEP = 0.15
REFLECTION_DELTA_CAP = 0.1
NARRATIVE_MAX_CHARS = 800

FEATURE_DEFAULTS = {
    "budget_strictness": 0.5,   # 0 = budget is elastic, 1 = cap is sacred
    "quick_meal_need": 0.5,
    "variety_appetite": 0.5,
    "repetition_tolerance": 0.5,
}
FEATURE_STEPS = {
    "budget_trade_taken": [("budget_strictness", -0.05)],
    "budget_trade_declined": [("budget_strictness", +0.05)],
    "midweek_swap": [("quick_meal_need", +0.05)],
    "never_show": [("variety_appetite", +0.02)],
}


@dataclass
class AffinityState:
    score: float = 0.0
    confidence: float = 0.0
    last_signal_at: datetime | None = None


@dataclass
class ReplayResult:
    affinities: dict[str, AffinityState] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)


def replay_events(events: list[dict]) -> ReplayResult:
    """Pure: fold the full event log into affinity scores + feature values."""
    result = ReplayResult(features=dict(FEATURE_DEFAULTS))

    def bump(recipe_id: str | None, weight: float, is_probe: bool, at: datetime) -> None:
        if not recipe_id or weight == 0.0:
            return
        if is_probe:
            weight *= PROBE_POSITIVE_MULT if weight > 0 else PROBE_NEGATIVE_MULT
        a = result.affinities.setdefault(recipe_id, AffinityState())
        a.score = max(-SCORE_CLAMP, min(SCORE_CLAMP, a.score + weight * STEP))
        a.confidence = min(1.0, a.confidence + CONFIDENCE_STEP)
        a.last_signal_at = at

    for e in events:
        etype = e["event_type"]
        payload = e.get("payload") or {}
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        at = e.get("created_at") or datetime.now(timezone.utc)

        bump(e.get("recipe_id"), SIGNAL_WEIGHTS.get(etype, 0.0), bool(e.get("is_probe")), at)

        if etype == "plan_accepted":
            for rid in payload.get("recipeIds", []):
                bump(rid, ACCEPTED_PER_RECIPE, False, at)
        elif etype == "midweek_swap" and payload.get("replacedBy"):
            bump(payload["replacedBy"], MIDWEEK_SWAP_IN, False, at)

        for feature_key, step in FEATURE_STEPS.get(etype, []):
            result.features[feature_key] = max(
                0.0, min(1.0, result.features[feature_key] + step)
            )

    return result


def summarize_week_events(events: list[dict], titles: dict[str, str]) -> str:
    """Human-readable behavior log for the reflection prompt."""
    lines = []
    for e in events:
        title = titles.get(e.get("recipe_id") or "", e.get("recipe_id") or "")
        probe = " [was the 'something new' probe]" if e.get("is_probe") else ""
        lines.append(f"- {e['event_type']}: {title}{probe}")
    return "\n".join(lines) or "- (no recorded behavior this week)"


def _validate_narrative(narrative: str) -> str | None:
    """L3 discipline: short, no money figures (numbers live in L2/code)."""
    import re

    if not narrative or not narrative.strip():
        return None
    if re.search(r"\$\s?\d", narrative):
        return None
    return narrative.strip()[:NARRATIVE_MAX_CHARS]


async def _synthesize_unopened_events(user_id: str, plan_id: str, events: list[dict]) -> None:
    """Meals that were in the closed plan but never even opened are weak
    negative evidence — logged as events so replay stays the single truth."""
    plan = await queries.get_plan(user_id, plan_id)
    if not plan:
        return
    touched = {
        e["recipe_id"]
        for e in events
        if e.get("plan_id") == plan_id
        and e["event_type"] in ("recipe_opened", "marked_cooked", "item_swapped_out")
    }
    already = {
        e["recipe_id"]
        for e in events
        if e.get("plan_id") == plan_id and e["event_type"] == "present_unopened"
    }
    for item in plan["items"]:
        rid = str(item["recipe_id"])
        if rid not in touched and rid not in already:
            await queries.log_event(
                user_id, "present_unopened", plan_id=plan_id, recipe_id=rid,
                is_probe=bool(item.get("is_probe")),
            )


async def run_weekly_update(user_id: str, plan_id: str) -> dict:
    """Close-of-week learning: stats replay (code) + reflection (one LLM call)."""
    events = await queries.fetch_all_events(user_id)
    await _synthesize_unopened_events(user_id, plan_id, events)
    events = await queries.fetch_all_events(user_id)

    # "movement this week" = effect of this plan's events on top of everything prior
    prior_events = [e for e in events if e.get("plan_id") != plan_id]
    before = {rid: a.score for rid, a in replay_events(prior_events).affinities.items()}

    result = replay_events(events)
    await queries.replace_affinities(
        user_id,
        {
            rid: {
                "score": round(a.score, 3),
                "confidence": round(a.confidence, 3),
                "last_signal_at": a.last_signal_at,
            }
            for rid, a in result.affinities.items()
        },
    )
    for key, value in result.features.items():
        await queries.upsert_feature(
            user_id, key, round(value, 3), 0.5,
            source="stats", delta=round(value - FEATURE_DEFAULTS[key], 3),
            evidence="weekly replay",
        )

    # ---- reflection (M3): one LLM call; narrative + clamped proposals ----
    reflection_summary: dict = {"narrative_updated": False, "proposals_applied": 0}
    week_events = [e for e in events if e.get("plan_id") == plan_id]
    titles = await queries.get_recipe_titles(
        [e["recipe_id"] for e in week_events if e.get("recipe_id")]
    )
    try:
        from app.agents.meal_planner.runner import run_reflection

        snapshot = await queries.affinity_snapshot(user_id)
        old_narrative = (snapshot.get("narrative") or {}).get("narrative", "") or "(none yet)"
        reflection = await run_reflection(
            user_id, old_narrative, summarize_week_events(week_events, titles)
        )
        if reflection:
            narrative = _validate_narrative(reflection.get("narrative", ""))
            if narrative:
                version = await queries.insert_narrative(user_id, narrative)
                reflection_summary["narrative_updated"] = True
                reflection_summary["narrative_version"] = version
            for prop in reflection.get("proposals", []):
                key = prop.get("feature_key")
                if key not in FEATURE_DEFAULTS:
                    continue
                delta = max(-REFLECTION_DELTA_CAP, min(REFLECTION_DELTA_CAP, float(prop.get("delta", 0))))
                if delta == 0:
                    continue
                current = result.features.get(key, FEATURE_DEFAULTS[key])
                await queries.upsert_feature(
                    user_id, key, round(max(0.0, min(1.0, current + delta)), 3), 0.5,
                    source="reflection", delta=delta, evidence=prop.get("evidence"),
                )
                reflection_summary["proposals_applied"] += 1
    except Exception:
        logger.exception("reflection failed — stats-only update applied")

    movers = sorted(
        (
            (rid, round(a.score - before.get(rid, 0.0), 3), round(a.score, 3))
            for rid, a in result.affinities.items()
        ),
        key=lambda x: -abs(x[1]),
    )[:8]
    mover_titles = await queries.get_recipe_titles([m[0] for m in movers])
    return {
        "eventsReplayed": len(events),
        "recipesScored": len(result.affinities),
        "topMovers": [
            {"recipeId": rid, "title": mover_titles.get(rid, rid), "delta": d, "score": s}
            for rid, d, s in movers
        ],
        "features": {k: round(v, 3) for k, v in result.features.items()},
        "reflection": reflection_summary,
    }


async def model_snapshot(user_id: str) -> dict:
    """The 'what we learned' panel payload."""
    snap = await queries.affinity_snapshot(user_id)
    return {
        "topRecipes": [
            {"recipeId": r["recipe_id"], "title": r["title"],
             "score": float(r["score"]), "confidence": float(r["confidence"])}
            for r in snap["top"]
        ],
        "bottomRecipes": [
            {"recipeId": r["recipe_id"], "title": r["title"],
             "score": float(r["score"]), "confidence": float(r["confidence"])}
            for r in snap["bottom"]
        ],
        "features": [
            {"key": f["feature_key"], "value": float(f["value"]),
             "confidence": float(f["confidence"])}
            for f in snap["features"]
        ],
        "narrative": snap["narrative"]["narrative"] if snap["narrative"] else None,
        "narrativeVersion": snap["narrative"]["version"] if snap["narrative"] else None,
    }
