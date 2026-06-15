"""
Service-facing entry points for the meal_planner ADK pipeline.

`run_week_planner` plans a week through the SequentialAgent (Gemini ranks).
If GOOGLE_API_KEY is unset or the pipeline fails, it degrades to the
deterministic path — same engine, affinity-sorted ranking — so planning
works headless (tests, demos, API outages).
"""

from __future__ import annotations

import logging
import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

import os

from app.config import settings
from app.services.meal_planning import engine, planner, queries

from .agent import reflector_agent, root_agent

logger = logging.getLogger(__name__)

APP_NAME = "meal_planner"


def _gemini_available() -> bool:
    """Gemini is reachable via an API key or via Vertex AI with gcloud ADC."""
    return bool(settings.GOOGLE_API_KEY) or os.environ.get(
        "GOOGLE_GENAI_USE_VERTEXAI", ""
    ).lower() in ("1", "true", "yes")

_session_service = InMemorySessionService()
_runner = Runner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=_session_service,
    auto_create_session=True,
)
_reflect_runner = Runner(
    agent=reflector_agent,
    app_name="meal_reflector",
    session_service=_session_service,
    auto_create_session=True,
)


async def _plan_deterministic(user_id: str, week_context: dict | None) -> dict:
    context = await queries.load_context(user_id)
    facts = planner.build_candidate_facts(context, week_context)
    ranking = engine.deterministic_ranking(facts)
    bundle, artifact = planner.assemble_bundle(
        context, week_context, ranking, ranking_source="deterministic"
    )
    return {"bundle": bundle, "ranking_artifact": artifact}


async def run_week_planner(user_id: str, week_context: dict | None = None) -> dict:
    """Plan the week. Returns {"bundle": ..., "ranking_artifact": ...}."""
    if not _gemini_available():
        logger.warning("no Gemini credentials — planning deterministically")
        return await _plan_deterministic(user_id, week_context)

    session_id = str(uuid.uuid4())
    await _session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state={"user_id": user_id, "week_context": week_context or {}},
    )
    message = genai_types.Content(
        role="user", parts=[genai_types.Part(text="Plan this week's meal bundle.")]
    )

    try:
        async for _event in _runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            pass
        session = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        bundle = session.state.get("bundle")
        artifact = session.state.get("ranking_artifact")
        if not bundle:
            raise RuntimeError("pipeline produced no bundle")
        return {"bundle": bundle, "ranking_artifact": artifact}
    except Exception:
        logger.exception("meal_planner pipeline failed — falling back to deterministic")
        return await _plan_deterministic(user_id, week_context)


async def run_reflection(
    user_id: str, old_narrative: str, week_events: str
) -> dict | None:
    """One reflection call per closed week. Returns the ReflectionOutput dict,
    or None headless (caller keeps the previous narrative — stats still ran)."""
    if not _gemini_available():
        logger.warning("no Gemini credentials — skipping reflection (stats-only update)")
        return None

    session_id = str(uuid.uuid4())
    await _session_service.create_session(
        app_name="meal_reflector",
        user_id=user_id,
        session_id=session_id,
        state={"old_narrative": old_narrative, "week_events": week_events},
    )
    message = genai_types.Content(
        role="user", parts=[genai_types.Part(text="Reflect on this week's behavior.")]
    )
    async for _event in _reflect_runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        pass
    session = await _session_service.get_session(
        app_name="meal_reflector", user_id=user_id, session_id=session_id
    )
    output = session.state.get("reflection_output")
    if hasattr(output, "model_dump"):
        output = output.model_dump()
    return output
