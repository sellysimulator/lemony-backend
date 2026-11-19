"""The `live_games` table: the state store used when Redis is switched off.

One row per key (a game document, or an identity's active-game pointer). It
mirrors the Redis `SETEX` semantics: a string value with an absolute expiry.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class LiveGame(Base):
    __tablename__ = "live_games"

    # Keys are `game:<32 hex>` or `active:<identity>` (a guest id or a Firebase uid,
    # at most 128 chars), well under the bound.
    state_key: Mapped[str] = mapped_column(String(191), primary_key=True)
    # The JSON game document; `text` has no practical size limit.
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # Aware UTC (`timestamptz`); null means the key never expires.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
