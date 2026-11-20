"""Application settings, loaded from environment variables and `.env`."""

import os
import ssl
import tempfile
from functools import lru_cache
from typing import Any
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@lru_cache
def _ca_file(ca: str) -> str:
    """A path to the PEM file psycopg verifies the server against (``sslrootcert``).

    ``ca`` is PEM text -- with real newlines, or ``\\n``-escaped so it fits on
    one ``.env`` line -- or a path to a PEM file. PEM text is written once per
    value to a private temp file. Either form is parsed here, so a value that is
    neither raises at startup instead of falling back to an unverified link.
    """
    ca = ca.strip()
    try:
        if ca.startswith("-----BEGIN"):
            pem = ca.replace("\\n", "\n") + "\n"
            ssl.create_default_context(cadata=pem)
            handle, path = tempfile.mkstemp(suffix=".pem")
            with os.fdopen(handle, "w") as fh:
                fh.write(pem)
            return path
        ssl.create_default_context(cafile=ca)
        return ca
    except OSError as exc:  # ssl.SSLError is an OSError too
        raise ValueError(
            "DB_SSL_CA is neither a valid PEM certificate nor a readable PEM file"
        ) from exc


class Settings(BaseSettings):
    """Environment-driven configuration for the Lemony backend."""

    APP_NAME: str = "Lemony"
    VERSION: str = "0.1.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""
    DB_DATABASE: str = "lemonydb"
    # TLS to PostgreSQL is required unless switched off (a local server without
    # TLS). DB_SSL_CA is the PostgreSQL service's CA certificate -- the PEM
    # text itself, or a path to a PEM file -- and adds certificate and hostname
    # verification (sslmode=verify-full); without it the link is encrypted
    # (sslmode=require) but the server is not authenticated.
    DB_REQUIRE_SSL: bool = True
    DB_SSL_CA: str = ""
    # Connections per instance: DB_POOL_SIZE kept open plus DB_MAX_OVERFLOW on
    # demand (4). Lemony's share of the shared 20-connection Aiven service (budget:
    # CLAUDE.md, Deploy); raise both on a bigger plan.
    DB_POOL_SIZE: int = 3
    DB_MAX_OVERFLOW: int = 1
    # Full SQLAlchemy URL that replaces the DB_* assembly. Used by the test
    # suite (SQLite); leave empty in every real deployment.
    DB_URL_OVERRIDE: str = ""

    # Off by default: live game documents are then kept in the PostgreSQL
    # `live_games` table, so a restart does not lose games in progress.
    REDIS_ENABLED: bool = False
    REDIS_URL: str = ""

    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # Live game documents expire this long after their last save.
    GAME_TTL_SECONDS: int = 7 * 24 * 3600
    MAX_DISPLAY_NAME_LENGTH: int = 24

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _reject_wildcard_origin(cls, value: list[str]) -> list[str]:
        """Never allow ``"*"``: the app sets ``allow_credentials=True``."""
        if "*" in value:
            raise ValueError(
                'CORS_ORIGINS must never contain "*": this app sets '
                "allow_credentials=True, and the two together are an "
                "invalid, permissive combination."
            )
        return value

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL for the database (credentials percent-encoded)."""
        if self.DB_URL_OVERRIDE:
            return self.DB_URL_OVERRIDE
        user = quote(self.DB_USER, safe="")
        password = quote(self.DB_PASSWORD, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}@{self.DB_HOST}:{self.DB_PORT}"
            f"/{self.DB_DATABASE}"
        )

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.db_url.startswith("postgresql")

    @property
    def db_connect_args(self) -> dict[str, Any]:
        """psycopg connect args: UTC sessions, and TLS unless ``DB_REQUIRE_SSL`` is false.

        ``options=-c timezone=UTC`` makes every session hand timestamps back in
        UTC. With ``DB_SSL_CA`` the server certificate and host name are verified
        (``sslmode=verify-full``); without it the link is encrypted but the server
        is not authenticated (``sslmode=require``). Any other URL (the suite's
        SQLite) gets no args.
        """
        if not self.is_postgres:
            return {}
        args: dict[str, Any] = {"options": "-c timezone=UTC"}
        if self.DB_SSL_CA.strip():
            args |= {"sslmode": "verify-full", "sslrootcert": _ca_file(self.DB_SSL_CA)}
        elif self.DB_REQUIRE_SSL:
            args |= {"sslmode": "require"}
        return args


settings = Settings()
