"""A player's stats never cost a save: the game is committed first, the stats are
recomputed after it in their own transaction, and reading the stats repairs them."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db.session import SessionLocal
from app.main import app
from app.models.game import Game, UserStats
from app.services import db_service, stats_service
from app.services import game_service as gs

from ..conftest import new_guest
from .test_persistence import finished_document

UID = "uid-stats"
AUTH = {"Authorization": "Bearer tok"}


@pytest.fixture
def stats_down(monkeypatch) -> dict[str, Any]:
    """While `down["on"]` is true every stats recompute fails; `down["calls"]` counts them."""
    down: dict[str, Any] = {"on": True, "calls": 0}
    real = stats_service.recompute_user_stats

    def flaky(db: Any, user_id: int) -> UserStats:
        down["calls"] += 1
        if down["on"]:
            raise OperationalError("SELECT", {}, Exception("stats are broken"))
        return real(db, user_id)

    monkeypatch.setattr(stats_service, "recompute_user_stats", flaky)
    return down


@pytest.fixture
def client(firebase_tokens) -> TestClient:
    firebase_tokens["tok"] = {"uid": UID}
    return TestClient(app)


def save(game_id: str = "f" * 32, identity: str = UID) -> str | None:
    document = {**finished_document(identity=identity), "game_id": game_id}
    return db_service.persist_finished_game(document, gs.engine_of(document).summary())


def stored() -> tuple[list[Game], UserStats | None]:
    with SessionLocal() as db:
        games = list(db.execute(select(Game).order_by(Game.id)).scalars())
        user_ids = {g.user_id for g in games if g.user_id is not None}
        stats = db.get(UserStats, user_ids.pop()) if user_ids else None
        return games, stats


def test_a_normal_save_updates_the_stats_at_once():
    assert save() == "f" * 32
    [game], stats = stored()
    assert stats is not None and stats.games_played == 1 and stats.days_played == 3
    assert stats.total_profit == game.total_profit and stats.best_game_id == game.id


def test_a_stats_failure_leaves_the_game_saved_and_returns_its_id(stats_down, caplog):
    assert save() == "f" * 32
    [game], stats = stored()
    assert game.public_id == "f" * 32 and game.user_id is not None and stats is None
    assert "Failed to persist finished game." not in caplog.text
    assert "Failed to recompute player stats" in caplog.text
    assert f"user_id={game.user_id}" in caplog.text and f"game={'f' * 32}" in caplog.text
    assert UID not in caplog.text  # the Firebase uid is never logged

    # A retry of the save finds the stored game, keeps its id and recomputes the stats.
    stats_down["on"] = False
    assert save() == "f" * 32
    [_], stats = stored()
    assert stats is not None and stats.games_played == 1


def test_a_game_another_writer_stored_first_still_gets_its_stats(
    monkeypatch: pytest.MonkeyPatch, stats_down: dict[str, Any]
) -> None:
    """Another writer saves the same game between our lookup and our INSERT, and its stats
    recompute fails: ours hits the duplicate, returns the stored id and fixes the stats."""
    document = finished_document(identity=UID)
    summary = gs.engine_of(document).summary()
    real_ensure = db_service._users.ensure_user
    other: dict[str, Any] = {}
    raced = {"done": False}

    def other_writer_first(db: Any, firebase_uid: str) -> Any:
        if not raced["done"]:
            raced["done"] = True  # the other writer's own lookup goes straight through
            other["saved"] = db_service.persist_finished_game(document, summary)
            other["stats"] = stored()[1]
            stats_down["on"] = False
        return real_ensure(db, firebase_uid)

    monkeypatch.setattr(db_service._users, "ensure_user", other_writer_first)
    assert db_service.persist_finished_game(document, summary) == document["game_id"]
    assert other == {"saved": document["game_id"], "stats": None}
    [game], stats = stored()
    assert stats is not None and stats.games_played == 1 and stats.best_game_id == game.id


def test_reading_the_stats_rebuilds_missing_stats(stats_down, client):
    assert save() == "f" * 32
    assert stored()[1] is None

    stats_down["on"] = False
    body = client.get("/api/v1/users/me/stats", headers=AUTH).json()
    [game], stats = stored()
    assert body["games_played"] == 1 and body["best_game_id"] == "f" * 32
    assert body["total_profit"] == float(game.total_profit)
    assert stats is not None and stats.games_played == 1  # stored, not just served


def test_reading_the_stats_repairs_stats_that_miss_a_later_game(stats_down, client):
    stats_down["on"] = False
    assert save("f" * 32) == "f" * 32
    stats_down["on"] = True
    assert save("e" * 32) == "e" * 32
    games, stats = stored()
    assert len(games) == 2 and stats is not None and stats.games_played == 1

    stats_down["on"] = False
    body = client.get("/api/v1/users/me/stats", headers=AUTH).json()
    assert body["games_played"] == 2 and body["days_played"] == 6
    assert body["total_profit"] == float(sum(g.total_profit for g in games))
    assert body["best_game_id"] == "e" * 32  # a tie on profit goes to the latest stored


def test_reading_current_stats_does_not_recompute(stats_down, client):
    stats_down["on"] = False
    assert save() == "f" * 32
    calls = stats_down["calls"]
    stats_down["on"] = True  # it would fail if it ran
    body = client.get("/api/v1/users/me/stats", headers=AUTH).json()
    assert body["games_played"] == 1 and stats_down["calls"] == calls


def test_a_failed_repair_serves_the_stored_stats_and_the_next_read_retries(
    stats_down, client, caplog
):
    stats_down["on"] = False
    assert save("f" * 32) == "f" * 32
    stats_down["on"] = True
    assert save("e" * 32) == "e" * 32

    response = client.get("/api/v1/users/me/stats", headers=AUTH)
    assert response.status_code == 200 and response.json()["games_played"] == 1
    assert "Failed to repair player stats" in caplog.text

    stats_down["on"] = False
    assert client.get("/api/v1/users/me/stats", headers=AUTH).json()["games_played"] == 2


def test_a_failed_repair_with_no_stats_stored_serves_empty_stats(stats_down, client):
    assert save() == "f" * 32
    response = client.get("/api/v1/users/me/stats", headers=AUTH)
    assert response.status_code == 200 and response.json()["games_played"] == 0


def test_a_claim_is_kept_when_its_stats_fail_and_reading_them_repairs_them(stats_down, client):
    guest = new_guest()
    assert save(identity=guest) == "f" * 32
    assert client.post("/api/v1/users/upsert", json={}, headers=AUTH).status_code == 200

    response = client.post("/api/v1/games/claim", json={"guest_identity": guest}, headers=AUTH)
    assert response.status_code == 200 and response.json() == {"claimed": 1}
    [game], stats = stored()
    assert game.guest_identity is None and game.user_id is not None and stats is None

    stats_down["on"] = False
    assert client.get("/api/v1/users/me/stats", headers=AUTH).json()["games_played"] == 1


def test_a_claim_updates_the_stats_at_once(client):
    guest = new_guest()
    assert save(identity=guest) == "f" * 32
    client.post("/api/v1/users/upsert", json={}, headers=AUTH)
    assert client.post(
        "/api/v1/games/claim", json={"guest_identity": guest}, headers=AUTH
    ).json() == {"claimed": 1}
    [_], stats = stored()
    assert stats is not None and stats.games_played == 1
