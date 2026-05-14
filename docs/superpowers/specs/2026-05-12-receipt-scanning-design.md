# Receipt Scanning — Backend Design Spec

**Date:** 2026-05-12  
**Status:** Approved

---

## Problem

Receipt scanning is currently mocked end-to-end: the frontend `useReceiptScanner` hook returns random pantry items after a fake delay, and the backend `POST /pantry/ocr` stub ignores the uploaded image and returns mock data. The Google Cloud Vision API key lives in the frontend bundle (`EXPO_PUBLIC_GCV_API_KEY`), which is a security risk.

---

## Decision

Move all OCR and parsing logic to the backend. The frontend captures the image and sends it; the backend handles everything else.

---

## Architecture

### Data Flow

```
Frontend
  camera → base64 image
  POST /pantry/ocr  (multipart/form-data: image file)

Backend: pantry.py  →  POST /pantry/ocr
  validate image (type, size)
  call receipt_parser.parse_receipt_image(bytes, content_type)

Backend: app/services/receipt_parser.py
  1. _call_gcv(image_bytes)         — HTTP POST to GCV DOCUMENT_TEXT_DETECTION
                                       returns raw_text: str
  2. _call_haiku(raw_text)          — Claude Haiku parses raw text → ScanResponse dict

Return ScanResponse JSON to frontend
```

### Why GCV + Haiku

- GCV handles the OCR step (raw text extraction from image). It's accurate and cheap.
- Claude Haiku handles the parsing step (raw text → structured items). This replaces the ~500-line regex parser in `receiptParser.ts`. Haiku handles receipt format variance gracefully and is already proven in this codebase via `recipe_parser.py`.
- Cost: ~$0.001–0.002 per scan billed to the app owner's Anthropic account.

---

## Components

### `app/services/receipt_parser.py` (new file)

Follows the exact pattern of `app/services/recipe_parser.py`.

**Public API:**
```python
async def parse_receipt_image(image_bytes: bytes, content_type: str) -> dict:
    """Validates input, calls GCV, calls Haiku, returns ScanResponse dict.
    Raises ValueError on bad input. Raises RuntimeError on API failure."""
```

**Internal functions:**
```python
def _call_gcv(image_bytes: bytes) -> str:
    """HTTP POST to GCV REST API (DOCUMENT_TEXT_DETECTION). Returns raw text string."""

def _call_haiku(raw_text: str) -> dict:
    """Sends raw text to Claude Haiku. Returns parsed ScanResponse dict."""
```

**Validation (in `parse_receipt_image`):**
- Supported types: `image/jpeg`, `image/png`, `image/webp`
- Max size: 5 MB
- Missing `GCV_API_KEY` or `ANTHROPIC_API_KEY` → `RuntimeError`

**GCV call:**
- Endpoint: `https://vision.googleapis.com/v1/images:annotate?key={GCV_API_KEY}`
- Feature: `DOCUMENT_TEXT_DETECTION`
- Returns `responses[0].fullTextAnnotation.text`
- Uses `httpx.AsyncClient` (already in requirements)

**Haiku prompt:** instructs the model to parse the raw OCR text and return a JSON object matching the `ScanResponse` schema (store info, summary with subtotal/tax/total, and items array). Item categories must be one of: `produce`, `proteins`, `dairy`, `grains`, `frozen`, `pantry`, `condiments`, `spices`, `bakery`, `beverages`.

**Response shape (ScanResponse):**
```python
{
    "scanned_at": "ISO8601 string",
    "store": {"name": str|None, "address": str|None, "phone": str|None},
    "summary": {"item_count": int, "subtotal": float|None, "tax": float|None, "total": float|None},
    "items": [
        {
            "name": str,
            "brand": str|None,
            "variant": str|None,
            "quantity": float|None,
            "unit": str|None,
            "unit_price": float|None,
            "total_price": float|None,
            "category": str,
            "subcategory": str|None,
            "on_sale": bool,
            "confidence": "high"|"medium"|"low",
            "parsing_notes": str|None,
            "raw_line": str,
        }
    ],
    "unparsed_lines": [str],
    "raw_text": str,
}
```

### `app/api/routes/pantry.py` — `POST /pantry/ocr` (replace stub)

Current stub body (random mock data) is replaced with:
```python
image_bytes = await image.read()
result = await parse_receipt_image(image_bytes, image.content_type or "image/jpeg")
return result
```

Route signature is unchanged (`UploadFile`, auth required). Error handling: catch `ValueError` → 400, `RuntimeError` → 502.

### `config.py` — Add `GCV_API_KEY`

Add `GCV_API_KEY: str = ""` to the settings class. The route/service checks for its presence and raises `RuntimeError` with a clear message if missing.

---

## Error Handling

| Condition | Exception | HTTP |
|-----------|-----------|------|
| Wrong image type | `ValueError` | 400 |
| Image > 5 MB | `ValueError` | 400 |
| `GCV_API_KEY` not set | `RuntimeError` | 502 |
| `ANTHROPIC_API_KEY` not set | `RuntimeError` | 502 |
| GCV HTTP error | `RuntimeError` | 502 |
| Haiku returns unparseable JSON | `RuntimeError` | 502 |

---

## What Does NOT Change

- Route path stays `POST /pantry/ocr`
- Auth requirement unchanged (`get_current_user`)
- `POST /pantry/bulk` — frontend still calls this to save confirmed items
- `POST /pantry/scan` — pantry photo scanning, unrelated, untouched
- All other routes untouched

---

## Out of Scope

- Saving items to DB automatically (user must confirm → call `/pantry/bulk`)
- Receipt history / audit log
- Per-user billing or rate limiting
