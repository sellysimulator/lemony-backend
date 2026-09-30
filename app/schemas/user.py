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
    max_num_days: int


class ConfigValidateResponse(BaseModel):
    valid: bool
    message: str | None = None
