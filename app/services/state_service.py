"""Live game documents: CRUD, schema-version guard, sequence numbers.

Keys:
    game:<game_id>        the game document (JSON)
    active:<identity>     the game_id this identity is currently playing

A document whose `schema_version` differs from `SCHEMA_VERSION` is discarded on
read — never migrated in place.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import settings
from .state_backend import StateBackend, build_state_backend

SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 10
_GAME = "game:"
_ACTIVE = "active:"

redis_client: StateBackend = build_state_backend(settings.REDIS_ENABLED, settings.REDIS_URL)


def _decode(raw: str | bytes) -> Any:
    return json.loads(raw.decode() if isinstance(raw, bytes) else raw)


class StateService:
    @staticmethod
    def _backend() -> StateBackend:
        # Read at call time so tests can swap `redis_client`.
        return redis_client

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
            f"{_GAME}{document['game_id']}", settings.GAME_TTL_SECONDS, json.dumps(document)
        )

    async def delete_game(self, game_id: str) -> None:
        await self._backend().delete(f"{_GAME}{game_id}")

    async def get_active_game_id(self, identity: str) -> str | None:
        raw = await self._backend().get(f"{_ACTIVE}{identity}")
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def set_active_game(self, identity: str, game_id: str) -> None:
        await self._backend().setex(f"{_ACTIVE}{identity}", settings.GAME_TTL_SECONDS, game_id)

    async def clear_active_game(self, identity: str) -> None:
        await self._backend().delete(f"{_ACTIVE}{identity}")

    @staticmethod
    def next_seq(document: dict[str, Any]) -> int:
        """Advance the document's sequence number. The caller saves afterwards."""
        document["seq"] = int(document.get("seq", 0)) + 1
        return document["seq"]


state_svc = StateService()
