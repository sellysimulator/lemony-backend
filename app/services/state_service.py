"""Live game documents: CRUD, schema-version guard, sequence numbers.

Keys:
    game:<game_id>        the game document (JSON)
    active:<identity>     the game_id this identity is currently playing

A document whose `schema_version` differs from `SCHEMA_VERSION` is discarded on
read — never migrated in place.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anyio.to_thread

from ..config import settings
from .state_backend import SqlStateBackend, StateBackend, build_state_backend

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
LOCK_TIMEOUT_SECONDS = 10
_GAME = "game:"
_ACTIVE = "active:"

# A guest's live game (and its active pointer) lives at most this long after its
# last save; a signed-in player's lives GAME_TTL_SECONDS. Anyone can mint guests.
GUEST_GAME_TTL_SECONDS = 2 * 24 * 3600
_GUEST_PREFIX = "guest_"
# Expired `live_games` rows are swept at most this often (database backend only;
# Redis expires keys itself). Without it they go only at startup or when read.
PURGE_EVERY_SECONDS = 600.0

redis_client: StateBackend = build_state_backend(settings.REDIS_ENABLED, settings.REDIS_URL)


def _decode(raw: str | bytes) -> Any:
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


def ttl_for(identity: str) -> int:
    """Seconds a live game of `identity` survives after its last save."""
    if identity.startswith(_GUEST_PREFIX):
        return min(GUEST_GAME_TTL_SECONDS, settings.GAME_TTL_SECONDS)
    return settings.GAME_TTL_SECONDS


class StateService:
    _last_purge = float("-inf")

    @staticmethod
    def _backend() -> StateBackend:
        # Read at call time so tests can swap `redis_client`.
        return redis_client

    async def purge_expired(self) -> None:
        """Delete expired live games, at most once per PURGE_EVERY_SECONDS. Never raises."""
        backend = self._backend()
        now = time.monotonic()
        if not isinstance(backend, SqlStateBackend) or now - self._last_purge < PURGE_EVERY_SECONDS:
            return
        self._last_purge = now
        try:
            purged = await anyio.to_thread.run_sync(backend.purge_expired_sync)
        except Exception as exc:  # noqa: BLE001 — a failed sweep must not fail the caller
            logger.warning("Could not purge expired live games (%s).", type(exc).__name__)
            return
        if purged:
            logger.info("Purged %d expired live game rows.", purged)

    def lock(self, game_id: str) -> Any:
        return self._backend().lock(f"lock:{_GAME}{game_id}", timeout=LOCK_TIMEOUT_SECONDS)

    def identity_lock(self, identity: str) -> Any:
        return self._backend().lock(f"lock:{_ACTIVE}{identity}", timeout=LOCK_TIMEOUT_SECONDS)

    async def get_game(self, game_id: str) -> dict[str, Any] | None:
        key = f"{_GAME}{game_id}"
        raw = await self._backend().get(key)
        if raw is None:
            return None
        document = _decode(raw)
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            await self._backend().delete(key)
            return None
        return document

    async def save_game(self, document: dict[str, Any]) -> None:
        await self._backend().setex(
            f"{_GAME}{document['game_id']}",
            ttl_for(str(document.get("identity", ""))),
            json.dumps(document),
        )

    async def delete_game(self, game_id: str) -> None:
        await self._backend().delete(f"{_GAME}{game_id}")

    async def get_active_game_id(self, identity: str) -> str | None:
        raw = await self._backend().get(f"{_ACTIVE}{identity}")
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def set_active_game(self, identity: str, game_id: str) -> None:
        await self._backend().setex(f"{_ACTIVE}{identity}", ttl_for(identity), game_id)

    async def clear_active_game(self, identity: str) -> None:
        await self._backend().delete(f"{_ACTIVE}{identity}")

    @staticmethod
    def next_seq(document: dict[str, Any]) -> int:
        """Advance the document's sequence number. The caller saves afterwards."""
        document["seq"] = int(document.get("seq", 0)) + 1
        return document["seq"]


state_svc = StateService()
