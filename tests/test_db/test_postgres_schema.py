"""The migrated PostgreSQL schema: it matches the models, the committed DDL, and the
session settings the app relies on. Needs Docker (`pytest -m dbschema`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.db.base import Base
from tests.fixtures.postgres import PgDatabase

pytestmark = pytest.mark.dbschema

DDL = Path(__file__).resolve().parents[2] / "db" / "schema.postgres.sql"


def normalize(dump: str) -> str:
    """Same rule as `normalize()` in Novus/scripts/db/gen_ddl.py, which writes the
    file."""
    keep = []
    for line in dump.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(
            ("--", "\\restrict", "\\unrestrict", "SET ", "SELECT pg_catalog.set_config")
        ):
            continue
        keep.append(line.rstrip())
    return "\n".join(keep) + "\n"


def test_ddl_snapshot_matches_migrations(pg: PgDatabase) -> None:
    exit_code, output = pg.container.exec(
        [
            "pg_dump",
            "-U",
            pg.user,
            "-d",
            pg.dbname,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
        ]
    )
    assert exit_code == 0, output
    assert (
        normalize(output.decode()) == DDL.read_text()
    ), "db/schema.postgres.sql is stale: run Novus/scripts/db/gen_ddl.py --backend ."


def test_models_match_migrations(pg: PgDatabase) -> None:
    with pg.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []


def test_sessions_run_in_utc(pg: PgDatabase) -> None:
    """`options=-c timezone=UTC` from `Settings.db_connect_args` reaches the server."""
    with pg.engine.connect() as conn:
        assert conn.execute(text("SHOW TimeZone")).scalar_one() == "UTC"


def test_every_timestamp_column_is_timestamptz(pg: PgDatabase) -> None:
    with pg.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND data_type LIKE 'timestamp%'"
            )
        ).all()
    assert rows, "no timestamp columns found"
    assert {r.data_type for r in rows} == {"timestamp with time zone"}, rows
