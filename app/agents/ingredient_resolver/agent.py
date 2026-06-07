import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar

# Ensure the project root is on sys.path so `from app.X import ...` works
# whether this module is loaded by FastAPI or by `adk web`
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from dotenv import load_dotenv
load_dotenv()

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from app.db import get_async_pool, open_pool

logger = logging.getLogger(__name__)

_PREP_WORDS = {
    "chopped", "diced", "minced", "sliced", "grated", "shredded", "frozen",
    "thawed", "rinsed", "drained", "peeled", "crushed", "ground", "toasted",
    "roasted", "cooked", "uncooked", "raw", "finely", "roughly", "thinly",
    "freshly", "lightly",
}

_FILLER_ADJECTIVES = {
    "organic", "fresh", "large", "small", "medium", "whole", "boneless",
    "skinless", "unsalted", "salted", "sweetened", "unsweetened", "fat-free",
    "low-fat", "extra", "baby", "ripe", "seedless", "lean",
}

_QUANTITY_RE = re.compile(
    r"^\s*[\d/\-½¼¾]+\s*"
    r"(?:cups?|tbsps?|tsps?|tablespoons?|teaspoons?|oz|lbs?|g|kg|ml|l|cloves?|cans?|packages?|pkgs?|bunches?|heads?|stalks?|sprigs?)?"
    r"\s*",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\(.*?\)")

_AGENT_INSTRUCTION = """\
You are resolving an ingredient name to a canonical entry in a shared food database.

You will receive:
- raw_name: the original text (e.g. "2 cups finely chopped organic yellow onion")
- normalized_name: already cleaned (e.g. "yellow onion")

The exact match pre-screen has already run and found nothing. Use your tools.

PROCESS:
1. Call search_fulltext with normalized_name to find candidates.
2. Call search_vector with normalized_name to find more candidates.
3. Evaluate all candidates across both results.
4. STRONGLY prefer matching an existing ingredient over creating a new one.
   - "spring onion" → "green onion" is a match
   - "brown sugar" → "sugar" is a match when no brown sugar entry exists
   - Case differences alone are NOT a reason to create a new ingredient
   - Only create a new ingredient if nothing in the candidates is the same food
5. If you pick an existing ingredient: respond with JSON only:
   {"ingredient_id": "<uuid>", "method": "matched", "reason": "brief explanation of why this candidate matches"}
6. If nothing matches and the ingredient is genuinely new: call create_ingredient,
   then respond with JSON only:
   {"ingredient_id": "<uuid>", "method": "created", "reason": "brief explanation of why no existing candidate matched"}

Your entire response must be valid JSON and nothing else.\
"""

_embedding_model = None

# Per-call candidates dict — tools read/write this; hallucination guard reads it after the run
_candidates_var: ContextVar[dict[str, str]] = ContextVar("candidates")


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return _embedding_model


def _normalize(raw: str) -> str:
    text = _PAREN_RE.sub("", raw)
    text = _QUANTITY_RE.sub("", text)
    tokens = text.split()
    kept = [t.strip(".,;:") for t in tokens if t.lower().strip(".,;:") not in _PREP_WORDS and t.lower().strip(".,;:") not in _FILLER_ADJECTIVES]
    return " ".join(t for t in kept if t).strip().lower()


async def _prescreen(raw_name: str, normalized_name: str) -> str | None:
    await _ensure_pool()
    pool = get_async_pool()
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            "SELECT id FROM ingredients WHERE lower(name) = lower(%s) OR lower(name) = lower(%s) LIMIT 1",
            (raw_name, normalized_name),
        )
        row = await cur.fetchone()
        if row:
            return str(row["id"])
        await cur.execute(
            "SELECT ingredient_id AS id FROM ingredient_aliases"
            " WHERE lower(alias) = lower(%s) OR lower(alias) = lower(%s) LIMIT 1",
            (raw_name, normalized_name),
        )
        row = await cur.fetchone()
        if row:
            return str(row["id"])
    return None


async def _ensure_pool() -> None:
    if get_async_pool() is None:
        await open_pool()


def _embed(text: str) -> str | None:
    try:
        model = _get_embedding_model()
        vec = list(next(model.embed([text])))
        return "[" + ",".join(str(x) for x in vec) + "]"
    except Exception as e:
        logger.warning("fastembed failed for '%s': %s", text, e)
        return None


async def search_fulltext(normalized_name: str) -> list[dict]:
    """Search ingredients by full-text. Returns up to 5 candidates with id, name, score."""
    await _ensure_pool()
    pool = get_async_pool()
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            """
            SELECT i.id, i.name,
                   ts_rank(to_tsvector('english', i.name), plainto_tsquery('english', %s)) AS score
            FROM ingredients i
            WHERE to_tsvector('english', i.name) @@ plainto_tsquery('english', %s)
            ORDER BY score DESC LIMIT 5
            """,
            (normalized_name, normalized_name),
        )
        rows = await cur.fetchall()
    results = [{"id": str(r["id"]), "name": r["name"], "score": float(r["score"])} for r in rows]
    try:
        _candidates_var.get().update({r["id"]: r["name"] for r in results})
    except LookupError:
        pass
    return results


async def search_vector(normalized_name: str) -> list[dict]:
    """Search ingredients by vector similarity. Returns up to 5 candidates with id, name, similarity."""
    await _ensure_pool()
    vec_str = _embed(normalized_name)
    if not vec_str:
        return []
    pool = get_async_pool()
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            """
            SELECT i.id, i.name,
                   1 - (i.embedding <=> %s::vector) AS similarity
            FROM ingredients i
            WHERE i.embedding IS NOT NULL
            ORDER BY i.embedding <=> %s::vector LIMIT 5
            """,
            (vec_str, vec_str),
        )
        rows = await cur.fetchall()
    results = [{"id": str(r["id"]), "name": r["name"], "similarity": float(r["similarity"])} for r in rows]
    try:
        _candidates_var.get().update({r["id"]: r["name"] for r in results})
    except LookupError:
        pass
    return results


async def create_ingredient(normalized_name: str, raw_name: str) -> dict:
    """Create a new canonical ingredient. Only call when no existing candidate is the same food."""
    await _ensure_pool()
    vec_str = _embed(normalized_name)
    pool = get_async_pool()
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            "INSERT INTO ingredients (name, nutrient_source) VALUES (%s, 'estimated')"
            " ON CONFLICT (name) DO NOTHING RETURNING id",
            (normalized_name,),
        )
        row = await cur.fetchone()
        if not row:
            await cur.execute("SELECT id FROM ingredients WHERE name = %s", (normalized_name,))
            row = await cur.fetchone()
        new_id = str(row["id"])
        if vec_str:
            await cur.execute(
                "UPDATE ingredients SET embedding = %s::vector WHERE id = %s",
                (vec_str, new_id),
            )
        if raw_name.lower() != normalized_name.lower():
            await cur.execute(
                "INSERT INTO ingredient_aliases (ingredient_id, alias) VALUES (%s, %s)"
                " ON CONFLICT (alias) DO NOTHING",
                (new_id, raw_name),
            )
    try:
        _candidates_var.get()[new_id] = normalized_name
    except LookupError:
        pass
    logger.info("created new ingredient %r -> %s", normalized_name, new_id)
    return {"id": new_id, "name": normalized_name}


# Single shared agent + runner — created once at import time
root_agent = Agent(
    name="ingredient_resolver",
    model="gemini-2.5-flash",
    instruction=_AGENT_INSTRUCTION,
    tools=[search_fulltext, search_vector, create_ingredient],
)

_session_service = InMemorySessionService()
_runner = Runner(
    agent=root_agent,
    app_name="ingredient_resolver",
    session_service=_session_service,
    auto_create_session=True,
)


async def run_ingredient_resolver(raw_name: str) -> str:
    """Resolve a raw ingredient name string to a canonical ingredient_id (UUID str)."""
    normalized = _normalize(raw_name)

    ingredient_id = await _prescreen(raw_name, normalized)
    if ingredient_id:
        logger.debug("pre-screen hit for %r -> %s", raw_name, ingredient_id)
        return ingredient_id

    logger.info("pre-screen miss for %r (normalized: %r) — invoking agent", raw_name, normalized)

    token = _candidates_var.set({})
    session_id = str(uuid.uuid4())

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=json.dumps({"raw_name": raw_name, "normalized_name": normalized}))],
    )

    final_text = ""
    async for event in _runner.run_async(
        user_id="system",
        session_id=session_id,
        new_message=message,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    logger.debug("tool call: %s(%s)", part.function_call.name, part.function_call.args)
                elif hasattr(part, "function_response") and part.function_response:
                    logger.debug("tool response: %s → %s", part.function_response.name, part.function_response.response)
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text = part.text.strip()
                    break

    candidates = _candidates_var.get()
    _candidates_var.reset(token)

    result = {}
    try:
        clean = re.sub(r"^```(?:json)?\n?", "", final_text)
        clean = re.sub(r"\n?```$", "", clean).strip()
        result = json.loads(clean)
        resolved_id = result["ingredient_id"]
    except Exception as e:
        logger.warning("Failed to parse agent response for '%s': %s — raw: %s", raw_name, e, final_text)
        resolved_id = ""

    logger.info(
        "agent resolved %r -> %s (method: %s, reason: %s)",
        raw_name, resolved_id,
        result.get("method", "unknown"),
        result.get("reason", "-"),
    )

    if resolved_id not in candidates:
        logger.warning("Agent returned unverified id '%s' for '%s', falling back", resolved_id, raw_name)
        if candidates:
            resolved_id = next(iter(candidates))
        else:
            pool = get_async_pool()
            async with pool.connection() as conn:
                cur = conn.cursor()
                name = normalized or raw_name
                await cur.execute(
                    "INSERT INTO ingredients (name, nutrient_source) VALUES (%s, 'estimated')"
                    " ON CONFLICT (name) DO NOTHING RETURNING id",
                    (name,),
                )
                row = await cur.fetchone()
                if not row:
                    await cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
                    row = await cur.fetchone()
                resolved_id = str(row["id"])

    return resolved_id
