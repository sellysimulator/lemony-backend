"""The server-default rule, checked on the models (no database needed): only the
`TimestampMixin` columns get a database default (`now()`); every fact column is written
explicitly by the app, so a missed write fails instead of storing a fact nobody played.
"""

import app.models  # noqa: F401
from app.db.base import Base


def test_only_created_at_and_updated_at_have_a_server_default() -> None:
    with_default = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.server_default is not None
    }
    assert with_default, "the timestamp defaults are gone"
    assert {name for _, name in with_default} == {"created_at", "updated_at"}
