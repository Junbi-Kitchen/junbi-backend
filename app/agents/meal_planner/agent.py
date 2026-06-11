"""
Meal planner ADK pipeline (Gemini): load facts -> rank (LLM) -> assemble.

A fixed SequentialAgent — deterministic order, no model-as-router. The two
bookend agents are plain code wearing the BaseAgent interface so the whole
flow is inspectable in `make adk` web; only the middle step is an LLM, and
it does judgment only (ranking + pairing cautions + rationales). Everything
numeric (cost, budget, feasibility) is computed by the engine in code.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types as genai_types

from app.services.meal_planning import planner, queries

from .schemas import RankingOutput, ReflectionOutput

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"


def _text_event(author: str, text: str, state_delta: dict | None = None) -> Event:
    return Event(
        author=author,
        content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
        actions=EventActions(state_delta=state_delta or {}),
    )


class LoadFactsAgent(BaseAgent):
    """Deterministic step 1: DB context -> measured pool facts in state."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        user_id = ctx.session.state.get("user_id") or "dev-user-alex-rivera"
        week_context = ctx.session.state.get("week_context") or {}

        context = await queries.load_context(user_id)
        facts = planner.build_candidate_facts(context, week_context)

        yield _text_event(
            self.name,
            f"Loaded candidate pool: {len(facts)} recipes (bounded), "
            f"{len(context['pantry_ingredient_ids'])} pantry items, "
            f"{len(context['expiring'])} expiring.",
            state_delta={
                "pool_facts": planner.compact_pool_summary(
                    facts, context["expiring"], context["ingredient_names"]
                ),
                "household_context": planner.household_summary(context, week_context),
                "_context": context,
            },
        )


RANKER_INSTRUCTION = """\
You are the taste judge for a household meal planner. Code has already
filtered the recipe pool to what is SAFE and FEASIBLE for this household and
measured every number; you never compute costs or totals — cost appears only
as a band (low/med/high).

Your job is the one thing that has no formula: rank ALL recipes below by how
much THIS household will genuinely want them THIS week. Consider their
narrative, affinity signals (learned from behavior), novelty, the week's
context, and how the meals feel together as a week.

Also provide:
- pairing_cautions: pairs of recipe_ids that would feel samey/repetitive if
  both landed in the same week (similar dish format, flavor profile, or
  protein-preparation).
- rationales: for your top ~10, one short user-facing reason each
  (e.g. "Quick comfort for a busy Tuesday", "Uses the spinach before it turns").

HOUSEHOLD:
{household_context}

RECIPE POOL (facts measured by code):
{pool_facts}

Return every recipe_id from the pool exactly once in `ranking`, most-wanted first.
"""

ranker_agent = LlmAgent(
    name="meal_ranker",
    model=GEMINI_MODEL,
    description="Ranks the feasible recipe pool by household preference; flags samey pairings.",
    instruction=RANKER_INSTRUCTION,
    output_schema=RankingOutput,
    output_key="ranking_output",
)


class AssembleAgent(BaseAgent):
    """Deterministic step 3: validated ranking -> feasible bundle (engine)."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        context = ctx.session.state.get("_context")
        week_context = ctx.session.state.get("week_context") or {}
        ranking_output = ctx.session.state.get("ranking_output") or {}
        if hasattr(ranking_output, "model_dump"):
            ranking_output = ranking_output.model_dump()

        bundle, artifact = planner.assemble_bundle(
            context,
            week_context,
            ranking_output.get("ranking") or [],
            pairing_cautions=ranking_output.get("pairing_cautions"),
            rationales=ranking_output.get("rationales"),
            ranking_source="llm",
        )

        yield _text_event(
            self.name,
            f"Assembled {bundle['status']} bundle: {len(bundle['items'])} meals, "
            f"${bundle['total_cost']:.2f} of "
            f"{'$%.2f cap' % bundle['budget_cap'] if bundle['budget_cap'] else 'no cap'}.",
            state_delta={"bundle": bundle, "ranking_artifact": artifact},
        )


REFLECTOR_INSTRUCTION = """\
You are the weekly reflection step of a household meal planner. Below is the
household's current narrative and everything they actually did this week
(cooked, skipped, swapped, rated, budget trades). Read between the lines —
revealed preferences over stated ones.

Rewrite the narrative: 3-5 short, concrete observations that would help next
week's planner. Keep observations that are still true, drop stale ones, add
new ones. NEVER include numbers, budgets, or safety constraints (allergies)
in the narrative — those live elsewhere and are not yours to manage.

If the behavior suggests a measured trait shifted (repetition_tolerance,
variety_appetite, budget_strictness, quick_meal_need), you may add proposals;
code will clamp and apply them.

CURRENT NARRATIVE:
{old_narrative}

THIS WEEK'S BEHAVIOR:
{week_events}
"""

reflector_agent = LlmAgent(
    name="meal_reflector",
    model=GEMINI_MODEL,
    description="Distills a week of behavior into the household narrative + bounded feature proposals.",
    instruction=REFLECTOR_INSTRUCTION,
    output_schema=ReflectionOutput,
    output_key="reflection_output",
)

root_agent = SequentialAgent(
    name="meal_planner",
    description="Weekly meal bundle planner: code loads+measures, Gemini ranks, code assembles.",
    sub_agents=[
        LoadFactsAgent(name="load_facts"),
        ranker_agent,
        AssembleAgent(name="assemble_bundle"),
    ],
)
