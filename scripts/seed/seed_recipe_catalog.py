#!/usr/bin/env python3
"""
Seed the synthetic recipe catalog for the meal-planning loop.

Reads scripts/seed/data/recipe_catalog.json and inserts global recipes
(with tags, steps, ingredients, and MiniLM embeddings) plus base ingredient
prices used by the plan cost math.

Usage:
    DATABASE_URL=postgresql://... python scripts/seed/seed_recipe_catalog.py

Idempotent: deletes previously seeded catalog recipes (source_type='catalog')
first, then re-inserts. Ingredient prices are only filled where NULL.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

CATALOG_PATH = Path(__file__).parent / "data" / "recipe_catalog.json"
SOURCE_TYPE = "catalog"


def embed_batch(texts: list[str]) -> list[str]:
    """Embed texts with the same MiniLM model used for ingredients."""
    from fastembed import TextEmbedding

    model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return ["[" + ",".join(str(x) for x in vec) + "]" for vec in model.embed(texts)]


def resolve_ingredient(cur, name: str, cache: dict) -> str:
    """Return ingredient UUID for name: exact -> case-insensitive -> alias -> create stub."""
    if name in cache:
        return cache[name]

    cur.execute("SELECT id FROM ingredients WHERE LOWER(name) = LOWER(%s)", (name,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "SELECT ingredient_id FROM ingredient_aliases WHERE LOWER(alias) = LOWER(%s)",
            (name,),
        )
        row = cur.fetchone()
    if row:
        cache[name] = str(row[0])
        return cache[name]

    new_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO ingredients (id, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING RETURNING id",
        (new_id, name),
    )
    result = cur.fetchone()
    if not result:
        cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
        result = cur.fetchone()
    cache[name] = str(result[0])
    return cache[name]


def recipe_embedding_text(r: dict) -> str:
    ingredients = ", ".join(i[0] for i in r["ingredients"])
    tags = " ".join(r.get("tags", []))
    return f"{r['title']}. {r['cuisine']} {r['protein']} {tags}. Ingredients: {ingredients}."


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    catalog = json.loads(CATALOG_PATH.read_text())
    prices: dict = catalog["prices"]
    recipes: list = catalog["recipes"]

    print(f"Embedding {len(recipes)} recipes (MiniLM)...")
    embeddings = embed_batch([recipe_embedding_text(r) for r in recipes])

    conn = psycopg.connect(db_url)
    cur = conn.cursor()

    print("Removing previous catalog recipes...")
    cur.execute("DELETE FROM recipes WHERE source_type = %s", (SOURCE_TYPE,))

    ingredient_cache: dict[str, str] = {}

    print(f"Inserting {len(recipes)} recipes...")
    for r, emb in zip(recipes, embeddings):
        recipe_id = str(uuid.uuid4())
        tags = set(r.get("tags", []))
        tags.add(f"protein:{r['protein']}")
        if r["prep"] + r["cook"] <= 30:
            tags.add("quick")

        cur.execute(
            """
            INSERT INTO recipes (
                id, title, description, prep_time_mins, cook_time_mins, servings,
                difficulty, cuisine, source_type, is_global, embedding
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
            """,
            (
                recipe_id, r["title"], r["desc"], r["prep"], r["cook"], r["servings"],
                r["difficulty"], r["cuisine"], SOURCE_TYPE, True, emb,
            ),
        )

        cur.executemany(
            "INSERT INTO recipe_tags (recipe_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(recipe_id, t) for t in sorted(tags)],
        )

        cur.executemany(
            "INSERT INTO recipe_steps (recipe_id, step_number, instruction) VALUES (%s, %s, %s)",
            [(recipe_id, n + 1, step) for n, step in enumerate(r["steps"])],
        )

        for idx, (name, qty, unit) in enumerate(r["ingredients"]):
            ing_id = resolve_ingredient(cur, name, ingredient_cache)
            cur.execute(
                """
                INSERT INTO recipe_ingredients
                    (recipe_id, ingredient_id, quantity, unit, order_index, display_text)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (recipe_id, ing_id, qty, unit, idx, name),
            )

    print(f"Setting base prices for {len(prices)} ingredients (where NULL)...")
    for name, (price, unit) in prices.items():
        ing_id = resolve_ingredient(cur, name, ingredient_cache)
        cur.execute(
            """
            UPDATE ingredients SET estimated_price = %s, price_unit = %s
            WHERE id = %s AND estimated_price IS NULL
            """,
            (price, unit, ing_id),
        )

    conn.commit()

    cur.execute("SELECT count(*) FROM recipes WHERE source_type = %s", (SOURCE_TYPE,))
    n_recipes = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ingredients WHERE estimated_price IS NOT NULL")
    n_priced = cur.fetchone()[0]
    print(f"Done: {n_recipes} catalog recipes, {n_priced} priced ingredients.")
    conn.close()


if __name__ == "__main__":
    main()
