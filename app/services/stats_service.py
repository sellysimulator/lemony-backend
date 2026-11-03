"""Per-user aggregates, recomputed from `games` (dialect-agnostic, no MySQL upsert).

`best_profit` is the highest `total_profit` of any one game and `avg_profit` the mean:
games of every length and setting are compared side by side, as the profile says.

The stats are a cache of the player's stored games, never the record: a game is saved
and committed first, and its stats are recomputed afterwards in their own transaction
(`refresh_user_stats`), so a stats failure never costs the save. `recompute_user_stats`
rebuilds every figure from all the stored games, so any later recompute repairs a failed
one, and `current_user_stats` (the `/users/me/stats` read) recomputes when the stored
stats miss a game.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.game import Game, UserStats

logger = logging.getLogger(__name__)


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
        # Ties on profit go to the latest game: finished last, then stored last.
        best_game_id = db.execute(
            select(Game.id)
            .where(Game.user_id == user_id, Game.total_profit == best)
            .order_by(Game.finished_at.desc(), Game.id.desc())
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
    stats.avg_profit = (
        (Decimal(str(profit)) / games).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if games
        else None
    )
    stats.best_game_id = best_game_id
    return stats


def refresh_user_stats(user_id: int, public_id: str | None = None) -> bool:
    """Recompute a player's stats in a session of their own and commit them.

    Called once the write it follows (a saved game, a guest claim) is committed. Never
    raises: a failure is logged with the user id and the game's public id (no secrets),
    returns False, and the next read of the stats repairs them (`current_user_stats`).
    """
    from ..db import session as db_session

    db = db_session.SessionLocal()
    try:
        recompute_user_stats(db, user_id)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to recompute player stats (user_id=%s, game=%s); the saved games are "
            "kept and the next read of the stats repairs them.",
            user_id,
            public_id,
        )
        return False
    finally:
        db.close()


def current_user_stats(db: Session, user_id: int) -> UserStats | None:
    """The player's stored stats, recomputed first when they miss a stored game.

    A player's games are only ever added (a save, a guest claim), never removed or moved
    to someone else, so the stored `games_played` differs from the count of their games
    exactly when a game came in without its stats (the recompute after it failed, or an
    older snapshot's recompute committed last). The check is one indexed COUNT; the
    timestamps can't decide it (MySQL `DATETIME` keeps whole seconds, and `updated_at`
    moves only when a value changes). None when there are no stats and no games.

    A failed repair is logged and the stored stats are served as they are; the next read
    tries again.
    """
    stats = db.get(UserStats, user_id)
    stored = db.execute(select(func.count(Game.id)).where(Game.user_id == user_id)).scalar_one()
    if (stats.games_played if stats is not None else 0) == stored:
        return stats
    try:
        stats = recompute_user_stats(db, user_id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to repair player stats (user_id=%s).", user_id)
        return db.get(UserStats, user_id)
    return stats
