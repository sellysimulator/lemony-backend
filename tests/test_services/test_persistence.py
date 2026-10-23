"""End-of-game persistence: what is written, how, and that a stored game rebuilds exactly."""

import re
from collections import Counter
from typing import Any, Self

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config_model import GameConfig
from app.core.defaults import default_config
from app.core.engine import DayPlan, LemonadeGame
from app.db.session import SessionLocal, engine
from app.models.game import Game, UserStats
from app.services import db_service
from app.services import game_service as gs

PLAN = {
    "purchases": {"ice": {"100": 1}, "sugar": {"100": 1}, "lemons": {"300": 1}, "cups": {"100": 1}},
    "price": 0.6,
    "recipe": {"ice": 2, "sugar": 2, "lemons": 3},
}


def finished_document(days: int = 3, identity: str = "guest_persist") -> dict[str, Any]:
    data = default_config()
    data["num_days"] = days
    game = LemonadeGame(GameConfig.model_validate(data), seed=20261022)
    for day in range(1, days + 1):
        game.run_day(DayPlan.model_validate(PLAN))
        game.acknowledge_day(day)
    assert game.phase == "finished"
    return {
        "game_id": "f" * 32,
        "identity": identity,
        "engine": game.to_payload(),
        "started_at": "2026-10-22T09:00:00",
        "finished_at": "2026-10-22T09:30:00",
        "persisted": False,
        "public_id": None,
    }


class Statements:
    """Every statement the test database executes (before_cursor_execute)."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __enter__(self) -> Self:
        event.listen(engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(engine, "before_cursor_execute", self._on)

    def _on(self, conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        self.seen.append(statement)

    def inserts(self) -> Counter[str]:
        tables = (re.match(r"\s*INSERT\s+INTO\s+`?(\w+)", s, re.IGNORECASE) for s in self.seen)
        return Counter(m.group(1) for m in tables if m)


def test_one_insert_statement_per_table():
    document = finished_document(days=3)
    with Statements() as statements:
        assert db_service.persist_finished_game(document, gs.engine_of(document).summary())
    assert statements.inserts() == {
        "games": 1,
        "game_configs": 1,
        "game_days": 1,
        "game_day_types": 1,
        "game_day_hours": 1,
    }


def test_persisting_twice_writes_once():
    document = finished_document()
    summary = gs.engine_of(document).summary()
    assert db_service.persist_finished_game(document, summary) == document["game_id"]
    assert db_service.persist_finished_game(document, summary) == document["game_id"]
    with SessionLocal() as db:
        assert len(db.execute(select(Game)).scalars().all()) == 1


def test_a_stored_game_rebuilds_the_live_summary_exactly():
    document = finished_document(days=3)
    public_id = db_service.persist_finished_game(document, gs.engine_of(document).summary())
    document["public_id"] = public_id
    live = gs.summary_of(document)
    with SessionLocal() as db:
        game = db.execute(select(Game).where(Game.public_id == public_id)).scalar_one()
        stored = db_service.load_game_detail(db, game)
    assert stored["started_at"] and stored["finished_at"]
    assert {k: v for k, v in stored.items() if k not in ("started_at", "finished_at")} == live
    assert all(day["inventory_end"] for day in stored["days"])


def test_a_failure_while_resolving_a_duplicate_returns_none(monkeypatch, caplog):
    def duplicate(*args: Any) -> Game:
        raise IntegrityError("INSERT", {}, Exception("duplicate"))

    real_execute = db_service.Session.execute

    def lost_connection(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr(db_service, "persist_game", duplicate)
    monkeypatch.setattr(db_service.Session, "execute", lost_connection)
    try:
        assert db_service.persist_finished_game(finished_document(), {}) is None
    finally:
        monkeypatch.setattr(db_service.Session, "execute", real_execute)
    assert "Failed to persist finished game." in caplog.text


def test_a_failed_write_never_reaches_the_game(monkeypatch):
    def broken(*args: Any) -> Game:
        raise OperationalError("INSERT", {}, Exception("database is down"))

    monkeypatch.setattr(db_service, "persist_game", broken)
    document = finished_document()
    assert db_service.persist_finished_game(document, gs.engine_of(document).summary()) is None


def test_the_best_game_tie_goes_to_the_latest_stored():
    first = finished_document(identity="uid-tie")
    second = {**finished_document(identity="uid-tie"), "game_id": "e" * 32}
    for document in (first, second):
        assert db_service.persist_finished_game(document, gs.engine_of(document).summary())
    with SessionLocal() as db:
        games = db.execute(select(Game).order_by(Game.id)).scalars().all()
        assert games[0].total_profit == games[1].total_profit
        assert games[0].finished_at == games[1].finished_at
        stats = db.get(UserStats, games[0].user_id)
        assert stats is not None and stats.best_game_id == games[1].id
