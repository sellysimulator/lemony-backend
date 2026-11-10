"""The rate limiter, the client address it is keyed on, and the REST body cap."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import abuse
from app.core.abuse import UNKNOWN_IP, RateLimiter, client_ip
from app.main import app


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_limit_holds_within_a_window_and_resets_after_it() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 10.0, clock=clock)
    assert [limiter.hit("a") for _ in range(3)] == [True, True, False]
    assert limiter.hit("b")  # keys are counted apart
    clock.now = 10.0
    assert limiter.hit("a")


def test_memory_stays_bounded_and_live_keys_survive_a_prune() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 10.0, max_keys=100, clock=clock)
    for i in range(100):
        limiter.hit(f"old{i}")
    clock.now = 10.0  # every window above has ended
    limiter.hit("live")
    assert len(limiter) == 1
    for i in range(1000):
        limiter.hit(f"new{i}")
    assert len(limiter) <= 100


def test_a_prune_frees_a_tenth_even_when_few_windows_have_ended() -> None:
    # A prune scans every key; trimming only to the cap would rescan on every new key.
    clock = Clock()
    limiter = RateLimiter(1, 10.0, max_keys=100, clock=clock)
    limiter.hit("first")
    clock.now = 5.0
    for i in range(99):
        limiter.hit(f"live{i}")
    clock.now = 10.0  # only "first" has ended
    limiter.hit("new")
    assert len(limiter) <= 91


def test_forget_and_clear() -> None:
    limiter = RateLimiter(1, 10.0)
    limiter.hit("a")
    limiter.forget("a")
    assert limiter.hit("a")
    limiter.clear()
    assert len(limiter) == 0


def test_client_ip_takes_the_rightmost_forwarded_entry() -> None:
    scope = {
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")],
        "client": ("6.6.6.6", 1),
    }
    assert client_ip(scope) == "1.2.3.4"
    two_headers = {"headers": [(b"X-Forwarded-For", b"6.6.6.6"), (b"x-forwarded-for", b"5.5.5.5")]}
    assert client_ip(two_headers) == "5.5.5.5"


def test_client_ip_prefers_cf_connecting_ip() -> None:
    scope = {
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4"), (b"CF-Connecting-IP", b" 7.7.7.7 ")],
        "client": ("6.6.6.6", 1),
    }
    assert client_ip(scope) == "7.7.7.7"
    empty = {"headers": [(b"cf-connecting-ip", b" "), (b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")]}
    assert client_ip(empty) == "1.2.3.4"


def test_client_ip_falls_back_to_the_peer() -> None:
    assert client_ip({"headers": [], "client": ("9.9.9.9", 1)}) == "9.9.9.9"
    assert client_ip({"headers": [(b"x-forwarded-for", b" ")], "client": None}) == UNKNOWN_IP
    assert client_ip({}) == UNKNOWN_IP


def test_client_ip_logs_each_source_once_and_never_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=abuse.__name__)
    forwarded = {"headers": [(b"x-forwarded-for", b"6.6.6.6, 1.2.3.4")]}
    assert client_ip(forwarded) == "1.2.3.4"
    assert client_ip(forwarded) == "1.2.3.4"
    assert caplog.messages == ["client ip source: x-forwarded-for (2 entries)"]
    assert client_ip({"headers": [(b"cf-connecting-ip", b"7.7.7.7")]}) == "7.7.7.7"
    assert client_ip({"headers": [], "client": ("9.9.9.9", 1)}) == "9.9.9.9"
    assert caplog.messages == [
        "client ip source: x-forwarded-for (2 entries)",
        "client ip source: cf-connecting-ip",
        "client ip source: peer",
    ]
    for ip in ("6.6.6.6", "1.2.3.4", "7.7.7.7", "9.9.9.9"):
        assert ip not in caplog.text


def test_client_ip_source_label_is_bounded(caplog: pytest.LogCaptureFixture) -> None:
    """A client picks how many X-Forwarded-For entries it sends; the log must not grow with it."""
    caplog.set_level(logging.INFO, logger=abuse.__name__)
    for n in (4, 5, 9):
        chain = ", ".join(f"10.0.0.{i}" for i in range(n)).encode()
        client_ip({"headers": [(b"x-forwarded-for", chain)]})
    assert caplog.messages == ["client ip source: x-forwarded-for (3+ entries)"]


# ------------------------------------------------------------------ body cap
def test_app_answers_413_for_a_large_chunked_body() -> None:
    """Fails against a cap that only reads Content-Length (a chunked body has none)."""

    def chunks() -> Iterator[bytes]:
        yield b'{"x": "'
        for _ in range(4):
            yield b"y" * abuse.MAX_BODY_BYTES
        yield b'"}'

    response = TestClient(app).post(
        "/api/v1/config/validate",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": abuse.TOO_LARGE_MESSAGE}


def test_a_small_chunked_body_still_reaches_the_route() -> None:
    def chunks() -> Iterator[bytes]:
        yield b"{"
        yield b"}"

    response = TestClient(app).post(
        "/api/v1/config/validate",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_replays_the_body_then_hands_over_receive() -> None:
    incoming: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.disconnect"},
        {"type": "http.disconnect"},
    ]
    seen: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        for _ in range(3):
            seen.append(await receive())

    async def no_send(message: Any) -> None:
        raise AssertionError("not called")

    scope: dict[str, Any] = {
        "type": "http",
        "path": "/api/v1/x",
        "method": "POST",
        "headers": [],
        "client": None,
    }
    await abuse.AbuseLimitMiddleware(inner)(scope, receive, no_send)
    assert [m["type"] for m in seen] == ["http.request"] + ["http.disconnect"] * 2
