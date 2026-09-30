"""The live-state backend, and the switch between Redis and the database.

`StateService` needs six operations (get / setex / delete / exists / lock) —
deliberately a subset of `redis.asyncio.Redis`, so the real client satisfies
the protocol without an adapter.

`REDIS_ENABLED` is the switch and it is **off by default**. Unlike Beery,
whose Redis-off backend is a dict in process memory, Lemony's is the
`live_games` table: a game in progress survives a restart or a redeploy.

**The constraint the database backend carries:** its lock is an
`asyncio.Lock` in this process, so exactly one instance may serve the app.
Two instances could interleave read-modify-writes of one game. Turn Redis on
before scaling out. `app/core/checks/state_backend_check.py` says so at
startup.

Both backends store JSON strings and require an explicit save, so switching
between them is a deployment choice, never a behavioural one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, Self

import anyio.to_thread
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models.live_game import LiveGame

__all__ = ["SqlStateBackend", "StateBackend", "build_state_backend"]


class StateBackend(Protocol):
    async def get(self, key: str) -> str | bytes | None: ...
    async def setex(self, key: str, ttl: int, value: str) -> object: ...
    async def delete(self, *keys: str) -> int: ...
    async def exists(self, key: str) -> int: ...
    def lock(self, key: str, timeout: int = 10) -> object: ...


class _ProcessLock:
    """An `asyncio.Lock` wearing `redis.asyncio.Lock`'s context-manager shape.

    `timeout` is accepted and ignored: within one event loop an owner cannot
    die mid-operation without unwinding through `__aexit__`.
    """

    def __init__(self, lock: asyncio.Lock, timeout: int) -> None:
        self._lock = lock
        self.timeout = timeout

    async def __aenter__(self) -> Self:
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SqlStateBackend:
    """Live state in the `live_games` table, with lazy TTL expiry.

    Every call runs its (synchronous) SQLAlchemy work in a worker thread so the
    event loop never blocks on the database.
    """

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self._session_factory = session_factory
        self._locks: dict[str, asyncio.Lock] = {}

    def _session(self) -> Session:
        if self._session_factory is None:
            from ..db.session import SessionLocal

            self._session_factory = SessionLocal
        return self._session_factory()

    # ------------------------------------------------------------ sync work
    def _get_sync(self, key: str) -> str | None:
        with self._session() as db:
            row = db.get(LiveGame, key)
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at <= _utcnow():
                db.delete(row)
                db.commit()
                return None
            return row.value

    def _setex_sync(self, key: str, ttl: int, value: str) -> None:
        expires = _utcnow() + timedelta(seconds=ttl) if ttl else None
        with self._session() as db:
            row = db.get(LiveGame, key)
            if row is None:
                db.add(LiveGame(state_key=key, value=value, expires_at=expires))
            else:
                row.value = value
                row.expires_at = expires
            db.commit()

    def _delete_sync(self, keys: tuple[str, ...]) -> int:
        with self._session() as db:
            result = db.execute(delete(LiveGame).where(LiveGame.state_key.in_(keys)))
            db.commit()
            return int(result.rowcount or 0)

    def purge_expired_sync(self) -> int:
        """Drop every expired row. Returns how many went."""
        with self._session() as db:
            result = db.execute(delete(LiveGame).where(LiveGame.expires_at <= _utcnow()))
            db.commit()
            return int(result.rowcount or 0)

    def keys_sync(self) -> list[str]:
        with self._session() as db:
            return list(db.execute(select(LiveGame.state_key)).scalars())

    # ----------------------------------------------------------- protocol
    async def get(self, key: str) -> str | None:
        return await anyio.to_thread.run_sync(self._get_sync, key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        await anyio.to_thread.run_sync(self._setex_sync, key, ttl, value)
        return True

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        removed = await anyio.to_thread.run_sync(self._delete_sync, keys)
        for key in keys:
            self._locks.pop(key, None)
        return removed

    async def exists(self, key: str) -> int:
        return 1 if await self.get(key) is not None else 0

    def lock(self, key: str, timeout: int = 10) -> _ProcessLock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return _ProcessLock(lock, timeout)


def build_state_backend(enabled: bool, url: str) -> StateBackend:
    """The Redis client when `enabled`, else a `SqlStateBackend`."""
    if not enabled:
        return SqlStateBackend()
    if not url:
        raise ValueError(
            "REDIS_ENABLED is true but REDIS_URL is empty. Set REDIS_URL, or set "
            "REDIS_ENABLED=false to keep live games in the database."
        )
    import redis.asyncio as aioredis

    return aioredis.from_url(url)  # type: ignore[no-any-return]
