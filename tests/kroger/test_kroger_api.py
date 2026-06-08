"""
tests/test_kroger_agent.py
───────────────────────────
Pytest suite for the Kroger ADK agent.

All tests run in STUB MODE by default — no real Kroger or Google credentials
needed. The agent's stub fallback returns deterministic fake data whenever
KROGER_CLIENT_ID or GOOGLE_API_KEY are absent.

Run:
    pytest tests/test_kroger_agent.py -v

Run with live API (requires credentials in .env):
    pytest tests/test_kroger_agent.py -v -m live

Test groups
───────────
  Result shape tests   — every field present and correct type (stub + live)
  Cart preview tests   — best-pick logic, price calculation, unfound items
  Edge case tests      — empty list, missing zip, single item
  Runner tests         — _parse_agent_response, _stub_result internals
"""

import json
import os
import sys

import pytest
import pytest_asyncio

# ── Make sure project root is on path ────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app.agents.kroger_client.runner import (
    search_kroger,
    _stub_result,
    _parse_agent_response,
    KrogerResult,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_ITEMS = ["eggs", "whole milk", "olive oil", "garlic", "pasta"]
SAMPLE_ZIP = "10001"
SAMPLE_QUANTITIES = {"eggs": 1, "whole milk": 1, "olive oil": 1, "garlic": 3, "pasta": 2}


@pytest.fixture
def stub_result() -> KrogerResult:
    """A stub result for the default sample items."""
    return _stub_result(SAMPLE_ITEMS, SAMPLE_ZIP)


@pytest.fixture
def sample_agent_json() -> str:
    """
    Valid JSON string in the exact shape the Kroger ADK agent returns.
    Used to test _parse_agent_response() without running the real agent.
    """
    return json.dumps({
        "store": {
            "location_id": "01400943",
            "name": "Kroger #123",
            "address": "123 Main St, New York, NY 10001",
        },
        "results": {
            "eggs": [
                {
                    "product_id": "0001111060903",
                    "name": "Kroger Large Grade A Eggs",
                    "price": 3.49,
                    "unit": "12 ct",
                    "image_url": "https://example.com/eggs.jpg",
                    "aisle": "Dairy",
                },
                {
                    "product_id": "0001111060904",
                    "name": "Simple Truth Organic Eggs",
                    "price": 5.99,
                    "unit": "12 ct",
                    "image_url": None,
                    "aisle": "Dairy",
                },
            ],
            "whole milk": [
                {
                    "product_id": "0001111085506",
                    "name": "Kroger Whole Milk",
                    "price": 4.29,
                    "unit": "1 gal",
                    "image_url": None,
                    "aisle": "Dairy",
                }
            ],
            "olive oil": [],  # not found
        },
        "unfound": ["olive oil"],
        "cart_preview": [
            {
                "name": "eggs",
                "product_id": "0001111060903",
                "product_name": "Kroger Large Grade A Eggs",
                "price": 3.49,
                "quantity": 1,
                "unit": "12 ct",
                "estimated_price": 3.49,
                "aisle": "Dairy",
            },
            {
                "name": "whole milk",
                "product_id": "0001111085506",
                "product_name": "Kroger Whole Milk",
                "price": 4.29,
                "quantity": 1,
                "unit": "1 gal",
                "estimated_price": 4.29,
                "aisle": "Dairy",
            },
        ],
        "estimated_total": 7.78,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Result shape tests (stub mode)
# ─────────────────────────────────────────────────────────────────────────────

class TestResultShape:
    """
    Validate that search_kroger() always returns the correct shape,
    regardless of whether real credentials are available.
    """

    @pytest.mark.asyncio
    async def test_returns_dict(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_top_level_keys_present(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        required_keys = {"store", "results", "unfound", "cart_preview", "estimated_total", "error"}
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )

    @pytest.mark.asyncio
    async def test_results_is_dict_keyed_by_item_name(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        assert isinstance(result["results"], dict)
        # Every requested item should appear as a key
        for item in SAMPLE_ITEMS:
            assert item in result["results"], f"Item '{item}' missing from results"

    @pytest.mark.asyncio
    async def test_cart_preview_is_list(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        assert isinstance(result["cart_preview"], list)

    @pytest.mark.asyncio
    async def test_unfound_is_list(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        assert isinstance(result["unfound"], list)

    @pytest.mark.asyncio
    async def test_estimated_total_is_numeric(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        assert isinstance(result["estimated_total"], (int, float))
        assert result["estimated_total"] >= 0

    @pytest.mark.asyncio
    async def test_store_has_required_fields(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        store = result["store"]
        assert store is not None
        assert "location_id" in store
        assert "name" in store
        assert "address" in store

    @pytest.mark.asyncio
    async def test_cart_preview_items_have_required_fields(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        required = {"name", "product_id", "product_name", "price", "quantity", "unit", "estimated_price", "aisle"}
        for item in result["cart_preview"]:
            missing = required - item.keys()
            assert not missing, f"cart_preview item missing fields {missing}: {item}"

    @pytest.mark.asyncio
    async def test_error_field_is_none_on_success(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        # In stub mode, error should be None
        assert result["error"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Cart preview logic tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCartPreview:
    """Verify the cart_preview content is sane."""

    @pytest.mark.asyncio
    async def test_cart_preview_count_matches_found_items(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        expected_found = len(SAMPLE_ITEMS) - len(result["unfound"])
        assert len(result["cart_preview"]) == expected_found

    @pytest.mark.asyncio
    async def test_estimated_total_matches_cart_preview_sum(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        preview_sum = sum(
            item["estimated_price"] for item in result["cart_preview"]
            if item.get("estimated_price") is not None
        )
        assert abs(result["estimated_total"] - preview_sum) < 0.01, (
            f"estimated_total {result['estimated_total']} doesn't match "
            f"cart_preview sum {preview_sum}"
        )

    @pytest.mark.asyncio
    async def test_cart_preview_item_names_are_from_input(self):
        result = await search_kroger(items=SAMPLE_ITEMS, zip_code=SAMPLE_ZIP)
        for item in result["cart_preview"]:
            assert item["name"] in SAMPLE_ITEMS, (
                f"cart_preview contains unexpected item: {item['name']}"
            )

    @pytest.mark.asyncio
    async def test_quantities_applied_correctly(self):
        """Items with quantity > 1 should have estimated_price = price * quantity."""
        result = await search_kroger(
            items=["pasta"],
            zip_code=SAMPLE_ZIP,
            quantities={"pasta": 3},
        )
        cart_items = result["cart_preview"]
        if cart_items:
            item = cart_items[0]
            if item.get("price") and item.get("estimated_price"):
                expected = round(item["price"] * item["quantity"], 2)
                assert abs(item["estimated_price"] - expected) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Edge case tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_empty_items_list(self):
        """Empty item list should return an empty result without crashing."""
        result = await search_kroger(items=[], zip_code=SAMPLE_ZIP)
        assert result["results"] == {}
        assert result["cart_preview"] == []
        assert result["unfound"] == []
        assert result["estimated_total"] == 0.0

    @pytest.mark.asyncio
    async def test_single_item(self):
        """Single item should work the same as a full list."""
        result = await search_kroger(items=["eggs"], zip_code=SAMPLE_ZIP)
        assert "eggs" in result["results"]
        assert result["estimated_total"] >= 0

    @pytest.mark.asyncio
    async def test_delivery_preference_pickup(self):
        """delivery_preference param should be accepted without error."""
        result = await search_kroger(
            items=SAMPLE_ITEMS,
            zip_code=SAMPLE_ZIP,
            delivery_preference="pickup",
        )
        assert "store" in result

    @pytest.mark.asyncio
    async def test_delivery_preference_delivery(self):
        result = await search_kroger(
            items=SAMPLE_ITEMS,
            zip_code=SAMPLE_ZIP,
            delivery_preference="delivery",
        )
        assert "store" in result

    @pytest.mark.asyncio
    async def test_no_quantities_defaults_to_one(self):
        """Omitting quantities should default to 1 per item without error."""
        result = await search_kroger(
            items=["eggs", "milk"],
            zip_code=SAMPLE_ZIP,
            quantities=None,
        )
        for item in result["cart_preview"]:
            assert item.get("quantity") is not None

    @pytest.mark.asyncio
    async def test_different_zip_codes(self):
        """Agent should handle any US zip code format."""
        for zip_code in ["90210", "10001", "30301", "77001"]:
            result = await search_kroger(items=["eggs"], zip_code=zip_code)
            assert isinstance(result, dict), f"Failed for zip {zip_code}"


# ─────────────────────────────────────────────────────────────────────────────
# Internal unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestStubResult:
    """Unit tests for _stub_result() — the fallback data generator."""

    def test_returns_all_items(self, stub_result):
        for item in SAMPLE_ITEMS:
            assert item in stub_result["results"]

    def test_cart_preview_length_matches_items(self, stub_result):
        # Stub always finds all items (no unfound in stub mode)
        assert len(stub_result["cart_preview"]) == len(SAMPLE_ITEMS)

    def test_unfound_is_empty_in_stub(self, stub_result):
        assert stub_result["unfound"] == []

    def test_estimated_total_is_positive(self, stub_result):
        assert stub_result["estimated_total"] > 0

    def test_store_name_indicates_stub(self, stub_result):
        assert "stub" in stub_result["store"]["name"].lower()

    def test_product_ids_are_stub_prefixed(self, stub_result):
        for item in stub_result["cart_preview"]:
            assert item["product_id"].startswith("stub-kroger-"), (
                f"Expected stub product_id, got: {item['product_id']}"
            )

    def test_prices_are_deterministic(self):
        """Same item name should always produce the same stub price."""
        r1 = _stub_result(["eggs"], "10001")
        r2 = _stub_result(["eggs"], "90210")
        assert r1["cart_preview"][0]["price"] == r2["cart_preview"][0]["price"]


class TestParseAgentResponse:
    """Unit tests for _parse_agent_response() — the JSON parser."""

    def test_parses_clean_json(self, sample_agent_json):
        result = _parse_agent_response(sample_agent_json)
        assert result["store"]["location_id"] == "01400943"
        assert "eggs" in result["results"]
        assert len(result["cart_preview"]) == 2
        assert result["estimated_total"] == 7.78

    def test_strips_markdown_fences(self, sample_agent_json):
        """Model sometimes wraps JSON in ```json ... ``` fences."""
        wrapped = f"```json\n{sample_agent_json}\n```"
        result = _parse_agent_response(wrapped)
        assert result["store"]["name"] == "Kroger #123"

    def test_strips_plain_code_fence(self, sample_agent_json):
        """Also handles ``` without language specifier."""
        wrapped = f"```\n{sample_agent_json}\n```"
        result = _parse_agent_response(wrapped)
        assert result["estimated_total"] == 7.78

    def test_unfound_items_present(self, sample_agent_json):
        result = _parse_agent_response(sample_agent_json)
        assert "olive oil" in result["unfound"]

    def test_empty_results_for_unfound_item(self, sample_agent_json):
        result = _parse_agent_response(sample_agent_json)
        assert result["results"].get("olive oil") == []

    def test_error_field_is_none(self, sample_agent_json):
        result = _parse_agent_response(sample_agent_json)
        assert result["error"] is None

    def test_invalid_json_raises(self):
        with pytest.raises((json.JSONDecodeError, Exception)):
            _parse_agent_response("this is not json")


# ─────────────────────────────────────────────────────────────────────────────
# Live API tests (skipped unless credentials present)
# ─────────────────────────────────────────────────────────────────────────────

_has_credentials = (
    bool(os.environ.get("KROGER_CLIENT_ID"))
    and bool(os.environ.get("KROGER_CLIENT_SECRET"))
    and bool(os.environ.get("GOOGLE_API_KEY"))
)


@pytest.mark.live
@pytest.mark.skipif(not _has_credentials, reason="Kroger + Google credentials not in env")
class TestLiveAgent:
    """
    Tests that run the real Kroger ADK agent against the live API.
    Skipped automatically unless all credentials are present.

    Run explicitly:
        pytest tests/test_kroger_agent.py -v -m live
    """

    @pytest.mark.asyncio
    async def test_live_returns_real_store(self):
        result = await search_kroger(items=["eggs"], zip_code="10001")
        # Real results should not have "stub" in the store name
        assert "stub" not in (result["store"] or {}).get("name", "").lower()
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_live_finds_common_items(self):
        """Common items like eggs and milk should always be found at Kroger."""
        result = await search_kroger(
            items=["eggs", "milk"],
            zip_code="10001",
        )
        # Both items should appear in results (may still be in unfound if
        # no Kroger in that zip, but results key should exist)
        assert "eggs" in result["results"]
        assert "milk" in result["results"]

    @pytest.mark.asyncio
    async def test_live_product_ids_are_real(self):
        """Live product IDs should be numeric UPCs, not stub- prefixed."""
        result = await search_kroger(items=["eggs"], zip_code="10001")
        for item in result["cart_preview"]:
            if item.get("product_id"):
                assert not item["product_id"].startswith("stub-"), (
                    f"Got stub product_id in live mode: {item['product_id']}"
                )
