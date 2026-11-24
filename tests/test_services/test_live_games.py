"""The `live_games` store (Redis off) on both databases: SQLite here, PostgreSQL under
`dbschema`. Expiry compares aware UTC on PostgreSQL and naive-as-UTC on SQLite."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.live_game import LiveGame
from app.services import state_backend
from app.services.state_backend import SqlStateBackend

NOW = datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC)


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(state_backend, "_utcnow", lambda: NOW)
    return NOW


def _keys(sessions: Callable[[], Session]) -> set[str]:
    with sessions() as db:
        return set(db.execute(select(LiveGame.state_key)).scalars())


def _expire_at(sessions: Callable[[], Session], key: str, when: datetime | None) -> None:
    with sessions() as db:
        row = db.get(LiveGame, key)
        assert row is not None
        row.expires_at = when
        db.commit()


def test_set_get_expire_delete(db_sessions: Callable[[], Session], frozen: datetime) -> None:
    backend = SqlStateBackend(db_sessions)
    backend._setex_sync("game:a", 60, "v1")
    backend._setex_sync("game:a", 60, "v2")  # an overwrite, not a second row
    assert backend._get_sync("game:a") == "v2"
    with db_sessions() as db:
        row = db.get(LiveGame, "game:a")
        assert row is not None and row.expires_at is not None
        stored = row.expires_at
    if stored.tzinfo is not None:  # PostgreSQL: aware, in UTC
        assert stored.utcoffset() == timedelta(0)
    assert stored.replace(tzinfo=UTC) == frozen + timedelta(seconds=60)

    backend._setex_sync("game:b", 60, "x")
    assert backend._delete_sync(("game:b", "game:missing")) == 1
    assert backend._get_sync("game:b") is None


def test_expiry_boundary_second(db_sessions: Callable[[], Session], frozen: datetime) -> None:
    """A row is gone at its expiry instant (`<=`), and alive one second before it."""
    backend = SqlStateBackend(db_sessions)
    backend._setex_sync("game:edge", 60, "v")
    backend._setex_sync("game:alive", 60, "v")
    backend._setex_sync("game:forever", 0, "v")  # no expiry
    _expire_at(db_sessions, "game:edge", frozen)
    _expire_at(db_sessions, "game:alive", frozen + timedelta(seconds=1))

    assert backend.purge_expired_sync() == 1
    assert _keys(db_sessions) == {"game:alive", "game:forever"}
    assert backend._get_sync("game:alive") == "v"

    _expire_at(db_sessions, "game:alive", frozen)
    assert backend._get_sync("game:alive") is None  # expired on read, and deleted
    assert _keys(db_sessions) == {"game:forever"}


def test_keys_are_case_sensitive_and_take_long_identities(
    db_sessions: Callable[[], Session],
) -> None:
    backend = SqlStateBackend(db_sessions)
    long_key = "active:" + "Ab" * 92  # 191 characters: the column's bound
    assert len(long_key) == 191
    backend._setex_sync(long_key, 60, "upper")
    backend._setex_sync(long_key.lower(), 60, "lower")
    assert backend._get_sync(long_key) == "upper"
    assert backend._get_sync(long_key.lower()) == "lower"
