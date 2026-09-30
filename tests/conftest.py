"""Shared fixtures.

The environment is overridden before `app` is imported, so a developer's `.env`
(which may point at a real database) is never touched: the suite runs on a
throwaway SQLite file, with Firebase unconfigured.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

_DB_FILE = Path(tempfile.mkdtemp(prefix="lemony-test-")) / "test.db"
os.environ.update(
    {
        "DB_URL_OVERRIDE": f"sqlite:///{_DB_FILE}",
        "REDIS_ENABLED": "False",
        "REDIS_URL": "",
        "FIREBASE_SERVICE_ACCOUNT_JSON": "",
        "DEBUG": "False",
    }
)

import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import engine


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, Any]] = []

    async def emit_to_sid(self, sid: str, event: str, data: Any) -> None:
        self.events.append((sid, event, data))

    def of(self, event: str) -> list[Any]:
        return [d for _, e, d in self.events if e == event]


@pytest.fixture
def emits(monkeypatch) -> Recorder:
    from app.sockets.manager import socket_manager

    rec = Recorder()
    monkeypatch.setattr(socket_manager, "emit_to_sid", rec.emit_to_sid)
    return rec


@pytest.fixture
def firebase_tokens(monkeypatch) -> dict[str, dict]:
    """{token: claims}; any other token fails verification."""
    tokens: dict[str, dict] = {}

    def verify(token: str) -> dict | None:
        return tokens.get(token)

    import app.sockets.handlers.connection as conn
    from app.api import deps

    monkeypatch.setattr(deps, "verify_firebase_id_token", verify)
    monkeypatch.setattr(conn, "verify_firebase_id_token", verify)
    return tokens


def new_guest() -> str:
    return f"guest_{uuid.uuid4()}"
