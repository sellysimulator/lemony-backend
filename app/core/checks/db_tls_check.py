"""Startup check: say how the PostgreSQL connection is protected.

This logs; it never aborts. A `DB_SSL_CA` that cannot be loaded never reaches
this check: building the engine raises first, so the process does not start.
"""

from __future__ import annotations

import logging

from ...config import settings
from . import register_check

logger = logging.getLogger(__name__)


@register_check
def check_db_tls() -> None:
    """Log at WARNING unless the server is verified against `DB_SSL_CA`."""
    if not settings.is_postgres:
        return
    if settings.DB_SSL_CA.strip():
        logger.info(
            "PostgreSQL TLS is required and verified against DB_SSL_CA "
            "(sslmode=verify-full)."
        )
    elif settings.DB_REQUIRE_SSL:
        logger.warning(
            "DB_SSL_CA is not set: PostgreSQL connections are encrypted "
            "(sslmode=require), but the server certificate is NOT verified."
        )
    else:
        logger.warning(
            "DB_REQUIRE_SSL is false and DB_SSL_CA is not set: PostgreSQL connections "
            "are not required to use TLS and fall back to plaintext if the server "
            "offers none."
        )
