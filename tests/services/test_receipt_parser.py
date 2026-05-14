import json
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


def test_call_gcv_http_error_raises():
    import httpx
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "403", request=MagicMock(), response=MagicMock()
    )

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.httpx.Client") as mock_client_cls:
        mock_settings.GCV_API_KEY = "test-key"
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            _call_gcv(VALID_JPEG)


# ── Haiku tests ──────────────────────────────────────────────────────────────

def test_call_haiku_no_api_key_raises_runtime_error():
    with patch("app.services.receipt_parser.settings") as mock_settings:
        mock_settings.ANTHROPIC_API_KEY = ""
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            _call_haiku(SAMPLE_RAW_TEXT)


def test_call_haiku_json_parse_error_raises_runtime_error():
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="not valid json {{{{")]

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.anthropic.Anthropic") as mock_anthropic:
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_anthropic.return_value.messages.create.return_value = mock_message

        with pytest.raises(RuntimeError, match="Receipt parsing failed"):
            _call_haiku(SAMPLE_RAW_TEXT)


def test_call_haiku_returns_parsed_dict():
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
    fenced = f"```json\n{json.dumps(SAMPLE_SCAN_RESPONSE)}\n```"
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=fenced)]

    with patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.anthropic.Anthropic") as mock_anthropic:
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_anthropic.return_value.messages.create.return_value = mock_message

        result = _call_haiku(SAMPLE_RAW_TEXT)

    assert result["store"]["name"] == "WINCO FOODS"
    assert len(result["items"]) == 2
    assert result["summary"]["total"] == 10.24


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


@pytest.mark.asyncio
async def test_parse_receipt_image_haiku_json_error_raises_runtime_error():
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="not valid json {{{{")]

    with patch("app.services.receipt_parser._call_gcv", return_value=SAMPLE_RAW_TEXT), \
         patch("app.services.receipt_parser.settings") as mock_settings, \
         patch("app.services.receipt_parser.anthropic.Anthropic") as mock_anthropic:
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_anthropic.return_value.messages.create.return_value = mock_message

        with pytest.raises(RuntimeError, match="Receipt parsing failed"):
            await parse_receipt_image(VALID_JPEG, "image/jpeg")
