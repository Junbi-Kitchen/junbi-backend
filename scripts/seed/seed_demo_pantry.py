#!/usr/bin/env python3
"""
Seed the demo user + a pantry with staggered expiry dates for the
meal-planning loop demo (expiring items make "uses the spinach" rationales
and waste-rescue math demonstrable).

Usage:
    DATABASE_URL=postgresql://... python scripts/seed/seed_demo_pantry.py

Idempotent: upserts the user/preferences, replaces the demo pantry.
Run after seed_recipe_catalog.py (pantry maps to catalog ingredient names).
"""

import os
import sys
from datetime import date, timedelta

import psycopg
from dotenv import load_dotenv

load_dotenv()

DEMO_USER_ID = "dev-user-alex-rivera"

# (ingredient name, qty, unit, location, days_until_expiry or None)
PANTRY = [
    ("spinach",          1,   "bag",    "fridge",  2),
    ("heavy cream",      1,   "pint",   "fridge",  3),
    ("chicken breast",   1.5, "lb",     "fridge",  2),
    ("cherry tomatoes",  1,   "pint",   "fridge",  4),
    ("mushrooms",        8,   "oz",     "fridge",  3),
    ("eggs",             10,  "unit",   "fridge",  14),
    ("parmesan cheese",  6,   "oz",     "fridge",  30),
    ("tortillas",        8,   "unit",   "pantry",  12),
    ("white rice",       2,   "lb",     "pantry",  None),
    ("soy sauce",        1,   "bottle", "pantry",  None),
    ("garlic",           2,   "head",   "pantry",  21),
    ("olive oil",        1,   "bottle", "pantry",  None),
    ("onion",            3,   "unit",   "pantry",  10),
    ("butter",           0.5, "lb",     "fridge",  40),
]


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(db_url)
    cur = conn.cursor()

    print("Upserting demo user...")
    cur.execute(
        """
        INSERT INTO user_profiles (id, email, display_name, onboarding_complete)
        VALUES (%s, %s, %s, true)
        ON CONFLICT (id) DO NOTHING
        """,
        (DEMO_USER_ID, "alex@example.com", "Alex Rivera"),
    )
    cur.execute("DELETE FROM user_preferences WHERE user_id = %s", (DEMO_USER_ID,))
    cur.execute(
        """
        INSERT INTO user_preferences
            (user_id, dietary_tags, allergies, cuisines, dislikes, household_size, weekly_budget)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (DEMO_USER_ID, [], ["shellfish"], ["italian", "japanese", "mexican"], [], 2, 90.00),
    )

    print("Replacing demo pantry...")
    cur.execute("DELETE FROM pantry_items WHERE user_id = %s", (DEMO_USER_ID,))
    today = date.today()
    for name, qty, unit, location, days in PANTRY:
        cur.execute("SELECT id FROM ingredients WHERE LOWER(name) = LOWER(%s)", (name,))
        row = cur.fetchone()
        if not row:
            print(f"  skip (no ingredient): {name}", file=sys.stderr)
            continue
        expiry = today + timedelta(days=days) if days is not None else None
        cur.execute(
            """
            INSERT INTO pantry_items
                (user_id, name, ingredient_id, quantity, unit, location, expiry_date, added_via)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual')
            """,
            (DEMO_USER_ID, name, row[0], qty, unit, location, expiry),
        )

    conn.commit()
    cur.execute(
        "SELECT count(*), count(expiry_date) FROM pantry_items WHERE user_id = %s",
        (DEMO_USER_ID,),
    )
    total, with_expiry = cur.fetchone()
    print(f"Done: {total} pantry items ({with_expiry} with expiry).")
    conn.close()


if __name__ == "__main__":
    main()
