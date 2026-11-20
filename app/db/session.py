"""Synchronous SQLAlchemy engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings

_engine_kwargs: dict[str, Any] = {
    "pool_pre_ping": True,
    "echo": settings.DEBUG,
    "connect_args": settings.db_connect_args,
}
if not settings.is_sqlite:
    # A request waits at most 10 s for a free connection, then fails instead of hanging.
    _engine_kwargs.update(
        pool_recycle=3600,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=10,
    )

# Sessions run in UTC through `settings.db_connect_args` (`options=-c timezone=UTC`).
engine = create_engine(settings.db_url, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session that always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
