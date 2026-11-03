"""Pydantic models for the user and profile routes."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserUpsertRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    photo_url: str | None = Field(default=None, max_length=500)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    display_name: str | None
    email: str | None
    photo_url: str | None
    created_at: datetime


class UserStatsResponse(BaseModel):
    games_played: int = 0
    days_played: int = 0
    total_cups_sold: int = 0
    total_visitors: int = 0
    # All cups sold over all visitors; null with no visitors.
    conversion: float | None = None
    total_profit: float = 0.0
    best_profit: float | None = None
    avg_profit: float | None = None
    best_game_id: str | None = None


class GameListItem(BaseModel):
    public_id: str
    num_days: int
    days_played: int
    starting_cash: float
    final_cash: float
    total_profit: float
    total_revenue: float
    total_spend: float
    total_visitors: int
    total_buyers: int
    total_sold_out: int
    final_popularity: float | None = None
    # Cups sold over visitors; null with no visitors.
    conversion: float | None = None
    started_at: str
    finished_at: str


class GameListResponse(BaseModel):
    items: list[GameListItem]
    total: int
    page: int
    page_size: int


class ClaimRequest(BaseModel):
    guest_identity: str


class ClaimResponse(BaseModel):
    claimed: int


class ConfigDefaultsResponse(BaseModel):
    config: dict[str, Any]
    weather_types: list[str]
    person_types: list[str]
    ingredient_names: list[str]
    # Every bound the config and plan screens show or enforce: `core/limits.public_limits()`.
    limits: dict[str, dict[str, float] | float]


class ConfigValidateResponse(BaseModel):
    valid: bool
    message: str | None = None


class ConfigPreviewResponse(BaseModel):
    """`POST /config/preview`: the verdict and every figure the config screen and help show.

    `ingredients` holds each ingredient's own figures (`core/config_preview.ingredient_preview`),
    or null when that ingredient's settings are invalid, so the packs and spoilage keep
    their figures while another part of the config is being fixed. `help` (the figures of
    `config_preview.config_help`) needs the whole config to be valid, else it is null.
    """

    valid: bool
    message: str | None = None
    ingredients: dict[str, dict[str, Any] | None]
    help: dict[str, Any] | None = None


class CupCostRequest(BaseModel):
    """Day 1 of a game on `config`, with these packs bought and this recipe."""

    config: dict[str, Any]
    purchases: dict[str, dict[str, int]] = Field(default_factory=dict)
    recipe: dict[str, int]


class CupCostResponse(BaseModel):
    spend: float
    cups_possible: int
    # The plan screen's cost per cup; null when not a single cup can be made.
    cost_per_cup: float | None
