"""
Recipe parsing service.

- parse_from_youtube: Fetches YouTube video metadata via the official Data API v3,
  then uses Claude Haiku to extract a structured recipe from the title + description.

- parse_from_image: Uses Claude Sonnet vision to extract a recipe from a photo
  (paper recipe, screenshot, etc.). Returns a draft; caller decides whether to save.
"""

import base64
import json
import logging
import re

import anthropic
import httpx

from config import settings

logger = logging.getLogger(__name__)

_YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3/videos"

_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# Shared Claude prompt + response parsing
# ---------------------------------------------------------------------------

_RECIPE_JSON_SCHEMA = """
{
  "title": "string — concise recipe name",
  "description": "string — 1-2 sentence appetizing summary of the dish",
  "cookTimeMinutes": "integer — total prep + cook time in minutes (estimate from steps if not stated, default 30)",
  "difficulty": "one of exactly: easy, medium, hard",
  "ingredients": [
    {
      "name": "string — common ingredient name, lowercase",
      "quantity": "number — numeric amount (0 if truly unknown)",
      "unit": "string — e.g. cup, tbsp, tsp, g, oz, lb, ml, count, clove, slice, pinch"
    }
  ],
  "steps": [
    {
      "stepNumber": "integer starting at 1",
      "instruction": "string — single clear action starting with a verb (Heat, Mix, Add, Bake, Stir, etc.)",
      "timerMinutes": "integer if a specific time is mentioned for this step, otherwise null"
    }
  ],
  "tags": ["pick ONLY tags that definitively apply from this exact list: vegan, vegetarian, gluten-free, dairy-free, keto, paleo, nut-free, low-carb, high-protein — leave array empty [] if none clearly apply"]
}
"""

_PARSE_SYSTEM = (
    "You are a recipe extraction assistant. "
    "Respond ONLY with valid JSON — no markdown fences, no explanation, no extra text."
)


def _call_claude_haiku(text: str) -> dict:
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    prompt = (
        "You are classifying whether content is food/cooking related, then extracting a recipe.\n\n"
        "STEP 1 — Is this content related to ANY of the following?\n"
        "  • Cooking, baking, grilling, frying, or food preparation\n"
        "  • Recipes, ingredients, or kitchen techniques\n"
        "  • Food culture, restaurant, street food, or mukbang\n"
        "  • Meal planning, nutrition, or diet\n"
        "  • Any video where food is cooked, prepared, or discussed\n\n"
        "Be VERY PERMISSIVE. If there is any indication of food/cooking — even if the title or description "
        "is in another language, or the description is sparse — classify it as food-related.\n"
        "Only return {\"error\": \"not_food_content\"} if the content is OBVIOUSLY about something completely "
        "unrelated to food (e.g. pure gaming, politics, sports, tech tutorial with no food).\n\n"
        "STEP 2 — If food-related, extract the recipe as JSON matching this schema:\n"
        f"{_RECIPE_JSON_SCHEMA}\n\n"
        "Extraction rules:\n"
        "- Each step is a single action starting with a verb (Heat, Mix, Add, Bake, etc.)\n"
        "- Split compound steps; aim for 6-20 steps total\n"
        "- If the description is in a non-English language, translate ingredient names and steps to English\n"
        "- Only include dietary tags definitively supported by the ingredients\n"
        "- Estimate cook time from steps if not stated\n"
        "- If the description has no recipe but the title is clearly a dish, create a reasonable recipe for that dish\n\n"
        f"Content:\n{text}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=_PARSE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    parsed = json.loads(raw)
    if parsed.get("error") == "not_food_content":
        raise ValueError("This video does not appear to be food or cooking related.")
    return parsed


def _call_claude_sonnet_vision(image_b64: str, media_type: str) -> dict:
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    prompt = f"Extract the recipe from this image.\n\nReturn JSON matching this schema:\n{_RECIPE_JSON_SCHEMA}"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=_PARSE_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def _build_result(parsed: dict, source_url: str, imported_from: str, image_uri: str = "") -> dict:
    """Normalize Claude output into the shape expected by CreateRecipeRequest."""
    ingredients = [
        {
            "name": ing.get("name", ""),
            "quantity": float(ing.get("quantity") or 0),
            "unit": ing.get("unit", ""),
        }
        for ing in parsed.get("ingredients", [])
        if ing.get("name")
    ]
    steps = [
        {
            "stepNumber": step.get("stepNumber", i + 1),
            "instruction": step.get("instruction", ""),
            "timerMinutes": step.get("timerMinutes"),
        }
        for i, step in enumerate(parsed.get("steps", []))
        if step.get("instruction")
    ]
    difficulty = parsed.get("difficulty", "medium")
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"

    return {
        "title": parsed.get("title", "Imported Recipe"),
        "description": parsed.get("description", ""),
        "cookTimeMinutes": int(parsed.get("cookTimeMinutes") or 30),
        "difficulty": difficulty,
        "ingredients": ingredients,
        "steps": steps,
        "tags": parsed.get("tags", []),
        "imageUri": image_uri,
        "source": source_url,
        "importedFrom": imported_from,
        "nutrition": {},
    }


# ---------------------------------------------------------------------------
# YouTube video ID extraction
# ---------------------------------------------------------------------------

_YT_PATTERNS = [
    r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})",
]


def _extract_video_id(url: str) -> str | None:
    for pattern in _YT_PATTERNS:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# oEmbed helpers (TikTok + Instagram)
# ---------------------------------------------------------------------------

_TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
_INSTAGRAM_OEMBED = "https://graph.facebook.com/v18.0/instagram_oembed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def parse_from_youtube(url: str) -> dict:
    """
    Fetch YouTube video metadata via the official Data API v3, then extract
    a structured recipe using Claude Haiku.

    Raises ValueError if the URL is not a recognisable YouTube link.
    Raises RuntimeError on API / parsing failure.
    """
    if not settings.YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not configured.")

    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    video_id = _extract_video_id(url)
    if not video_id:
        raise ValueError(f"Could not extract a YouTube video ID from URL: {url}")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            _YOUTUBE_API_BASE,
            params={"part": "snippet", "id": video_id, "key": settings.YOUTUBE_API_KEY},
        )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    if not items:
        raise ValueError(f"YouTube video not found or is private: {video_id}")

    snippet = items[0]["snippet"]
    title = snippet.get("title", "")
    description = snippet.get("description", "")
    thumbnail_url = (
        snippet.get("thumbnails", {}).get("high", {}).get("url")
        or snippet.get("thumbnails", {}).get("default", {}).get("url")
        or ""
    )

    text_blob = f"Video title: {title}\n\nVideo description:\n{description}"

    try:
        parsed = _call_claude_haiku(text_blob)
    except Exception as e:
        logger.error("Claude Haiku recipe extraction failed for %s: %s", url, e)
        raise RuntimeError("Recipe extraction failed. The video description may not contain a recipe.")

    return _build_result(parsed, source_url=url, imported_from="youtube", image_uri=thumbnail_url)


async def parse_from_tiktok(url: str) -> dict:
    """
    Fetch TikTok video metadata via the public oEmbed API (no auth required),
    then extract a structured recipe using Claude Haiku.

    TikTok's oEmbed `title` field contains the full caption, which food creators
    typically use to post their recipe steps and ingredients.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(_TIKTOK_OEMBED, params={"url": url})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Could not fetch TikTok metadata: {e}")

    title = data.get("title", "")
    author = data.get("author_name", "")
    thumbnail_url = data.get("thumbnail_url", "")

    text_blob = f"TikTok creator: {author}\n\nVideo caption:\n{title}"

    try:
        parsed = _call_claude_haiku(text_blob)
    except Exception as e:
        logger.error("Claude Haiku recipe extraction failed for TikTok %s: %s", url, e)
        raise RuntimeError("Recipe extraction failed. The video caption may not contain a recipe.")

    return _build_result(parsed, source_url=url, imported_from="tiktok", image_uri=thumbnail_url)


async def parse_from_instagram(url: str) -> dict:
    """
    Fetch Instagram post/reel metadata via the Meta oEmbed API.
    Requires INSTAGRAM_ACCESS_TOKEN (app_id|app_secret) in config.

    The oEmbed `title` field contains the post caption, which food creators
    commonly use to share full recipes.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    if not settings.INSTAGRAM_ACCESS_TOKEN:
        raise RuntimeError(
            "INSTAGRAM_ACCESS_TOKEN is not configured. "
            "Create a Meta app at developers.facebook.com, enable 'oEmbed Read', "
            "then set INSTAGRAM_ACCESS_TOKEN=app_id|app_secret in .env"
        )

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                _INSTAGRAM_OEMBED,
                params={"url": url, "access_token": settings.INSTAGRAM_ACCESS_TOKEN},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Could not fetch Instagram metadata: {e}")

    caption = data.get("title", "")
    author = data.get("author_name", "")
    thumbnail_url = data.get("thumbnail_url", "")

    text_blob = f"Instagram creator: @{author}\n\nPost caption:\n{caption}"

    try:
        parsed = _call_claude_haiku(text_blob)
    except Exception as e:
        logger.error("Claude Haiku recipe extraction failed for Instagram %s: %s", url, e)
        raise RuntimeError("Recipe extraction failed. The post caption may not contain a recipe.")

    return _build_result(parsed, source_url=url, imported_from="instagram", image_uri=thumbnail_url)


async def parse_from_image(image_bytes: bytes, content_type: str) -> dict:
    """
    Use Claude Sonnet vision to extract a recipe from a photo.
    Returns a draft dict — the caller is responsible for saving to DB.

    Raises ValueError on unsupported format or oversized image.
    Raises RuntimeError on Claude failure.
    """
    if content_type not in _SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"Unsupported image type '{content_type}'. Use JPEG, PNG, or WebP.")

    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds 5 MB limit.")

    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        parsed = _call_claude_sonnet_vision(image_b64, content_type)
    except Exception as e:
        logger.error("Claude vision recipe extraction failed: %s", e)
        raise RuntimeError("Image analysis failed. Please try again.")

    return _build_result(parsed, source_url="", imported_from="manual", image_uri="")
