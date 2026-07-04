import json
import logging
import re

from app.agents.ingredient_resolver import ingredient_resolver_graph
from app.agents.ingredient_resolver.resources import get_pool

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
    "low-fat", "baby", "ripe", "seedless", "lean",
}

_QUANTITY_RE = re.compile(
    r"^\s*[\d/\-½¼¾]+\s*"
    r"(?:cups?|tbsps?|tsps?|tablespoons?|teaspoons?|oz|lbs?|g|kg|ml|l"
    r"|cloves?|cans?|packages?|pkgs?|bunches?|heads?|stalks?|sprigs?)?"
    r"\s*",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\(.*?\)")

def _normalize(raw: str) -> str:
    text = _PAREN_RE.sub("", raw)
    text = _QUANTITY_RE.sub("", text)
    tokens = text.split()
    kept = [
        t.strip(".,;:")
        for t in tokens
        if t.lower().strip(".,;:") not in _PREP_WORDS
        and t.lower().strip(".,;:") not in _FILLER_ADJECTIVES
    ]
    return " ".join(t for t in kept if t).strip().lower()


async def _prescreen(raw_name: str, normalized_name: str) -> str | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM ingredients"
                " WHERE lower(name) = lower(%s) OR lower(name) = lower(%s) LIMIT 1",
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


async def _create_fallback(name: str) -> str:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO ingredients (name, nutrient_source) VALUES (%s, 'estimated')"
                " ON CONFLICT (name) DO NOTHING RETURNING id",
                (name,),
            )
            row = await cur.fetchone()
            if not row:
                await cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
                row = await cur.fetchone()
            return str(row["id"])


async def run_ingredient_resolver(raw_name: str) -> str:
    """Resolve a raw ingredient name to a canonical ingredient_id."""
    normalized = _normalize(raw_name)

    ingredient_id = await _prescreen(raw_name, normalized)
    if ingredient_id:
        logger.debug("pre-screen hit for %r -> %s", raw_name, ingredient_id)
        return ingredient_id

    logger.info("pre-screen miss for %r (normalized: %r) — invoking agent", raw_name, normalized)

    initial_state = {
        "messages": [{
            "role": "user",
            "content": json.dumps({"raw_name": raw_name, "normalized_name": normalized}),
        }],
    }
    final_state = await ingredient_resolver_graph.ainvoke(initial_state)
    messages = final_state["messages"]

    seen_ids: set[str] = set()
    for msg in messages:
        if msg["role"] != "user" or not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            try:
                resp = json.loads(block["content"])
            except (TypeError, ValueError):
                continue
            if isinstance(resp, list):
                for item in resp:
                    if isinstance(item, dict) and "id" in item:
                        seen_ids.add(item["id"])
            elif isinstance(resp, dict) and "id" in resp:
                seen_ids.add(resp["id"])

    final_text = ""
    for block in messages[-1]["content"]:
        if getattr(block, "type", None) == "text" and block.text:
            final_text = block.text.strip()
            break

    result = {}
    try:
        # Claude sometimes prefaces the JSON with a sentence or two of explanation
        # despite the system prompt forbidding it — pull the JSON object out of
        # wherever it lands (fenced or not) instead of assuming it's at index 0.
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", final_text, re.DOTALL)
        bare = re.search(r"(\{.*\})", final_text, re.DOTALL)
        clean = fenced.group(1) if fenced else (bare.group(1) if bare else final_text)
        result = json.loads(clean)
        resolved_id = result["ingredient_id"]
    except Exception as e:
        logger.warning("failed to parse agent response for '%s': %s — raw: %s", raw_name, e, final_text)
        resolved_id = ""

    logger.info(
        "agent resolved %r -> %s (method: %s, reason: %s)",
        raw_name, resolved_id,
        result.get("method", "unknown"),
        result.get("reason", "-"),
    )

    if resolved_id not in seen_ids:
        logger.warning("agent returned unverified id '%s' for '%s', falling back", resolved_id, raw_name)
        resolved_id = next(iter(seen_ids)) if seen_ids else await _create_fallback(normalized or raw_name)

    return resolved_id
