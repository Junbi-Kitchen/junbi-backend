import logging

from app.db import get_async_pool

logger = logging.getLogger(__name__)

_embedding_model = None


def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        _embedding_model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return _embedding_model


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
    pool = get_async_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
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
    return [{"id": str(r["id"]), "name": r["name"], "score": float(r["score"])} for r in rows]


async def search_vector(normalized_name: str) -> list[dict]:
    """Search ingredients by vector similarity. Returns up to 5 candidates with id, name, similarity."""
    vec_str = _embed(normalized_name)
    if not vec_str:
        return []
    pool = get_async_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
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
    return [{"id": str(r["id"]), "name": r["name"], "similarity": float(r["similarity"])} for r in rows]


async def create_ingredient(normalized_name: str, raw_name: str) -> dict:
    """Create a new canonical ingredient. Only call when no existing candidate is the same food."""
    vec_str = _embed(normalized_name)
    pool = get_async_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
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
    logger.info("created new ingredient %r -> %s", normalized_name, new_id)
    return {"id": new_id, "name": normalized_name}
