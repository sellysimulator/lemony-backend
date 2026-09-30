"""Synchronous SQLAlchemy engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings

_engine_kwargs: dict[str, Any] = {
    "pool_pre_ping": True,
    "echo": settings.DEBUG,
    "connect_args": settings.db_connect_args,
}
if not settings.is_sqlite:
    _engine_kwargs.update(pool_recycle=3600, pool_size=10, max_overflow=20)

engine = create_engine(settings.db_url, **_engine_kwargs)

if not settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_utc_timezone(dbapi_connection: Any, connection_record: Any) -> None:
        """Keep every timestamp the driver hands back in UTC."""
        cursor = dbapi_connection.cursor()
        cursor.execute("SET time_zone = '+00:00'")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session that always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
