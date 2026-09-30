"""Per-user aggregates, recomputed from `games` (dialect-agnostic, no MySQL upsert)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.game import Game, UserStats


def recompute_user_stats(db: Session, user_id: int) -> UserStats:
    row = db.execute(
        select(
            func.count(Game.id),
            func.coalesce(func.sum(Game.days_played), 0),
            func.coalesce(func.sum(Game.total_buyers), 0),
            func.coalesce(func.sum(Game.total_visitors), 0),
            func.coalesce(func.sum(Game.total_profit), 0),
            func.max(Game.total_profit),
        ).where(Game.user_id == user_id)
    ).one()
    games, days, cups, visitors, profit, best = row
    best_game_id = None
    if best is not None:
        best_game_id = db.execute(
            select(Game.id)
            .where(Game.user_id == user_id, Game.total_profit == best)
            .order_by(Game.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    stats = db.get(UserStats, user_id)
    if stats is None:
        stats = UserStats(user_id=user_id)
        db.add(stats)
    stats.games_played = int(games)
    stats.days_played = int(days)
    stats.total_cups_sold = int(cups)
    stats.total_visitors = int(visitors)
    stats.total_profit = Decimal(str(profit))
    stats.best_profit = Decimal(str(best)) if best is not None else None
    stats.avg_profit = (Decimal(str(profit)) / games).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if games else None
    stats.best_game_id = best_game_id
    return stats
