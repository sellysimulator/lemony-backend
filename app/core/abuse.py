"""Abuse limits: per-client rate limits, and the client address they are keyed on.

Counts live in this process's memory. That is the whole store because exactly one
instance serves the app (see `services/state_backend.py`). Each limiter remembers at
most `MAX_KEYS` keys, so a flood of distinct addresses cannot grow memory without bound.
The limits are plain constants: change them here.

Where they apply:
- REST: `AbuseLimitMiddleware`, added in `app/main.py`. Every request counts against
  `REST_LIMIT` per address, requests to `HEAVY_PATHS` against `HEAVY_LIMIT` as well, and
  a body declared larger than `MAX_BODY_BYTES` is refused unread. 429 / 413, JSON `detail`.
- Socket.IO: `SocketManager.connect` (open sockets and connects per address) and
  `guarded()` (events per sid, and `preview_plan` per sid, dropped unanswered;
  `create_game` per address, answered with `error {code: "RATE_LIMITED"}`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

# ------------------------------------------------------------------- REST
REST_LIMIT = 600  # requests per address per REST_WINDOW, any REST route
REST_WINDOW = 60.0
HEAVY_LIMIT = 120  # requests per address per HEAVY_WINDOW to HEAVY_PATHS
HEAVY_WINDOW = 60.0
#: Open to anyone, and each one runs the config validator or the engine, or opens
#: backing-service connections. Paths without a trailing slash.
HEAVY_PATHS = frozenset(
    {
        "/api/v1/config/preview",
        "/api/v1/config/validate",
        "/api/v1/config/cup-cost",
        "/api/v1/health/deep",
    }
)
MAX_BODY_BYTES = 64 * 1024  # a full config is about 3 KB

# --------------------------------------------------------------- Socket.IO
MAX_SOCKETS_PER_IP = 50  # open connections per address
CONNECT_LIMIT = 60  # connects per address per CONNECT_WINDOW
CONNECT_WINDOW = 60.0
EVENT_LIMIT = 20  # events per sid per EVENT_WINDOW; a real client sends one per click
EVENT_WINDOW = 1.0
# create_game per address per CREATE_GAME_WINDOW (a classroom shares one address)
CREATE_GAME_LIMIT = 60
CREATE_GAME_WINDOW = 60.0
PREVIEW_LIMIT = 5  # preview_plan per sid per PREVIEW_WINDOW (the plan screen debounces)
PREVIEW_WINDOW = 1.0

MAX_KEYS = 10_000  # keys one limiter remembers
UNKNOWN_IP = "unknown"
RATE_LIMITED_MESSAGE = "Too many requests. Please wait a moment and try again."
TOO_LARGE_MESSAGE = "Request body too large."


class RateLimiter:
    """At most `limit` hits per key in each fixed window of `window` seconds.

    When `max_keys` keys are remembered, keys whose window has ended are dropped
    first, then the oldest, down to nine tenths of `max_keys`: a prune is a full scan,
    so it must free enough room that the next one is far off.
    """

    def __init__(
        self,
        limit: int,
        window: float,
        max_keys: int = MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.clock = clock
        self._windows: dict[str, tuple[float, int]] = {}  # key -> (window start, hits)

    def __len__(self) -> int:
        return len(self._windows)

    def hit(self, key: str) -> bool:
        """Count one hit for `key`. True while the key is within its limit."""
        now = self.clock()
        start, hits = self._windows.get(key, (now, 0))
        if now - start >= self.window:
            start, hits = now, 0
        if key not in self._windows and len(self._windows) >= self.max_keys:
            self._prune(now)
        self._windows[key] = (start, hits + 1)
        return hits + 1 <= self.limit

    def forget(self, key: str) -> None:
        self._windows.pop(key, None)

    def clear(self) -> None:
        self._windows.clear()

    def _prune(self, now: float) -> None:
        ended = [k for k, (start, _) in self._windows.items() if now - start >= self.window]
        for key in ended:
            del self._windows[key]
        excess = len(self._windows) - min(self.max_keys * 9 // 10, self.max_keys - 1)
        if excess > 0:
            for key in list(self._windows)[:excess]:
                del self._windows[key]


rest_limiter = RateLimiter(REST_LIMIT, REST_WINDOW)
heavy_limiter = RateLimiter(HEAVY_LIMIT, HEAVY_WINDOW)
connect_limiter = RateLimiter(CONNECT_LIMIT, CONNECT_WINDOW)
event_limiter = RateLimiter(EVENT_LIMIT, EVENT_WINDOW)
create_game_limiter = RateLimiter(CREATE_GAME_LIMIT, CREATE_GAME_WINDOW)
preview_limiter = RateLimiter(PREVIEW_LIMIT, PREVIEW_WINDOW)
#: Keyed on sid: `SocketManager.disconnect` forgets the sid in each.
SID_LIMITERS = (event_limiter, preview_limiter)


def reset() -> None:
    """Forget every count. For tests."""
    for limiter in (
        rest_limiter,
        heavy_limiter,
        connect_limiter,
        event_limiter,
        create_game_limiter,
        preview_limiter,
    ):
        limiter.clear()


def client_ip(scope: Mapping[str, Any]) -> str:
    """The caller's address for an ASGI scope (REST, or Socket.IO's `environ["asgi.scope"]`).

    Order: the `cf-connecting-ip` header if non-empty, else the RIGHTMOST non-empty
    `X-Forwarded-For` entry, else the ASGI peer.

    Render fronts services with Cloudflare, which overwrites `cf-connecting-ip`, and its
    proxy appends the address it saw to `X-Forwarded-For`, so the RIGHTMOST entry is the
    one a client cannot forge. uvicorn's `--proxy-headers` with `FORWARDED_ALLOW_IPS=*`
    puts the LEFTMOST, forgeable entry into `scope["client"]`, so that is only the
    fallback, for a request that came through no proxy (local dev).
    """
    headers = scope.get("headers") or ()
    for name, value in headers:
        if name.lower() == b"cf-connecting-ip":
            cf = value.decode("latin-1").strip()
            if cf:
                return cf[:64]
    forwarded = [
        value.decode("latin-1") for name, value in headers if name.lower() == b"x-forwarded-for"
    ]
    if forwarded:
        entries = [e.strip() for e in ",".join(forwarded).split(",")]
        last = next((e for e in reversed(entries) if e), "")
        if last:
            return last[:64]
    client = scope.get("client")
    return str(client[0]) if client else UNKNOWN_IP


def _declared_length(scope: Scope) -> int:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


async def _reply(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", b"%d" % len(body))]
    if status == 429:
        headers.append((b"retry-after", b"%d" % int(REST_WINDOW)))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class AbuseLimitMiddleware:
    """Pure ASGI middleware for the REST app: 429 over a rate limit, 413 over the body cap.

    Add it before `CORSMiddleware`, so CORS wraps it and a 429 still carries the CORS
    headers a browser needs to read it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ip = client_ip(scope)
        heavy = str(scope.get("path", "")).rstrip("/") in HEAVY_PATHS
        if not rest_limiter.hit(ip) or (heavy and not heavy_limiter.hit(ip)):
            await _reply(send, 429, RATE_LIMITED_MESSAGE)
            return
        if _declared_length(scope) > MAX_BODY_BYTES:
            await _reply(send, 413, TOO_LARGE_MESSAGE)
            return
        await self.app(scope, receive, send)
