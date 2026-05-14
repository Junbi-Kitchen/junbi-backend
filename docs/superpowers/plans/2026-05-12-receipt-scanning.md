# Receipt Scanning — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move receipt OCR (Google Cloud Vision) and parsing (Claude Haiku) from the frontend to the backend, replacing the stub `POST /pantry/ocr` endpoint.

**Architecture:** A new `app/services/receipt_parser.py` service owns the GCV HTTP call and the Haiku parsing call. The existing route handler in `pantry.py` validates the upload and delegates to the service. The route returns a full `ScanResponse` dict (store info, summary, items with prices/categories).

**Tech Stack:** FastAPI, httpx (GCV REST), anthropic SDK (Claude Haiku), pydantic-settings (config), pytest + pytest-asyncio (tests)

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `config.py` | Add `GCV_API_KEY` setting |
| Create | `app/services/receipt_parser.py` | GCV call, Haiku call, validation, ScanResponse assembly |
| Modify | `app/api/routes/pantry.py` | Replace stub body of `POST /pantry/ocr` |
| Create | `tests/services/test_receipt_parser.py` | Unit tests for the service |

---

## Task 1: Create Feature Branch and Test Infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/services/__init__.py`
- Create: `tests/services/test_receipt_parser.py`

- [ ] **Step 1: Create the feature branch**

```bash
git checkout -b feature/receipt-scanning
```

- [ ] **Step 2: Install test dependencies**

```bash
pip install pytest pytest-asyncio
```

- [ ] **Step 3: Create test package structure**

```bash
mkdir -p tests/services
touch tests/__init__.py tests/services/__init__.py
```

- [ ] **Step 4: Create `tests/services/test_receipt_parser.py` with all failing tests**

```python
import pytest
from unittest.mock import MagicMock, patch

# All imports will fail until Task 2 creates the module — that's expected.
from app.services.receipt_parser import parse_receipt_image, _call_gcv, _call_haiku

VALID_JPEG = b"\xff\xd8\xff" + b"\x00" * 100  # minimal fake JPEG bytes
BIG_IMAGE = b"\x00" * (5 * 1024 * 1024 + 1)
SAMPLE_RAW_TEXT = (
    "WINCO FOODS\n1234 Main St\n503-555-0100\n"
    "CHICKEN BREAST    5.99\n"
    "MILK 1GAL         3.49\n"
    "Subtotal          9.48\n"
    "Tax               0.76\n"
    "Total             10.24\n"
)
SAMPLE_SCAN_RESPONSE = {
    "scanned_at": "2026-05-12T10:00:00Z",
    "store": {"name": "WINCO FOODS", "address": "1234 Main St", "phone": "503-555-0100"},
    "summary": {"item_count": 2, "subtotal": 9.48, "tax": 0.76, "total": 10.24},
    "items": [
        {
            "name": "chicken breast",
            "brand": None,
            "variant": None,
            "quantity": 1,
            "unit": "each",
            "unit_price": 5.99,
            "total_price": 5.99,
            "category": "proteins",
            "subcategory": "poultry",
            "on_sale": False,
            "confidence": "high",
            "parsing_notes": None,
            "raw_line": "CHICKEN BREAST    5.99",
        },
        {
            "name": "milk",
            "brand": None,
            "variant": "1GAL",
            "quantity": 1,
            "unit": "gallon",
            "unit_price": 3.49,
            "total_price": 3.49,
            "category": "dairy",
            "subcategory": None,
            "on_sale": False,
            "confidence": "high",
            "parsing_notes": None,
            "raw_line": "MILK 1GAL         3.49",
        },
    ],
    "unparsed_lines": [],
    "raw_text": SAMPLE_RAW_TEXT,
}


# ── Validation tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unsupported_image_type_raises_value_error():
    with pytest.raises(ValueError, match="Unsupported image type"):
        await parse_receipt_image(VALID_JPEG, "image/gif")


@pytest.mark.asyncio
async def test_oversized_image_raises_value_error():
    with pytest.raises(ValueError, match="exceeds 5 MB"):
        await parse_receipt_image(BIG_IMAGE, "image/jpeg")


# ── GCV tests ───────────────────────────────────────────────────────────────

def test_call_gcv_no_api_key_raises_runtime_error():
    with patch("app.services.receipt_parser.settings") as mock_settings:
        mock_settings.GCV_API_KEY = ""
        with pytest.raises(RuntimeError, match="GCV_API_KEY"):
            _call_gcv(VALID_JPEG)


def test_call_gcv_returns_text():
    gcv_response = {
        "responses": [
            {"fullTextAnnotation": {"text": SAMPLE_RAW_TEXT}}
        ]
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = gcv_response
    mock_resp.raise_for_status = MagicMock()

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.httpx.Client") as mock_client_cls:
        mock_settings.GCV_API_KEY = "test-key"
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        result = _call_gcv(VALID_JPEG)

    assert result == SAMPLE_RAW_TEXT


def test_call_gcv_empty_response_returns_empty_string():
    gcv_response = {"responses": [{}]}
    mock_resp = MagicMock()
    mock_resp.json.return_value = gcv_response
    mock_resp.raise_for_status = MagicMock()

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.httpx.Client") as mock_client_cls:
        mock_settings.GCV_API_KEY = "test-key"
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        result = _call_gcv(VALID_JPEG)

    assert result == ""


# ── Haiku tests ──────────────────────────────────────────────────────────────

def test_call_haiku_no_api_key_raises_runtime_error():
    with patch("app.services.receipt_parser.settings") as mock_settings:
        mock_settings.ANTHROPIC_API_KEY = ""
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            _call_haiku(SAMPLE_RAW_TEXT)


def test_call_haiku_returns_parsed_dict():
    import json
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps(SAMPLE_SCAN_RESPONSE))]

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.anthropic.Anthropic") as mock_anthropic:
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_anthropic.return_value.messages.create.return_value = mock_message

        result = _call_haiku(SAMPLE_RAW_TEXT)

    assert result["store"]["name"] == "WINCO FOODS"
    assert len(result["items"]) == 2
    assert result["raw_text"] == SAMPLE_RAW_TEXT


def test_call_haiku_strips_markdown_fences():
    import json
    fenced = f"```json\n{json.dumps(SAMPLE_SCAN_RESPONSE)}\n```"
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=fenced)]

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.anthropic.Anthropic") as mock_anthropic:
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_anthropic.return_value.messages.create.return_value = mock_message

        result = _call_haiku(SAMPLE_RAW_TEXT)

    assert result["store"]["name"] == "WINCO FOODS"


# ── Full flow test ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_receipt_image_full_flow():
    with patch("app.services.receipt_parser._call_gcv", return_value=SAMPLE_RAW_TEXT), \
         patch("app.services.receipt_parser._call_haiku", return_value=SAMPLE_SCAN_RESPONSE):
        result = await parse_receipt_image(VALID_JPEG, "image/jpeg")

    assert result["store"]["name"] == "WINCO FOODS"
    assert result["summary"]["total"] == 10.24
    assert len(result["items"]) == 2


@pytest.mark.asyncio
async def test_parse_receipt_image_empty_ocr_raises_runtime_error():
    with patch("app.services.receipt_parser._call_gcv", return_value="   "):
        with pytest.raises(RuntimeError, match="No text found"):
            await parse_receipt_image(VALID_JPEG, "image/jpeg")
```

- [ ] **Step 5: Run tests to confirm they all fail with ImportError**

```bash
pytest tests/services/test_receipt_parser.py -v 2>&1 | head -30
```

Expected: `ImportError: cannot import name 'parse_receipt_image'` (or similar). This confirms the test file is wired up correctly before the implementation exists.

---

## Task 2: Add `GCV_API_KEY` to Config

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add the new setting**

In `config.py`, add `GCV_API_KEY` after `YOUTUBE_API_KEY`:

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    FIREBASE_PROJECT_ID: str
    FIREBASE_SERVICE_ACCOUNT_KEY: str | None = None
    DATABASE_URL: str
    ANTHROPIC_API_KEY: str | None = None
    YOUTUBE_API_KEY: str | None = None
    GCV_API_KEY: str | None = None
    INSTACART_API_KEY: str | None = None
    INSTACART_SERVICE_URL: str = "http://localhost:3001"
    INSTACART_SERVICE_KEY: str | None = None

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
```

- [ ] **Step 2: Add the key to `.env` (do not commit `.env`)**

Open `.env` and add:
```
GCV_API_KEY=your-google-cloud-vision-api-key
```

- [ ] **Step 3: Commit config change**

```bash
git add config.py
git commit -m "feat: add GCV_API_KEY to settings"
```

---

## Task 3: Implement `app/services/receipt_parser.py`

**Files:**
- Create: `app/services/receipt_parser.py`

- [ ] **Step 1: Create the service file**

```python
import base64
import json
import logging

import anthropic
import httpx

from config import settings

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


def _call_gcv(image_bytes: bytes) -> str:
    """POST image to GCV DOCUMENT_TEXT_DETECTION. Returns raw text string."""
    if not settings.GCV_API_KEY:
        raise RuntimeError("GCV_API_KEY is not configured.")

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    with httpx.Client(timeout=30) as client:
        resp = client.post(
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


def _call_haiku(raw_text: str) -> dict:
    """Parse raw receipt OCR text with Claude Haiku. Returns ScanResponse dict."""
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured.")

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
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

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=_PARSE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rstrip("`").strip()

    parsed = json.loads(raw)
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
        raw_text = _call_gcv(image_bytes)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("GCV call failed: %s", e)
        raise RuntimeError("OCR failed. Please try again.")

    if not raw_text.strip():
        raise RuntimeError("No text found in image.")

    try:
        return _call_haiku(raw_text)
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Haiku parsing failed: %s", e)
        raise RuntimeError("Receipt parsing failed. Please try again.")
```

- [ ] **Step 2: Run the tests — expect most to pass now**

```bash
pytest tests/services/test_receipt_parser.py -v
```

Expected: all tests pass. If any fail, fix the implementation before moving on. Common issues:
- `json.loads` fails on markdown-fenced response → check the fence-stripping logic in `_call_haiku`
- `httpx.Client` context manager mock — the patch path must be `app.services.receipt_parser.httpx.Client`

- [ ] **Step 3: Commit**

```bash
git add app/services/receipt_parser.py tests/services/test_receipt_parser.py tests/__init__.py tests/services/__init__.py
git commit -m "feat: add receipt_parser service (GCV + Haiku)"
```

---

## Task 4: Replace the Stub in `pantry.py`

**Files:**
- Modify: `app/api/routes/pantry.py`

- [ ] **Step 1: Add the import at the top of `pantry.py`**

After the existing imports, add:

```python
from app.services.receipt_parser import parse_receipt_image
```

- [ ] **Step 2: Replace the stub body of `POST /pantry/ocr`**

Find the existing `ocr_receipt` function (currently lines 310–326) and replace its entire body:

```python
@router.post("/ocr")
async def ocr_receipt(
    image: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    content_type = (image.content_type or "image/jpeg").lower()
    image_bytes = await image.read()

    try:
        return await parse_receipt_image(image_bytes, content_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
```

Remove the old stub imports (`import copy`, `from app.data.mock_data import MOCK_PANTRY`) if they are only used by the old stub body.

- [ ] **Step 3: Verify the server starts without errors**

```bash
uvicorn app.main:app --reload
```

Expected: server starts, no import errors.

- [ ] **Step 4: Smoke test with curl (requires a real JPEG and valid keys)**

```bash
curl -X POST http://localhost:8000/pantry/ocr \
  -H "Authorization: Bearer <your-firebase-token>" \
  -F "image=@/path/to/receipt.jpg" \
  | python -m json.tool
```

Expected: JSON response with `store`, `summary`, `items` array. If `GCV_API_KEY` or `ANTHROPIC_API_KEY` is missing, you'll get a 502 with a clear message.

- [ ] **Step 5: Commit**

```bash
git add app/api/routes/pantry.py
git commit -m "feat: wire POST /pantry/ocr to receipt_parser service"
```

---

## Task 5: Final Verification and PR Prep

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Confirm unused stub imports are gone from `pantry.py`**

Search for `mock_data` and `copy` in `pantry.py`:

```bash
grep -n "mock_data\|import copy" app/api/routes/pantry.py
```

Expected: no output (both should be gone from the route file).

- [ ] **Step 3: Confirm `GCV_API_KEY` is not hardcoded anywhere**

```bash
grep -rn "GCV_API_KEY" app/ config.py
```

Expected: only appears in `config.py` definition and `app/services/receipt_parser.py` usage.

- [ ] **Step 4: Push branch**

```bash
git push -u origin feature/receipt-scanning
```
