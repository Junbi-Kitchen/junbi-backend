#!/usr/bin/env python3
"""
Seed mock development data from gook-frontend/lib/mockData.ts.

Inserts one demo user (Alex Rivera) with recipes, pantry, grocery list,
collections, stores, an order, and waste log entries.

Usage:
    DATABASE_URL=postgresql://... python scripts/seed/seed_mock.py

Idempotent: deletes existing demo user data first, then re-inserts.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ------------------------------------------------------------------ #
# Demo user — use a fixed UUID so we can re-run safely
# ------------------------------------------------------------------ #
DEMO_USER_ID = "dev-user-alex-rivera"

# ------------------------------------------------------------------ #
# Category → storage location mapping
# ------------------------------------------------------------------ #
CATEGORY_TO_LOCATION = {
    "proteins":   "fridge",
    "dairy":      "fridge",
    "produce":    "fridge",
    "pantry":     "pantry",
    "spices":     "pantry",
    "condiments": "pantry",
    "grains":     "pantry",
    "frozen":     "freezer",
    "bakery":     "pantry",
    "beverages":  "fridge",
}

# ------------------------------------------------------------------ #
# Recipe source platform mapping
# ------------------------------------------------------------------ #
PLATFORM_MAP = {
    "instagram": "instagram",
    "tiktok":    "tiktok",
    "youtube":   "youtube",
    "url":       "other",
    "manual":    None,
}

# ------------------------------------------------------------------ #
# Mock data (translated from mockData.ts)
# ------------------------------------------------------------------ #

# Stable IDs so re-runs are clean
RECIPE_IDS = {f"r{i}": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gook-recipe-r{i}")) for i in range(1, 11)}
COLLECTION_IDS = {f"col-{i}": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gook-collection-col-{i}")) for i in range(1, 5)}
STORE_IDS = {f"s{i}": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gook-store-s{i}")) for i in range(1, 7)}
PANTRY_IDS = {f"p{i}": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gook-pantry-p{i}")) for i in range(1, 21)}
GROCERY_LIST_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "gook-grocery-list-1"))
GROCERY_IDS = {f"g{i}": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"gook-grocery-g{i}")) for i in range(1, 13)}
ORDER_ID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "gook-order-o1"))
ADDRESS_IDS = {
    "addr-1": str(uuid.uuid5(uuid.NAMESPACE_DNS, "gook-address-addr-1")),
    "addr-2": str(uuid.uuid5(uuid.NAMESPACE_DNS, "gook-address-addr-2")),
}

now = datetime.now(timezone.utc)
tomorrow     = (now + timedelta(days=1)).isoformat()
in_two_days  = (now + timedelta(days=2)).isoformat()
in_five_days = (now + timedelta(days=5)).isoformat()
in_ten_days  = (now + timedelta(days=10)).isoformat()

MOCK_RECIPES = [
    {
        "id": "r1", "title": "Cacio e Pepe",
        "description": "The Roman classic — silky pasta with aged pecorino and black pepper.",
        "blurhash": "LKF~Ws%2IUM{~qj[WBj[00WB-;of",
        "cook_time_mins": 20, "difficulty": "medium",
        "imported_from": "instagram", "source": "@pasta.everyday",
        "tags": ["vegetarian"],
        "calories": 520, "protein_g": 18, "carbs_g": 72, "fat_g": 18,
        "ingredients": [
            {"name": "Spaghetti",       "quantity": 400, "unit": "g"},
            {"name": "Pecorino Romano", "quantity": 100, "unit": "g"},
            {"name": "Black Pepper",    "quantity": 2,   "unit": "tsp"},
            {"name": "Pasta Water",     "quantity": 200, "unit": "ml"},
        ],
        "steps": [
            {"n": 1, "instruction": "Cook spaghetti in generously salted water until al dente.", "timer": 10},
            {"n": 2, "instruction": "Toast black pepper in a dry pan over medium heat until fragrant."},
            {"n": 3, "instruction": "Reserve 1 cup pasta water. Drain pasta and add to pan with pepper."},
            {"n": 4, "instruction": "Off heat, add grated pecorino and toss vigorously with pasta water to form a creamy sauce.", "timer": 3},
        ],
    },
    {
        "id": "r2", "title": "Lasagna Bolognese",
        "description": "Layers of fresh pasta, slow-cooked meat ragù, and béchamel.",
        "blurhash": "LDEyE:~q00xu00Rj%Mt700WB-;of",
        "cook_time_mins": 90, "difficulty": "hard",
        "imported_from": "url", "source": "seriouseats.com",
        "tags": ["high-protein"],
        "calories": 680, "protein_g": 34, "carbs_g": 58, "fat_g": 32,
        "ingredients": [
            {"name": "Ground Beef",       "quantity": 500, "unit": "g"},
            {"name": "Lasagna Sheets",    "quantity": 250, "unit": "g"},
            {"name": "Whole Milk",        "quantity": 500, "unit": "ml"},
            {"name": "Crushed Tomatoes",  "quantity": 400, "unit": "g"},
            {"name": "Mozzarella",        "quantity": 200, "unit": "g"},
            {"name": "Onion",             "quantity": 1,   "unit": "large"},
            {"name": "Butter",            "quantity": 60,  "unit": "g"},
            {"name": "All-Purpose Flour", "quantity": 60,  "unit": "g"},
        ],
        "steps": [
            {"n": 1, "instruction": "Brown ground beef with diced onion and garlic over high heat.", "timer": 8},
            {"n": 2, "instruction": "Add crushed tomatoes, reduce heat, and simmer ragù for 45 minutes.", "timer": 45},
            {"n": 3, "instruction": "Make béchamel: melt butter, whisk in flour, gradually add warm milk.", "timer": 10},
            {"n": 4, "instruction": "Layer pasta, ragù, béchamel, and mozzarella. Repeat 3 times."},
            {"n": 5, "instruction": "Bake at 375°F for 35 minutes until bubbly and golden.", "timer": 35},
        ],
    },
    {
        "id": "r3", "title": "Miso Ramen",
        "description": "Rich, deeply umami broth with noodles, soft-boiled egg, and crispy pork belly.",
        "blurhash": "LDEL;F~q00x]00Rj%Mt7?bWB-;j[",
        "cook_time_mins": 45, "difficulty": "medium",
        "imported_from": "tiktok", "source": "@ramenlord",
        "tags": ["dairy-free", "high-protein"],
        "calories": 590, "protein_g": 38, "carbs_g": 62, "fat_g": 22,
        "ingredients": [
            {"name": "Ramen Noodles",   "quantity": 200,  "unit": "g"},
            {"name": "Miso Paste",      "quantity": 3,    "unit": "tbsp"},
            {"name": "Pork Belly",      "quantity": 300,  "unit": "g"},
            {"name": "Soft Boiled Eggs","quantity": 2,    "unit": "whole"},
            {"name": "Chicken Stock",   "quantity": 1000, "unit": "ml"},
            {"name": "Green Onions",    "quantity": 3,    "unit": "stalks"},
            {"name": "Nori",            "quantity": 2,    "unit": "sheets"},
        ],
        "steps": [
            {"n": 1, "instruction": "Simmer chicken stock with miso paste and sesame oil for 20 minutes.", "timer": 20},
            {"n": 2, "instruction": "Pan-fry pork belly until crispy on all sides.", "timer": 10},
            {"n": 3, "instruction": "Cook ramen noodles according to package directions.", "timer": 4},
            {"n": 4, "instruction": "Assemble: noodles, hot broth, pork belly, halved egg, green onions, nori."},
        ],
    },
    {
        "id": "r4", "title": "Pad Thai",
        "description": "Wok-fried rice noodles with shrimp, egg, bean sprouts, and a tangy tamarind sauce.",
        "blurhash": "LBF=.800~qkC00IU%Mx]?bRj-;ay",
        "cook_time_mins": 25, "difficulty": "medium",
        "imported_from": "instagram", "source": "@thai.kitchen",
        "tags": ["dairy-free", "gluten-free"],
        "calories": 480, "protein_g": 28, "carbs_g": 68, "fat_g": 12,
        "ingredients": [
            {"name": "Rice Noodles",   "quantity": 200, "unit": "g"},
            {"name": "Shrimp",         "quantity": 250, "unit": "g"},
            {"name": "Eggs",           "quantity": 2,   "unit": "whole"},
            {"name": "Bean Sprouts",   "quantity": 100, "unit": "g"},
            {"name": "Tamarind Paste", "quantity": 2,   "unit": "tbsp"},
            {"name": "Fish Sauce",     "quantity": 2,   "unit": "tbsp"},
            {"name": "Lime",           "quantity": 1,   "unit": "whole"},
        ],
        "steps": [
            {"n": 1, "instruction": "Soak rice noodles in warm water for 30 minutes, drain."},
            {"n": 2, "instruction": "Mix tamarind paste, fish sauce, sugar, and lime juice for the sauce."},
            {"n": 3, "instruction": "Stir-fry shrimp in hot wok until pink. Push aside, scramble eggs."},
            {"n": 4, "instruction": "Add noodles and sauce. Toss until coated. Top with bean sprouts and peanuts."},
        ],
    },
    {
        "id": "r5", "title": "Chicken Tacos al Pastor",
        "description": "Marinated chicken with pineapple, charred to perfection, served on warm corn tortillas.",
        "blurhash": "LCEG{H~q00xZ00Rj%Mt700WB-;of",
        "cook_time_mins": 30, "difficulty": "easy",
        "imported_from": "tiktok", "source": "@mexicana.cooks",
        "tags": ["dairy-free", "gluten-free"],
        "calories": 420, "protein_g": 32, "carbs_g": 44, "fat_g": 14,
        "ingredients": [
            {"name": "Chicken Thighs",      "quantity": 600, "unit": "g"},
            {"name": "Corn Tortillas",      "quantity": 8,   "unit": "whole"},
            {"name": "Pineapple",           "quantity": 150, "unit": "g"},
            {"name": "Chipotle in Adobo",   "quantity": 2,   "unit": "tbsp"},
            {"name": "Cilantro",            "quantity": 1,   "unit": "bunch"},
            {"name": "White Onion",         "quantity": 1,   "unit": "medium"},
        ],
        "steps": [
            {"n": 1, "instruction": "Blend chipotle, pineapple juice, garlic, and spices for marinade."},
            {"n": 2, "instruction": "Marinate chicken thighs for at least 30 minutes.", "timer": 30},
            {"n": 3, "instruction": "Grill or pan-sear chicken until charred and cooked through.", "timer": 12},
            {"n": 4, "instruction": "Slice thin, serve on warm tortillas with pineapple, onion, and cilantro."},
        ],
    },
    {
        "id": "r6", "title": "Black Bean Enchiladas",
        "description": "Hearty vegetarian enchiladas loaded with black beans, roasted peppers, and smoky red sauce.",
        "blurhash": "LDDc*?~q00xu00Rj%Mt700WB-;of",
        "cook_time_mins": 40, "difficulty": "easy",
        "imported_from": "manual", "source": "My Kitchen",
        "tags": ["vegetarian", "gluten-free"],
        "calories": 390, "protein_g": 16, "carbs_g": 58, "fat_g": 10,
        "ingredients": [
            {"name": "Black Beans",         "quantity": 400, "unit": "g"},
            {"name": "Flour Tortillas",     "quantity": 8,   "unit": "whole"},
            {"name": "Red Enchilada Sauce", "quantity": 400, "unit": "ml"},
            {"name": "Cheddar Cheese",      "quantity": 150, "unit": "g"},
            {"name": "Red Bell Pepper",     "quantity": 2,   "unit": "whole"},
            {"name": "Cumin",               "quantity": 1,   "unit": "tsp"},
        ],
        "steps": [
            {"n": 1, "instruction": "Roast bell peppers at 400°F for 20 minutes until charred.", "timer": 20},
            {"n": 2, "instruction": "Mix drained beans with roasted peppers, cumin, salt, and half the cheese."},
            {"n": 3, "instruction": "Fill tortillas, roll tightly, place seam-down in baking dish."},
            {"n": 4, "instruction": "Pour enchilada sauce over top, cover with remaining cheese."},
            {"n": 5, "instruction": "Bake at 375°F for 20 minutes until bubbly.", "timer": 20},
        ],
    },
    {
        "id": "r7", "title": "Greek Salad with Grilled Halloumi",
        "description": "Fresh tomatoes, cucumber, olives, and crispy halloumi with herby oregano dressing.",
        "blurhash": "LAFA:N~q00xu00Rj%Mt700WB-;of",
        "cook_time_mins": 15, "difficulty": "easy",
        "imported_from": "url", "source": "ottolenghi.co.uk",
        "tags": ["vegetarian", "gluten-free", "mediterranean"],
        "calories": 340, "protein_g": 18, "carbs_g": 14, "fat_g": 24,
        "ingredients": [
            {"name": "Halloumi",        "quantity": 250,  "unit": "g"},
            {"name": "Cherry Tomatoes", "quantity": 200,  "unit": "g"},
            {"name": "Cucumber",        "quantity": 1,    "unit": "large"},
            {"name": "Kalamata Olives", "quantity": 80,   "unit": "g"},
            {"name": "Red Onion",       "quantity": 0.5,  "unit": "medium"},
            {"name": "Dried Oregano",   "quantity": 1,    "unit": "tsp"},
            {"name": "Olive Oil",       "quantity": 3,    "unit": "tbsp"},
        ],
        "steps": [
            {"n": 1, "instruction": "Slice halloumi and grill in dry pan until golden on each side.", "timer": 6},
            {"n": 2, "instruction": "Chop tomatoes, cucumber, and red onion. Combine with olives."},
            {"n": 3, "instruction": "Whisk olive oil, lemon juice, and dried oregano for dressing."},
            {"n": 4, "instruction": "Toss salad with dressing, top with warm grilled halloumi."},
        ],
    },
    {
        "id": "r8", "title": "Hummus Bowl with Roasted Vegetables",
        "description": "Creamy tahini hummus topped with oven-roasted seasonal vegetables and pomegranate seeds.",
        "blurhash": "LDFA?I~q00xu00Rj%Mt700WB-;of",
        "cook_time_mins": 35, "difficulty": "easy",
        "imported_from": "instagram", "source": "@plant.plate",
        "tags": ["vegan", "gluten-free", "mediterranean", "dairy-free"],
        "calories": 310, "protein_g": 12, "carbs_g": 38, "fat_g": 14,
        "ingredients": [
            {"name": "Chickpeas",    "quantity": 400, "unit": "g"},
            {"name": "Tahini",       "quantity": 3,   "unit": "tbsp"},
            {"name": "Zucchini",     "quantity": 2,   "unit": "medium"},
            {"name": "Eggplant",     "quantity": 1,   "unit": "medium"},
            {"name": "Garlic",       "quantity": 3,   "unit": "cloves"},
            {"name": "Lemon Juice",  "quantity": 2,   "unit": "tbsp"},
        ],
        "steps": [
            {"n": 1, "instruction": "Roast cubed zucchini and eggplant at 425°F with olive oil and salt.", "timer": 25},
            {"n": 2, "instruction": "Blend chickpeas, tahini, lemon juice, garlic, and ice water until silky smooth.", "timer": 5},
            {"n": 3, "instruction": "Spread hummus in bowls, top with roasted vegetables and pomegranate."},
        ],
    },
    {
        "id": "r9", "title": "Smash Burgers",
        "description": "Thin crispy-edged beef patties with American cheese, pickles, and special sauce on brioche.",
        "blurhash": "LBFiPK~q00xu00Rj%Mt700WB-;of",
        "cook_time_mins": 20, "difficulty": "easy",
        "imported_from": "tiktok", "source": "@burger.lab",
        "tags": ["high-protein"],
        "calories": 720, "protein_g": 42, "carbs_g": 48, "fat_g": 36,
        "ingredients": [
            {"name": "Ground Beef 80/20", "quantity": 500, "unit": "g"},
            {"name": "Brioche Buns",      "quantity": 4,   "unit": "whole"},
            {"name": "American Cheese",   "quantity": 4,   "unit": "slices"},
            {"name": "Dill Pickles",      "quantity": 8,   "unit": "slices"},
            {"name": "Mayonnaise",        "quantity": 3,   "unit": "tbsp"},
            {"name": "Yellow Mustard",    "quantity": 1,   "unit": "tbsp"},
        ],
        "steps": [
            {"n": 1, "instruction": "Form beef into 2oz loosely packed balls. Season with salt and pepper."},
            {"n": 2, "instruction": "Heat cast iron skillet until screaming hot. Add beef balls and smash flat immediately.", "timer": 2},
            {"n": 3, "instruction": "Cook until edges are crispy and brown. Flip, add cheese, cover for 30 seconds."},
            {"n": 4, "instruction": "Mix mayo, ketchup, mustard for sauce. Build burgers with sauce and pickles."},
        ],
    },
    {
        "id": "r10", "title": "Avocado Toast with Poached Eggs",
        "description": "Thick sourdough, smashed avocado, perfectly poached eggs, everything bagel seasoning.",
        "blurhash": "LCFC:N~q00xu00Rj%Mt700WB-;of",
        "cook_time_mins": 12, "difficulty": "easy",
        "imported_from": "instagram", "source": "@morning.eats",
        "tags": ["vegetarian", "dairy-free"],
        "calories": 380, "protein_g": 14, "carbs_g": 32, "fat_g": 22,
        "ingredients": [
            {"name": "Sourdough Bread",           "quantity": 2,   "unit": "thick slices"},
            {"name": "Avocado",                   "quantity": 1,   "unit": "large"},
            {"name": "Eggs",                      "quantity": 2,   "unit": "whole"},
            {"name": "Lemon",                     "quantity": 0.5, "unit": "whole"},
            {"name": "Red Pepper Flakes",         "quantity": 0.5, "unit": "tsp"},
            {"name": "Everything Bagel Seasoning","quantity": 1,   "unit": "tsp"},
        ],
        "steps": [
            {"n": 1, "instruction": "Toast sourdough until deep golden and crispy."},
            {"n": 2, "instruction": "Poach eggs in simmering water with a splash of vinegar for 3 minutes.", "timer": 3},
            {"n": 3, "instruction": "Smash avocado with lemon juice, salt. Spread thickly on toast."},
            {"n": 4, "instruction": "Top with poached eggs, everything bagel seasoning, and chili flakes."},
        ],
    },
]

MOCK_COLLECTIONS = [
    {"id": "col-1", "name": "Weeknight Dinners", "description": "Quick meals ready in under 30 minutes", "emoji": "🍳", "recipe_ids": ["r1", "r4", "r5", "r10"]},
    {"id": "col-2", "name": "Date Night",         "description": "Impress with these show-stoppers",      "emoji": "🍷", "recipe_ids": ["r2", "r3", "r7"]},
    {"id": "col-3", "name": "Plant Power",         "description": "Vegetarian & vegan favorites",          "emoji": "🥬", "recipe_ids": ["r6", "r7", "r8", "r10"]},
    {"id": "col-4", "name": "Meal Prep Sunday",    "description": "Batch-cook recipes that keep well",     "emoji": "📦", "recipe_ids": ["r2", "r6", "r8"]},
]

MOCK_STORES = [
    {"id": "s1", "name": "Kroger",     "logo": "kroger",     "supports_pickup": True,  "is_instacart": True},
    {"id": "s2", "name": "Walmart",    "logo": "walmart",    "supports_pickup": True,  "is_instacart": False},
    {"id": "s3", "name": "H-Mart",     "logo": "hmart",      "supports_pickup": True,  "is_instacart": True},
    {"id": "s4", "name": "Costco",     "logo": "costco",     "supports_pickup": True,  "is_instacart": True},
    {"id": "s5", "name": "Sprouts",    "logo": "sprouts",    "supports_pickup": True,  "is_instacart": True},
    {"id": "s6", "name": "Albertsons", "logo": "albertsons", "supports_pickup": True,  "is_instacart": True},
]

MOCK_PANTRY = [
    {"id": "p1",  "name": "Eggs",            "quantity": 6,   "unit": "whole",   "category": "proteins",   "added_via": "manual",   "expiry": tomorrow},
    {"id": "p2",  "name": "Whole Milk",      "quantity": 500, "unit": "ml",      "category": "dairy",      "added_via": "receipt",  "expiry": in_two_days},
    {"id": "p3",  "name": "Spinach",         "quantity": 100, "unit": "g",       "category": "produce",    "added_via": "manual",   "expiry": in_two_days},
    {"id": "p4",  "name": "Chicken Thighs",  "quantity": 400, "unit": "g",       "category": "proteins",   "added_via": "receipt",  "expiry": in_five_days},
    {"id": "p5",  "name": "Garlic",          "quantity": 6,   "unit": "cloves",  "category": "produce",    "added_via": "manual",   "expiry": None},
    {"id": "p6",  "name": "Olive Oil",       "quantity": 300, "unit": "ml",      "category": "pantry",     "added_via": "manual",   "expiry": None},
    {"id": "p7",  "name": "Black Pepper",    "quantity": 50,  "unit": "g",       "category": "spices",     "added_via": "manual",   "expiry": None},
    {"id": "p8",  "name": "Spaghetti",       "quantity": 400, "unit": "g",       "category": "grains",     "added_via": "receipt",  "expiry": None},
    {"id": "p9",  "name": "Crushed Tomatoes","quantity": 400, "unit": "g",       "category": "pantry",     "added_via": "receipt",  "expiry": None},
    {"id": "p10", "name": "Onion",           "quantity": 3,   "unit": "medium",  "category": "produce",    "added_via": "manual",   "expiry": None},
    {"id": "p11", "name": "Miso Paste",      "quantity": 200, "unit": "g",       "category": "condiments", "added_via": "scan",     "expiry": in_ten_days},
    {"id": "p12", "name": "Soy Sauce",       "quantity": 250, "unit": "ml",      "category": "condiments", "added_via": "manual",   "expiry": None},
    {"id": "p13", "name": "Avocado",         "quantity": 2,   "unit": "whole",   "category": "produce",    "added_via": "receipt",  "expiry": None},
    {"id": "p14", "name": "Chickpeas",       "quantity": 400, "unit": "g",       "category": "pantry",     "added_via": "receipt",  "expiry": None},
    {"id": "p15", "name": "Frozen Peas",     "quantity": 300, "unit": "g",       "category": "frozen",     "added_via": "manual",   "expiry": None},
    {"id": "p16", "name": "Sourdough Bread", "quantity": 1,   "unit": "loaf",    "category": "bakery",     "added_via": "manual",   "expiry": in_five_days},
    {"id": "p17", "name": "Lemon",           "quantity": 4,   "unit": "whole",   "category": "produce",    "added_via": "manual",   "expiry": None},
    {"id": "p18", "name": "Cumin",           "quantity": 30,  "unit": "g",       "category": "spices",     "added_via": "manual",   "expiry": None},
    {"id": "p19", "name": "Orange Juice",    "quantity": 500, "unit": "ml",      "category": "beverages",  "added_via": "receipt",  "expiry": in_five_days},
    {"id": "p20", "name": "All-Purpose Flour","quantity": 500,"unit": "g",       "category": "grains",     "added_via": "manual",   "expiry": None},
]

MOCK_GROCERY_ITEMS = [
    {"id": "g1",  "name": "Pecorino Romano",  "quantity": 100, "unit": "g",     "checked": False, "aisle": "Dairy & Eggs",       "recipe_id": "r1"},
    {"id": "g2",  "name": "Ground Beef 80/20","quantity": 500, "unit": "g",     "checked": False, "aisle": "Meat & Seafood",     "recipe_id": "r9"},
    {"id": "g3",  "name": "Brioche Buns",     "quantity": 4,   "unit": "whole", "checked": False, "aisle": "Bakery",             "recipe_id": "r9"},
    {"id": "g4",  "name": "Shrimp",           "quantity": 250, "unit": "g",     "checked": False, "aisle": "Meat & Seafood",     "recipe_id": "r4"},
    {"id": "g5",  "name": "Rice Noodles",     "quantity": 200, "unit": "g",     "checked": False, "aisle": "Grains & Pasta",     "recipe_id": "r4"},
    {"id": "g6",  "name": "Tamarind Paste",   "quantity": 1,   "unit": "jar",   "checked": False, "aisle": "Condiments & Sauces","recipe_id": "r4"},
    {"id": "g7",  "name": "Halloumi",         "quantity": 250, "unit": "g",     "checked": False, "aisle": "Dairy & Eggs",       "recipe_id": "r7"},
    {"id": "g8",  "name": "Cherry Tomatoes",  "quantity": 200, "unit": "g",     "checked": True,  "aisle": "Produce",            "recipe_id": "r7"},
    {"id": "g9",  "name": "Kalamata Olives",  "quantity": 80,  "unit": "g",     "checked": True,  "aisle": "Pantry",             "recipe_id": "r7"},
    {"id": "g10", "name": "Pork Belly",       "quantity": 300, "unit": "g",     "checked": True,  "aisle": "Meat & Seafood",     "recipe_id": "r3"},
    {"id": "g11", "name": "Ramen Noodles",    "quantity": 200, "unit": "g",     "checked": True,  "aisle": "Grains & Pasta",     "recipe_id": "r3"},
    {"id": "g12", "name": "Nori",             "quantity": 1,   "unit": "pack",  "checked": False, "aisle": "Pantry",             "recipe_id": "r3"},
]

MOCK_WASTE_LOG = [
    {"item_name": "Spinach",       "action": "used",   "value": 2.5, "date": "2026-03-22T18:00:00Z"},
    {"item_name": "Chicken Breast","action": "used",   "value": 5.0, "date": "2026-03-21T19:30:00Z"},
    {"item_name": "Yogurt",        "action": "used",   "value": 3.5, "date": "2026-03-20T12:00:00Z"},
    {"item_name": "Lettuce",       "action": "tossed", "value": 2.0, "date": "2026-03-19T09:00:00Z"},
    {"item_name": "Avocado",       "action": "used",   "value": 2.5, "date": "2026-03-18T20:00:00Z"},
    {"item_name": "Salmon",        "action": "used",   "value": 8.0, "date": "2026-03-17T18:30:00Z"},
    {"item_name": "Bread",         "action": "tossed", "value": 3.0, "date": "2026-03-16T10:00:00Z"},
    {"item_name": "Bell Peppers",  "action": "used",   "value": 3.0, "date": "2026-03-15T19:00:00Z"},
    {"item_name": "Ground Beef",   "action": "used",   "value": 5.0, "date": "2026-03-14T18:00:00Z"},
    {"item_name": "Mushrooms",     "action": "used",   "value": 2.5, "date": "2026-03-13T17:30:00Z"},
    {"item_name": "Milk",          "action": "tossed", "value": 3.5, "date": "2026-03-12T08:00:00Z"},
    {"item_name": "Eggs",          "action": "used",   "value": 4.0, "date": "2026-03-10T12:00:00Z"},
]


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def resolve_ingredient(cur, name: str, ingredient_cache: dict) -> str:
    """Return ingredient UUID for name, creating a stub row if not found."""
    if name in ingredient_cache:
        return ingredient_cache[name]

    # Try exact match first, then case-insensitive
    cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT id FROM ingredients WHERE LOWER(name) = LOWER(%s)", (name,))
        row = cur.fetchone()

    if row:
        ingredient_cache[name] = str(row[0])
        return ingredient_cache[name]

    # Not in USDA data — create a stub ingredient
    new_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO ingredients (id, name) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING RETURNING id",
        (new_id, name),
    )
    result = cur.fetchone()
    if not result:
        # Conflict: another process inserted it, fetch again
        cur.execute("SELECT id FROM ingredients WHERE name = %s", (name,))
        result = cur.fetchone()
    ingredient_cache[name] = str(result[0])
    return ingredient_cache[name]


# ------------------------------------------------------------------ #
# Main seed
# ------------------------------------------------------------------ #

def seed(conn) -> None:
    cur = conn.cursor()
    ingredient_cache: dict = {}

    # ---- Clean up existing demo data ----
    print("Cleaning up existing demo data...")
    cur.execute("DELETE FROM user_profiles WHERE id = %s", (DEMO_USER_ID,))
    # CASCADE handles all child rows

    # Also clean stores and any stub ingredients we may have created previously
    for s in MOCK_STORES:
        cur.execute("DELETE FROM stores WHERE id = %s", (STORE_IDS[s["id"]],))

    # ---- User ----
    print("Inserting user...")
    cur.execute("""
        INSERT INTO user_profiles (id, email, display_name, onboarding_complete)
        VALUES (%s, %s, %s, %s)
    """, (DEMO_USER_ID, "alex@example.com", "Alex Rivera", True))

    cur.execute("""
        INSERT INTO user_preferences (user_id, dietary_tags, allergies, cuisines, household_size)
        VALUES (%s, %s, %s, %s, %s)
    """, (DEMO_USER_ID, ["vegetarian", "gluten-free"], ["peanuts"], ["Italian", "Asian", "Mediterranean"], 2))

    psycopg2.extras.execute_values(cur, """
        INSERT INTO user_addresses (id, user_id, label, street, city, state, zip, is_default)
        VALUES %s
    """, [
        (ADDRESS_IDS["addr-1"], DEMO_USER_ID, "Home", "1234 Oak Street",   "Austin", "TX", "78701", True),
        (ADDRESS_IDS["addr-2"], DEMO_USER_ID, "Work", "500 Congress Ave",  "Austin", "TX", "78701", False),
    ])

    cur.execute("""
        INSERT INTO connected_accounts (user_id, provider, handle)
        VALUES (%s, %s, %s)
    """, (DEMO_USER_ID, "instagram", "@alex.eats"))

    # ---- Stores ----
    print("Inserting stores...")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO stores (id, name, logo, supports_pickup, is_instacart)
        VALUES %s
    """, [
        (STORE_IDS[s["id"]], s["name"], s["logo"], s["supports_pickup"], s["is_instacart"])
        for s in MOCK_STORES
    ])

    # ---- Recipes ----
    print("Inserting recipes...")
    for r in MOCK_RECIPES:
        recipe_uuid = RECIPE_IDS[r["id"]]
        platform = PLATFORM_MAP.get(r["imported_from"])
        source_url = r["source"] if r["imported_from"] == "url" else None
        creator_handle = r["source"] if r["source"].startswith("@") else None

        cur.execute("""
            INSERT INTO recipes (
                id, title, description, blurhash, cook_time_mins, difficulty,
                source_platform, source_type, source_url, creator_handle,
                calories, protein_g, carbs_g, fat_g,
                created_by, is_global
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            recipe_uuid, r["title"], r["description"], r["blurhash"],
            r["cook_time_mins"], r["difficulty"],
            platform, r["imported_from"], source_url, creator_handle,
            r["calories"], r["protein_g"], r["carbs_g"], r["fat_g"],
            DEMO_USER_ID, False,
        ))

        # Tags
        if r.get("tags"):
            psycopg2.extras.execute_values(cur,
                "INSERT INTO recipe_tags (recipe_id, tag) VALUES %s ON CONFLICT DO NOTHING",
                [(recipe_uuid, tag) for tag in r["tags"]],
            )

        # Steps
        psycopg2.extras.execute_values(cur,
            "INSERT INTO recipe_steps (recipe_id, step_number, instruction, duration_minutes) VALUES %s",
            [(recipe_uuid, s["n"], s["instruction"], s.get("timer")) for s in r["steps"]],
        )

        # Ingredients
        for idx, ing in enumerate(r["ingredients"]):
            ing_id = resolve_ingredient(cur, ing["name"], ingredient_cache)
            cur.execute("""
                INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit, order_index, display_text)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (recipe_uuid, ing_id, ing["quantity"], ing["unit"], idx, ing["name"]))

    # ---- Collections ----
    print("Inserting collections...")
    for col in MOCK_COLLECTIONS:
        col_uuid = COLLECTION_IDS[col["id"]]
        cur.execute("""
            INSERT INTO collections (id, user_id, name, description, emoji)
            VALUES (%s, %s, %s, %s, %s)
        """, (col_uuid, DEMO_USER_ID, col["name"], col["description"], col["emoji"]))

        psycopg2.extras.execute_values(cur,
            "INSERT INTO collection_recipes (collection_id, recipe_id) VALUES %s ON CONFLICT DO NOTHING",
            [(col_uuid, RECIPE_IDS[rid]) for rid in col["recipe_ids"]],
        )

    # ---- Pantry ----
    print("Inserting pantry items...")
    for item in MOCK_PANTRY:
        ing_id = resolve_ingredient(cur, item["name"], ingredient_cache)
        location = CATEGORY_TO_LOCATION.get(item["category"], "pantry")
        cur.execute("""
            INSERT INTO pantry_items (id, user_id, name, ingredient_id, quantity, unit, location, expiry_date, added_via)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            PANTRY_IDS[item["id"]], DEMO_USER_ID, item["name"], ing_id,
            item["quantity"], item["unit"], location,
            item["expiry"], item["added_via"],
        ))

    # ---- Grocery list + items ----
    print("Inserting grocery list...")
    cur.execute("""
        INSERT INTO grocery_lists (id, user_id, name, status)
        VALUES (%s, %s, %s, %s)
    """, (GROCERY_LIST_ID, DEMO_USER_ID, "This Week's List", "active"))

    for item in MOCK_GROCERY_ITEMS:
        ing_id = resolve_ingredient(cur, item["name"], ingredient_cache)
        source_recipe_uuid = RECIPE_IDS.get(item.get("recipe_id"))
        cur.execute("""
            INSERT INTO grocery_items (id, list_id, name, ingredient_id, quantity, unit, is_checked, aisle, source_recipe_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            GROCERY_IDS[item["id"]], GROCERY_LIST_ID, item["name"], ing_id,
            item["quantity"], item["unit"], item["checked"],
            item["aisle"], source_recipe_uuid,
        ))

    # ---- Order ----
    print("Inserting order...")
    cur.execute("""
        INSERT INTO orders (id, user_id, store_id, address_id, status, total, placed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        ORDER_ID, DEMO_USER_ID, STORE_IDS["s1"], ADDRESS_IDS["addr-1"],
        "preparing", 34.87,
        (now - timedelta(minutes=15)).isoformat(),
    ))

    # Link grocery list to order
    cur.execute("UPDATE grocery_lists SET order_id = %s WHERE id = %s", (ORDER_ID, GROCERY_LIST_ID))

    # ---- Waste log ----
    print("Inserting waste log...")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO waste_log (user_id, item_name, action, estimated_value, logged_at)
        VALUES %s
    """, [
        (DEMO_USER_ID, w["item_name"], w["action"], w["value"], w["date"])
        for w in MOCK_WASTE_LOG
    ])

    conn.commit()
    cur.close()
    print("Done. Demo user seeded:", DEMO_USER_ID)


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Error: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print("Connecting to database...")
    conn = psycopg2.connect(database_url)
    psycopg2.extras.register_uuid()
    try:
        seed(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
