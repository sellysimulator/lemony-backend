"""The `live_games` table: the state store used when Redis is switched off.

One row per key (a game document, or an identity's active-game pointer). It
mirrors the Redis `SETEX` semantics: a string value with an absolute expiry.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class LiveGame(Base):
    __tablename__ = "live_games"

    state_key: Mapped[str] = mapped_column(String(191), primary_key=True)
    value: Mapped[str] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False
    )
    # Naive UTC.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True, index=True)
