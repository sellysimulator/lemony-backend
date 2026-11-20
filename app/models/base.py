"""Declarative base and the shared timestamp mixin."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every Lemony model.

    The naming convention gives every unnamed constraint and index a deterministic
    name, identical in PostgreSQL, the suite's SQLite and `db/schema.postgres.sql`.
    """

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class TimestampMixin:
    """`created_at` / `updated_at` for the persisted game record.

    `default=` and `onupdate=` are ORM-side and fire only for ORM inserts. A Core
    insert bypasses them, and a NOT NULL column with no database-side default then
    fails with a not-null violation. `server_default=now()` makes the database
    supply the value, which keeps both paths correct. Both columns are
    `timestamptz`, always written and read in UTC.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
