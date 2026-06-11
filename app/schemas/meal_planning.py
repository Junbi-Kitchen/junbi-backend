"""Request schemas for the meal-planning routes (camelCase via CamelModel)."""

from typing import Literal

from pydantic import Field

from app.schemas.base import CamelModel


class GenerateWeekRequest(CamelModel):
    slots: int = Field(default=5, ge=1, le=7)
    busy_nights: int = Field(default=2, ge=0, le=7)
    budget_cap: float | None = Field(default=None, ge=0)
    notes: str | None = None  # free-text week context ("guests Friday", cravings)


class ReactRequest(CamelModel):
    action: Literal[
        "accept",
        "swap",
        "never_show",
        "budget_trade_take",
        "budget_trade_decline",
    ]
    recipe_id: str | None = None       # swap: the item being replaced; never_show: target
    replacement_id: str | None = None  # swap: must come from the item's alternates


class TonightAckRequest(CamelModel):
    recipe_id: str
    action: Literal["opened", "made_it", "quick_swap"]
    replacement_id: str | None = None  # quick_swap only


class SwipeItem(CamelModel):
    recipe_id: str
    liked: bool


class SwipesRequest(CamelModel):
    swipes: list[SwipeItem]
