import base64
import json
import logging

import anthropic
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GCV_URL = "https://vision.googleapis.com/v1/images:annotate"
_SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_BYTES = 5 * 1024 * 1024

_RECEIPT_SCHEMA = """
{
  "scanned_at": "ISO8601 UTC timestamp",
  "store": {
    "name": "string or null",
    "address": "string or null",
    "phone": "string or null"
  },
  "summary": {
    "item_count": "integer — count of items array",
    "subtotal": "float or null",
    "tax": "float or null",
    "total": "float or null — use the largest total found"
  },
  "items": [
    {
      "name": "string — clean lowercase product name (e.g. 'chicken breast', 'whole milk')",
      "brand": "string or null — brand name if identifiable (e.g. 'Tyson', 'Kirkland')",
      "variant": "string or null — size/pack descriptor (e.g. '3 lb', '12 oz', '6-pack')",
      "quantity": "float or null — numeric quantity purchased",
      "unit": "string or null — unit of measure (e.g. lb, oz, gallon, each, count)",
      "unit_price": "float or null",
      "total_price": "float or null — price paid for this line",
      "category": "exactly one of: produce, proteins, dairy, grains, frozen, pantry, condiments, spices, bakery, beverages",
      "subcategory": "string or null — e.g. 'poultry', 'fruit', 'cheese'",
      "on_sale": "boolean — true if a discount or savings line follows this item",
      "confidence": "high | medium | low",
      "parsing_notes": "string or null — note if quantity was estimated or line was ambiguous",
      "raw_line": "the exact OCR line this item came from"
    }
  ],
  "unparsed_lines": ["exact OCR lines that could not be matched to a product"]
}
"""

_PARSE_SYSTEM = (
    "You are a grocery receipt parsing assistant. "
    "Respond ONLY with valid JSON — no markdown fences, no explanation, no extra text."
)


async def _call_gcv(image_bytes: bytes) -> str:
    """POST image to GCV DOCUMENT_TEXT_DETECTION. Returns raw text string."""
    if not settings.GCV_API_KEY:
        raise RuntimeError("GCV_API_KEY is not configured.")

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_GCV_URL}?key={settings.GCV_API_KEY}",
            json={
                "requests": [
                    {
                        "image": {"content": b64},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            },
        )
        resp.raise_for_status()

    data = resp.json()
    return data.get("responses", [{}])[0].get("fullTextAnnotation", {}).get("text", "")


async def _call_haiku(raw_text: str) -> dict:
    """Parse raw receipt OCR text with Claude Haiku. Returns ScanResponse dict."""
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    prompt = (
        "Parse this grocery receipt OCR text into structured JSON.\n\n"
        f"Return JSON matching this schema exactly:\n{_RECEIPT_SCHEMA}\n\n"
        "Rules:\n"
        "- Skip non-product lines (cashier ID, payment method, loyalty points, timestamps)\n"
        "- Extract store name, address, and phone from the header lines\n"
        "- Identify subtotal, tax, and total from the footer lines\n"
        "- For weighted items (e.g. '1.23 lb @ 2.99/lb'), compute total_price = weight × unit_price\n"
        "- For discount lines (e.g. 'MEMBER SAVINGS -1.50'), set on_sale=true on the preceding item\n"
        "- Lines you cannot confidently match to a product go in unparsed_lines\n"
        "- item_count must equal the length of the items array\n\n"
        f"Receipt text:\n{raw_text}"
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        system=_PARSE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rstrip("`").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Haiku returned invalid JSON: %s", e)
        raise RuntimeError("Receipt parsing failed. Please try again.")
    parsed["raw_text"] = raw_text
    return parsed


async def parse_receipt_image(image_bytes: bytes, content_type: str) -> dict:
    """
    Validate image, run GCV OCR, parse with Haiku, return ScanResponse dict.

    Raises ValueError on bad input (wrong type or oversized).
    Raises RuntimeError on GCV or Haiku failure.
    """
    if content_type not in _SUPPORTED_TYPES:
        raise ValueError(
            f"Unsupported image type '{content_type}'. Use JPEG, PNG, or WebP."
        )

    if len(image_bytes) > _MAX_BYTES:
        raise ValueError("Image exceeds 5 MB limit.")

    try:
        raw_text = await _call_gcv(image_bytes)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("GCV call failed: %s", e)
        raise RuntimeError("OCR failed. Please try again.")

    if not raw_text.strip():
        raise RuntimeError("No text found in image.")

    try:
        return await _call_haiku(raw_text)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Haiku parsing failed: %s", e)
        raise RuntimeError("Receipt parsing failed. Please try again.")
