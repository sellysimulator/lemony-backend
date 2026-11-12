"""Abuse limits where they are wired in: the REST middleware, the socket handshake, and
`guarded()` around every socket handler."""

from __future__ import annotations

from typing import Any

import pytest
import socketio  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from app.config import settings
from app.core import abuse
from app.main import app
from app.sockets.handlers import connection, game
from app.sockets.manager import sio, socket_manager

from ..conftest import new_guest
from .test_play import config


def environ(ip: str) -> dict[str, Any]:
    return {"asgi.scope": {"headers": [(b"x-forwarded-for", f"6.6.6.6, {ip}".encode())]}}


# ---------------------------------------------------------------------- REST
def test_a_heavy_route_answers_429_past_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse.heavy_limiter, "limit", 2)
    client = TestClient(app)
    origin = settings.CORS_ORIGINS[0]
    sent = {"X-Forwarded-For": "6.6.6.6, 1.1.1.1", "Origin": origin}
    codes = [client.post("/api/v1/config/validate", json={}, headers=sent) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    refused = codes[-1]
    assert refused.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}
    assert refused.headers["retry-after"] == "60"
    assert refused.headers["access-control-allow-origin"] == origin
    assert "retry-after" in refused.headers["access-control-expose-headers"].lower()
    # a forged leftmost entry changes nothing; another real address is counted apart
    other = {"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}
    assert client.post("/api/v1/config/validate", json={}, headers=other).status_code == 200
    # light routes only count against the general limit
    assert client.get("/api/v1/health", headers=sent).status_code == 200


def test_every_route_answers_429_past_the_general_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse.rest_limiter, "limit", 1)
    client = TestClient(app)
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/health").status_code == 429


def test_an_oversized_body_is_refused_unread() -> None:
    client = TestClient(app)
    body = b"{" + b" " * abuse.MAX_BODY_BYTES + b"}"
    res = client.post(
        "/api/v1/config/validate", content=body, headers={"Content-Type": "application/json"}
    )
    assert res.status_code == 413 and res.json() == {"detail": abuse.TOO_LARGE_MESSAGE}


# ------------------------------------------------------------------- sockets
def test_the_socket_buffer_is_small() -> None:
    assert sio.eio.max_http_buffer_size == 64 * 1024


@pytest.mark.asyncio
async def test_open_sockets_are_capped_per_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.sockets.manager.MAX_SOCKETS_PER_IP", 2)
    auth = {"guestId": new_guest()}
    assert await connection.connect("a", environ("1.1.1.1"), auth) is None
    assert await connection.connect("b", environ("1.1.1.1"), auth) is None
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await connection.connect("c", environ("1.1.1.1"), auth)
    assert await connection.connect("d", environ("2.2.2.2"), auth) is None
    await connection.disconnect("a")
    assert await connection.connect("e", environ("1.1.1.1"), auth) is None
    assert socket_manager.sockets_per_ip == {"1.1.1.1": 2, "2.2.2.2": 1}


@pytest.mark.asyncio
async def test_connects_are_rate_limited_per_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse.connect_limiter, "limit", 1)
    auth = {"guestId": new_guest()}
    assert await connection.connect("a", environ("1.1.1.1"), auth) is None
    await connection.disconnect("a")
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await connection.connect("b", environ("1.1.1.1"), auth)


@pytest.mark.asyncio
async def test_a_refused_token_frees_its_slot(firebase_tokens: dict[str, dict]) -> None:
    assert await connection.connect("a", environ("1.1.1.1"), {"idToken": "bad"}) is False
    assert socket_manager.sockets_per_ip == {} and "a" not in socket_manager.sid_to_ip


@pytest.mark.asyncio
async def test_a_flood_from_one_sid_is_dropped_unanswered(
    emits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(abuse.event_limiter, "limit", 3)
    await connection.connect("s1", environ("1.1.1.1"), {"guestId": new_guest()})
    for _ in range(5):
        await game.request_state("s1", {"game_id": "f" * 32})
    assert [e["code"] for e in emits.of("error")] == ["NO_GAME"] * 3


@pytest.mark.asyncio
async def test_create_game_is_rate_limited_per_address(
    emits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(abuse.create_game_limiter, "limit", 1)
    for sid in ("s1", "s2"):  # a fresh guest per socket does not reset the count
        await connection.connect(sid, environ("1.1.1.1"), {})
        await game.create_game(sid, {"config": config()})
    assert len(emits.of("game_created")) == 1
    assert emits.of("error")[-1] == {
        "code": "RATE_LIMITED",
        "message": abuse.RATE_LIMITED_MESSAGE,
    }
