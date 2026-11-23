"""PostgreSQL through Testcontainers, for the `dbschema` tests.

One `postgres:18` container per session with `alembic upgrade head` applied, and
an engine built with the app's own connect args (`Settings.db_connect_args`).
Every test starts on empty tables: the teardown truncates them all. Nothing
here ever connects to a database named by `settings` or a `.env`.

`db_sessions` runs a test once per database: on the suite's SQLite, and on
PostgreSQL under the `dbschema` marker (with the app's `SessionLocal` pointed
at it, so the real service code path writes and reads there).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "postgres:18"


@dataclass
class PgDatabase:
    container: Any  # testcontainers PostgresContainer
    engine: Engine

    @property
    def user(self) -> str:
        return str(self.container.username)

    @property
    def dbname(self) -> str:
        return str(self.container.dbname)


def _connect_args(url: str) -> dict[str, Any]:
    """What the app would pass for this URL: UTC sessions, no TLS (local container)."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, DB_URL_OVERRIDE=url, DB_REQUIRE_SSL=False, DB_SSL_CA=""
    )
    return settings.db_connect_args


@pytest.fixture(scope="session")
def pg() -> Iterator[PgDatabase]:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(IMAGE, driver="psycopg") as container:
        url = container.get_connection_url()
        engine = create_engine(url, connect_args=_connect_args(url))
        cfg = Config()  # no ini file: alembic must not reconfigure the suite's logging
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        try:
            yield PgDatabase(container, engine)
        finally:
            engine.dispose()


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as conn:
        names = (
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
                    "AND table_name <> 'alembic_version'"
                )
            )
            .scalars()
            .all()
        )
        if names:
            tables = ", ".join(f'"{name}"' for name in names)
            conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def pg_sessions(pg: PgDatabase) -> Iterator[sessionmaker[Session]]:
    """A session factory on the migrated PostgreSQL; every table is emptied after."""
    try:
        yield sessionmaker(autocommit=False, autoflush=False, bind=pg.engine)
    finally:
        _truncate_all(pg.engine)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.dbschema)])
def db_sessions(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], Session]:
    """The session factory the app uses, on SQLite and (dbschema) on PostgreSQL."""
    from app.db import session as db_session

    if request.param == "sqlite":
        return db_session.SessionLocal
    factory: sessionmaker[Session] = request.getfixturevalue("pg_sessions")
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    return factory
