import asyncio
import logging
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.DEBUG, format="%(name)s | %(levelname)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("google_adk").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
from app.db import open_pool, close_pool
from app.agents.ingredient_resolver.agent import run_ingredient_resolver, _normalize

TESTS = [
    # (source, raw_name)

    # --- Pantry: clean names from manual input ---
    ("pantry",  "eggs"),
    ("pantry",  "yellow onion"),
    ("pantry",  "cheddar cheese"),
    ("pantry",  "chicken breast"),

    # --- Recipe import: raw ingredient lines ---
    ("recipe",  "2 cups finely chopped organic yellow onion"),
    ("recipe",  "1/2 lb boneless skinless chicken breast, diced"),
    ("recipe",  "3 cloves garlic, minced"),
    ("recipe",  "1 can (14 oz) diced tomatoes"),
    ("recipe",  "freshly ground black pepper"),

    # --- Alias / fuzzy cases ---
    ("alias",   "scallion"),          # should match green onion
    ("alias",   "spring onion"),      # same

    # --- Novel ingredient: should create a new row ---
    ("novel",   "szechuan peppercorn flakes"),
]


async def main():
    await open_pool()
    print(f"{'SOURCE':<10} {'RAW NAME':<45} {'NORMALIZED':<30} {'ingredient_id'}")
    print("-" * 130)
    for source, raw in TESTS:
        normalized = _normalize(raw)
        result = await run_ingredient_resolver(raw)
        print(f"{source:<10} {raw:<45} {normalized:<30} {result}")
    await close_pool()


asyncio.run(main())
