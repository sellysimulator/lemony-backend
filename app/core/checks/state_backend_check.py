"""Startup check: say which store holds live games, and purge expired rows."""

from __future__ import annotations

import logging

from ...config import settings
from . import register_check

logger = logging.getLogger(__name__)


@register_check
def check_state_backend() -> None:
    if settings.REDIS_ENABLED:
        if not settings.REDIS_URL:
            logger.critical(
                "REDIS_ENABLED is true but REDIS_URL is empty. Live games cannot be stored. "
                "Set REDIS_URL, or set REDIS_ENABLED=false."
            )
            return
        logger.info("Live games: Redis. Multiple instances may serve the app.")
        return
    logger.warning(
        "Live games: the database `live_games` table (REDIS_ENABLED is false). Games "
        "survive restarts, but the per-game lock is in this process, so this must be "
        "the ONLY instance serving the app."
    )
    from ...services import state_service
    from ...services.state_backend import SqlStateBackend

    backend = state_service.redis_client
    if isinstance(backend, SqlStateBackend):
        try:
            purged = backend.purge_expired_sync()
            if purged:
                logger.info("Purged %d expired live game rows.", purged)
        except Exception as exc:  # noqa: BLE001 — a down database must not block startup
            logger.warning("Could not purge expired live games (%s).", type(exc).__name__)
