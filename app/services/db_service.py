"""End-of-game persistence and reading finished games back.

`persist_finished_game` runs once when a game ends: one transaction, idempotent
on `public_id` (the live game id), one INSERT per table (the day, type and hour
rows each go in one executemany). It is synchronous — call it through
`anyio.to_thread.run_sync` from async code so the event loop never blocks on
MySQL. It never raises: on failure it logs and returns None, and the live
document keeps `persisted: false`.

Every field of a day record is stored, so `load_game_detail` rebuilds the live
summary exactly through the same builder (`engine.summarize`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Table, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.money import round_cents

from ..core.defaults import INGREDIENT_NAMES, PERSON_TYPES
from ..core.engine import summarize
from ..models.game import Game, GameConfigRow, GameDay, GameDayHour, GameDayType
from .stats_service import recompute_user_stats
from .user_service import UserService

logger = logging.getLogger(__name__)
_users = UserService()

# Core statements on the mapped tables, not the ORM unit of work: the ORM inserts
# autoincrement rows one statement at a time on MySQL. None of these tables has
# created_at/updated_at.
_GAME_DAYS = cast(Table, GameDay.__table__)
_GAME_DAY_TYPES = cast(Table, GameDayType.__table__)
_GAME_DAY_HOURS = cast(Table, GameDayHour.__table__)


def _money(value: float) -> Decimal:
    return Decimal(str(round_cents(float(value))))


def _share(value: float | None) -> Decimal | None:
    return Decimal(str(round(float(value), 4))) if value is not None else None


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def is_guest(identity: str) -> bool:
    return identity.startswith("guest_")


def _day_row(game_id: int, d: dict[str, Any]) -> dict[str, Any]:
    inventory_end = d.get("inventory_end") or {}
    return {
        "game_id": game_id,
        "day": d["day"],
        "weather": d["weather"],
        "temperature": Decimal(str(d["temperature"])),
        "price": _money(d["price"]),
        "recipe_ice": d["recipe"]["ice"],
        "recipe_sugar": d["recipe"]["sugar"],
        "recipe_lemons": d["recipe"]["lemons"],
        "cost_per_cup": None if d["cost_per_cup"] is None else _money(d["cost_per_cup"]),
        "bought_ice": d["purchased"]["ice"],
        "bought_sugar": d["purchased"]["sugar"],
        "bought_lemons": d["purchased"]["lemons"],
        "bought_cups": d["purchased"]["cups"],
        "spend": _money(d["spend"]),
        "revenue": _money(d["revenue"]),
        "profit": _money(d["profit"]),
        "visitors": d["visitors"],
        "buyers": d["buyers"],
        "sold_out": d["sold_out"],
        "refused": d["refused"],
        "popularity": _share(d.get("popularity")),
        "perished_ice": d["perished"]["ice"],
        "perished_sugar": d["perished"]["sugar"],
        "perished_lemons": d["perished"]["lemons"],
        "perished_cups": d["perished"]["cups"],
        "perished_value": _money(d["perished_value"]),
        "cash_end": _money(d["cash_end"]),
        **{f"inventory_end_{name}": inventory_end.get(name) for name in INGREDIENT_NAMES},
        "refusal_reasons": d["refusal_reasons"],
    }


def persist_game(db: Session, document: dict[str, Any], summary: dict[str, Any]) -> Game:
    public_id = document["game_id"]
    existing = db.execute(select(Game).where(Game.public_id == public_id)).scalar_one_or_none()
    if existing is not None:
        return existing

    identity = document["identity"]
    user_id = None
    guest_identity = None
    if is_guest(identity):
        guest_identity = identity
    else:
        user_id = _users.ensure_user(db, identity).id

    game = Game(
        public_id=public_id,
        user_id=user_id,
        guest_identity=guest_identity,
        rng_seed=int(document["engine"]["seed"]),
        num_days=summary["num_days"],
        days_played=summary["days_played"],
        starting_cash=_money(summary["starting_cash"]),
        final_cash=_money(summary["final_cash"]),
        total_profit=_money(summary["total_profit"]),
        total_revenue=_money(summary["total_revenue"]),
        total_spend=_money(summary["total_spend"]),
        total_visitors=summary["total_visitors"],
        total_buyers=summary["total_buyers"],
        total_sold_out=summary["total_sold_out"],
        final_popularity=_share(summary.get("final_popularity")),
        started_at=_dt(document["started_at"]),
        finished_at=_dt(document["finished_at"]),
    )
    db.add(game)
    db.flush()
    db.add(GameConfigRow(game_id=game.id, config=document["engine"]["config"]))
    days = summary["days"]
    if days:
        db.execute(insert(_GAME_DAYS), [_day_row(game.id, d) for d in days])
        db.execute(
            insert(_GAME_DAY_TYPES),
            [
                {"game_id": game.id, "day": d["day"], "person_type": kind, **d["by_type"][kind]}
                for d in days
                for kind in PERSON_TYPES
            ],
        )
        hours = [{"game_id": game.id, "day": d["day"], **h} for d in days for h in d["by_hour"]]
        if hours:
            db.execute(insert(_GAME_DAY_HOURS), hours)
    db.flush()
    if user_id is not None:
        recompute_user_stats(db, user_id)
    return game


def persist_finished_game(document: dict[str, Any], summary: dict[str, Any]) -> str | None:
    from ..db import session as db_session

    db = db_session.SessionLocal()
    try:
        game = persist_game(db, document, summary)
        db.commit()
        return game.public_id
    except IntegrityError:
        # Another writer stored the same game first: that copy is the result.
        db.rollback()
        try:
            return db.execute(
                select(Game.public_id).where(Game.public_id == document["game_id"])
            ).scalar_one_or_none()
        except Exception:
            logger.exception("Failed to persist finished game.")
            return None
    except Exception:
        db.rollback()
        logger.exception("Failed to persist finished game.")
        return None
    finally:
        db.close()


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def game_row_summary(game: Game) -> dict[str, Any]:
    return {
        "public_id": game.public_id,
        "num_days": game.num_days,
        "days_played": game.days_played,
        "starting_cash": _f(game.starting_cash),
        "final_cash": _f(game.final_cash),
        "total_profit": _f(game.total_profit),
        "total_revenue": _f(game.total_revenue),
        "total_spend": _f(game.total_spend),
        "total_visitors": game.total_visitors,
        "total_buyers": game.total_buyers,
        "total_sold_out": game.total_sold_out,
        "final_popularity": _f(game.final_popularity),
        "started_at": game.started_at.isoformat(),
        "finished_at": game.finished_at.isoformat(),
    }


def load_game_detail(db: Session, game: Game) -> dict[str, Any]:
    """A finished game in the same shape as the live engine's `summary()`."""
    days_rows = db.execute(
        select(GameDay).where(GameDay.game_id == game.id).order_by(GameDay.day)
    ).scalars()
    types = db.execute(select(GameDayType).where(GameDayType.game_id == game.id)).scalars()
    hours = db.execute(
        select(GameDayHour).where(GameDayHour.game_id == game.id).order_by(GameDayHour.hour)
    ).scalars()
    by_type: dict[int, dict[str, Any]] = {}
    for t in types:
        by_type.setdefault(t.day, {})[t.person_type] = {
            "visitors": t.visitors,
            "buyers": t.buyers,
            "sold_out": t.sold_out,
        }
    by_hour: dict[int, list[dict[str, Any]]] = {}
    for h in hours:
        by_hour.setdefault(h.day, []).append(
            {"hour": h.hour, "visitors": h.visitors, "buyers": h.buyers, "sold_out": h.sold_out}
        )
    days: list[dict[str, Any]] = []
    for d in days_rows:
        perished = {name: getattr(d, f"perished_{name}") for name in INGREDIENT_NAMES}
        day: dict[str, Any] = {
            "day": d.day,
            "weather": d.weather,
            "temperature": float(d.temperature),
            "price": float(d.price),
            "recipe": {"ice": d.recipe_ice, "sugar": d.recipe_sugar, "lemons": d.recipe_lemons},
            "cost_per_cup": _f(d.cost_per_cup),
            "purchased": {name: getattr(d, f"bought_{name}") for name in INGREDIENT_NAMES},
            "spend": float(d.spend),
            "revenue": float(d.revenue),
            "profit": float(d.profit),
            "visitors": d.visitors,
            "buyers": d.buyers,
            "sold_out": d.sold_out,
            "refused": d.refused,
            "conversion": round(d.buyers / d.visitors, 4) if d.visitors else 0.0,
            "popularity": _f(d.popularity),
            "perished": perished,
            "perished_value": float(d.perished_value),
            "cash_end": float(d.cash_end),
            "by_type": by_type.get(d.day, {}),
            "by_hour": by_hour.get(d.day, []),
            "refusal_reasons": d.refusal_reasons,
        }
        inventory_end = {name: getattr(d, f"inventory_end_{name}") for name in INGREDIENT_NAMES}
        if None not in inventory_end.values():  # absent on games stored before it was kept
            day["inventory_end"] = inventory_end
        days.append(day)
    detail = summarize(
        num_days=game.num_days,
        starting_cash=float(game.starting_cash),
        final_cash=float(game.final_cash),
        final_popularity=_f(game.final_popularity),
        days=days,
    )
    detail.update(
        public_id=game.public_id,
        started_at=game.started_at.isoformat(),
        finished_at=game.finished_at.isoformat(),
    )
    config_row = db.get(GameConfigRow, game.id)
    detail["config"] = config_row.config if config_row else None
    return detail
