#!/usr/bin/env python3
"""
Seed USDA SR Legacy food data into ingredient_categories and ingredients tables.

Usage:
    DATABASE_URL=postgresql://user:pass@localhost/gook python scripts/seed/seed_usda.py

The script is idempotent: safe to run multiple times.
"""

import csv
import os
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

CSV_DIR = Path(__file__).parent / "sr_legacy_food_csv"

SKIP_CATEGORIES = {
    3,   # Baby Foods
    21,  # Fast Foods
    22,  # Meals, Entrees, and Side Dishes
    25,  # Restaurant Foods
    26,  # Branded Food Products Database
    27,  # Quality Control Materials
}


def slugify(text: str) -> str:
    text = text.lower()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def load_csv(filename: str) -> list[dict]:
    with open(CSV_DIR / filename, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def seed(conn) -> None:
    cur = conn.cursor()

    # ------------------------------------------------------------------ #
    # 1. ingredient_categories  ← food_category.csv
    # ------------------------------------------------------------------ #
    print("Seeding ingredient_categories...")
    categories = load_csv("food_category.csv")
    cat_rows = [
        (
            int(row["id"]),
            slugify(row["description"]),
            row["description"],
            row["code"],
            row["description"],  # usda_name same as name (CSV has one description field)
        )
        for row in categories
        if int(row["id"]) not in SKIP_CATEGORIES
    ]
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO ingredient_categories (id, slug, name, usda_code, usda_name)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
        """,
        cat_rows,
    )
    # Advance the SERIAL sequence past the manually inserted IDs
    cur.execute("SELECT setval('ingredient_categories_id_seq', (SELECT MAX(id) FROM ingredient_categories))")
    print(f"  {cur.rowcount if cur.rowcount >= 0 else len(cat_rows)} categories inserted (conflicts skipped)")

    # ------------------------------------------------------------------ #
    # 2. measure_unit lookup dict  ← measure_unit.csv
    # ------------------------------------------------------------------ #
    measure_units: dict[str, str | None] = {}
    for row in load_csv("measure_unit.csv"):
        uid = row["id"].strip()
        name = row["name"].strip() or None
        measure_units[uid] = name
    # 9999 = "undetermined" → treat as NULL
    measure_units["9999"] = None

    # ------------------------------------------------------------------ #
    # 3. Default portion per food  ← food_portion.csv (seq_num = 1)
    # ------------------------------------------------------------------ #
    print("Building portion lookup (seq_num=1)...")
    portions: dict[str, tuple[float | None, str | None]] = {}
    for row in load_csv("food_portion.csv"):
        if row["seq_num"].strip() != "1":
            continue
        fdc_id = row["fdc_id"].strip()
        gram_weight_raw = row["gram_weight"].strip()
        gram_weight = float(gram_weight_raw) if gram_weight_raw else None
        unit_name = measure_units.get(row["measure_unit_id"].strip())
        portions[fdc_id] = (gram_weight, unit_name)

    # ------------------------------------------------------------------ #
    # 4. ingredients  ← food.csv
    # ------------------------------------------------------------------ #
    print("Seeding ingredients...")
    foods = load_csv("food.csv")
    ingredient_rows = []
    for row in foods:
        food_category_id = row["food_category_id"].strip()
        category_id = int(food_category_id) if food_category_id else None
        if category_id in SKIP_CATEGORIES:
            continue
        fdc_id = row["fdc_id"].strip()
        gram_weight, default_unit = portions.get(fdc_id, (None, None))
        ingredient_rows.append((
            row["description"],
            fdc_id,
            category_id,
            gram_weight,
            default_unit,
            "usda",
        ))

    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO ingredients (name, usda_fdc_id, category_id, gram_weight, default_unit, nutrient_source)
        VALUES %s
        ON CONFLICT (name) DO NOTHING
        """,
        ingredient_rows,
        page_size=500,
    )
    print(f"  {len(ingredient_rows)} ingredients processed (conflicts skipped for existing names)")

    conn.commit()
    cur.close()
    print("Done.")


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Error: DATABASE_URL environment variable is not set.", file=sys.stderr)
        print("Usage: DATABASE_URL=postgresql://user:pass@localhost/gook python scripts/seed/seed_usda.py", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to database...")
    conn = psycopg2.connect(database_url)
    try:
        seed(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
