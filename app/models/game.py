"""Finished games, written once when a game ends (idempotent on `public_id`)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin

MONEY = Numeric(12, 2)


class Game(Base, TimestampMixin):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    guest_identity: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rng_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    num_days: Mapped[int] = mapped_column(Integer, nullable=False)
    days_played: Mapped[int] = mapped_column(Integer, nullable=False)
    starting_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    final_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_revenue: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_spend: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_visitors: Mapped[int] = mapped_column(Integer, nullable=False)
    total_buyers: Mapped[int] = mapped_column(Integer, nullable=False)
    total_sold_out: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class GameConfigRow(Base):
    __tablename__ = "game_configs"

    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class GameDay(Base):
    __tablename__ = "game_days"
    __table_args__ = (UniqueConstraint("game_id", "day", name="uq_game_days_game_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    weather: Mapped[str] = mapped_column(String(16), nullable=False)
    temperature: Mapped[Decimal] = mapped_column(Numeric(5, 1), nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    recipe_ice: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_sugar: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_lemons: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_per_cup: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    bought_ice: Mapped[int] = mapped_column(Integer, nullable=False)
    bought_sugar: Mapped[int] = mapped_column(Integer, nullable=False)
    bought_lemons: Mapped[int] = mapped_column(Integer, nullable=False)
    bought_cups: Mapped[int] = mapped_column(Integer, nullable=False)
    spend: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    revenue: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False)
    buyers: Mapped[int] = mapped_column(Integer, nullable=False)
    sold_out: Mapped[int] = mapped_column(Integer, nullable=False)
    refused: Mapped[int] = mapped_column(Integer, nullable=False)
    perished_ice: Mapped[int] = mapped_column(Integer, nullable=False)
    perished_sugar: Mapped[int] = mapped_column(Integer, nullable=False)
    perished_lemons: Mapped[int] = mapped_column(Integer, nullable=False)
    perished_cups: Mapped[int] = mapped_column(Integer, nullable=False)
    perished_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash_end: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    refusal_reasons: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)


class GameDayType(Base):
    __tablename__ = "game_day_types"
    __table_args__ = (UniqueConstraint("game_id", "day", "person_type", name="uq_game_day_types"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    person_type: Mapped[str] = mapped_column(String(16), nullable=False)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False)
    buyers: Mapped[int] = mapped_column(Integer, nullable=False)
    sold_out: Mapped[int] = mapped_column(Integer, nullable=False)


class GameDayHour(Base):
    __tablename__ = "game_day_hours"
    __table_args__ = (UniqueConstraint("game_id", "day", "hour", name="uq_game_day_hours"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[int] = mapped_column(Integer, nullable=False)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    visitors: Mapped[int] = mapped_column(Integer, nullable=False)
    buyers: Mapped[int] = mapped_column(Integer, nullable=False)
    sold_out: Mapped[int] = mapped_column(Integer, nullable=False)


class UserStats(Base, TimestampMixin):
    __tablename__ = "user_stats"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    games_played: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_played: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cups_sold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_visitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    best_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    avg_profit: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    best_game_id: Mapped[int | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), nullable=True
    )
