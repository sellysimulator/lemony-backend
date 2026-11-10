"""Self-cleaning locks, game ids refused before a lock, oversized tokens, the
handshake off the event loop, and the cached deep health probe."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api import deps
from app.api.v1 import health
from app.config import settings
from app.db.session import SessionLocal
from app.models.live_game import LiveGame
from app.services import state_service
from app.services.state_backend import SqlStateBackend
from app.sockets.handlers import connection, game

from ..conftest import new_guest
from .test_play import config, connect


@pytest.mark.asyncio
async def test_a_lock_is_dropped_once_nobody_holds_it():
    backend = SqlStateBackend()
    async with backend.lock("k"):
        assert "k" in backend._locks
    assert "k" not in backend._locks


@pytest.mark.asyncio
async def test_the_lock_still_excludes():
    backend = SqlStateBackend()
    inside = peak = 0

    async def worker() -> None:
        nonlocal inside, peak
        async with backend.lock("k"):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1 and "k" not in backend._locks


@pytest.mark.asyncio
@pytest.mark.parametrize("game_id", [None, 7, "", "x" * 100_000, "F" * 32, "f" * 33])
async def test_a_malformed_game_id_is_refused_before_any_lock(
    emits: Any, monkeypatch: pytest.MonkeyPatch, game_id: Any
) -> None:
    taken: list[str] = []
    real_lock = state_service.state_svc.lock

    def spy(key: str) -> Any:
        taken.append(key)
        return real_lock(key)

    monkeypatch.setattr(state_service.state_svc, "lock", spy)
    await connect("s1", {"guestId": new_guest()})
    await game.submit_day("s1", {"game_id": game_id, "day": 1})
    await game.ack_day("s1", {"game_id": game_id, "day": 1})
    await game.abandon_game("s1", {"game_id": game_id})
    assert [e["code"] for e in emits.of("error")] == ["NO_GAME"] * 3
    assert taken == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, "x", 5, [1], {"game_id": {"a": 1}}])
async def test_a_payload_of_any_shape_gets_a_refusal_not_a_crash(emits, data):
    await connect("s1", {"guestId": new_guest()})
    for handler in (game.submit_day, game.ack_day, game.abandon_game, game.request_state):
        await handler("s1", data)
    assert [e["code"] for e in emits.of("error")] == ["NO_GAME"] * 4


def test_an_oversized_id_token_is_refused_unparsed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable() -> None:
        raise AssertionError("an oversized token reached Firebase")

    monkeypatch.setattr(deps, "init_firebase", unreachable)
    assert deps.verify_firebase_id_token("x" * (deps.MAX_ID_TOKEN_LENGTH + 1)) is None


@pytest.mark.asyncio
async def test_token_verification_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    on_loop_thread: list[bool] = []

    def verify(token: str) -> dict | None:
        on_loop_thread.append(threading.current_thread() is threading.main_thread())
        return {"uid": "uid-1"}

    monkeypatch.setattr(connection, "verify_firebase_id_token", verify)
    assert await connect("s1", {"idToken": "tok"}) == "uid-1"
    assert on_loop_thread == [False]


@pytest.mark.asyncio
async def test_deep_health_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    pings: list[int] = []
    monkeypatch.setattr(health, "_ping_database", lambda: pings.append(1))
    monkeypatch.setattr(health, "_deep_cache", None)
    first = await health.health_deep()
    second = await health.health_deep()
    assert first.status == "ok" and second is first and pings == [1]


@pytest.mark.asyncio
async def test_a_guest_game_gets_the_guest_ttl(emits: Any) -> None:
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": config()})
    game_id = emits.of("game_created")[0]["game_id"]
    with SessionLocal() as db:
        row = db.get(LiveGame, f"game:{game_id}")
        assert row is not None and row.expires_at is not None
        left = (row.expires_at - datetime.now(UTC).replace(tzinfo=None)).total_seconds()
    assert state_service.ttl_for(guest) <= state_service.GUEST_GAME_TTL_SECONDS
    assert abs(left - state_service.ttl_for(guest)) < 60
    assert state_service.ttl_for("uid-1") == settings.GAME_TTL_SECONDS


@pytest.mark.asyncio
async def test_expired_live_games_are_swept_at_most_once_a_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await state_service.redis_client.setex("game:stale", 60, "{}")
    await state_service.redis_client.setex("game:live", 60, "{}")
    await state_service.redis_client.setex("game:forever", 0, "{}")  # no expiry
    with SessionLocal() as db:
        row = db.get(LiveGame, "game:stale")
        assert row is not None
        row.expires_at = datetime(2000, 1, 1)  # noqa: DTZ001 — column is naive UTC
        db.commit()
    monkeypatch.setattr(state_service.state_svc, "_last_purge", float("-inf"))
    await state_service.state_svc.purge_expired()
    with SessionLocal() as db:
        assert db.get(LiveGame, "game:stale") is None
        assert db.get(LiveGame, "game:live") is not None  # only expired rows go
        assert db.get(LiveGame, "game:forever") is not None

    sweeps: list[int] = []

    def sweep() -> int:
        sweeps.append(1)
        return 0

    monkeypatch.setattr(state_service.redis_client, "purge_expired_sync", sweep)
    await state_service.state_svc.purge_expired()  # within the period: skipped
    assert sweeps == []
