"""Application settings, loaded from environment variables and `.env`."""

import ssl
from typing import Any
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _db_ssl_context(ca: str) -> ssl.SSLContext:
    """TLS context for PyMySQL, verifying the server against ``ca`` if given.

    ``ca`` is PEM text -- with real newlines, or ``\\n``-escaped so it fits on
    one ``.env`` line -- or a path to a PEM file. A value that is neither
    raises instead of falling back to an unverified connection.
    """
    ca = ca.strip()
    if not ca:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        if ca.startswith("-----BEGIN"):
            return ssl.create_default_context(cadata=ca.replace("\\n", "\n"))
        return ssl.create_default_context(cafile=ca)
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
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_DATABASE: str = "lemony"
    # TLS to MySQL is required unless switched off (local MySQL without TLS).
    # DB_SSL_CA is this project's MySQL CA certificate -- the PEM text itself,
    # or a path to a PEM file -- and adds certificate and hostname
    # verification; without it the link is encrypted but the server is not
    # authenticated.
    DB_REQUIRE_SSL: bool = True
    DB_SSL_CA: str = ""
    # Full SQLAlchemy URL that replaces the DB_* assembly. Used by the test
    # suite (SQLite); leave empty in every real deployment.
    DB_URL_OVERRIDE: str = ""

    # Off by default: live game documents are then kept in the MySQL
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
        return f"mysql+pymysql://{user}:{password}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")

    @property
    def db_connect_args(self) -> dict[str, Any]:
        """Driver connect args; TLS unless ``DB_REQUIRE_SSL`` is false.

        TLS is passed as an ``SSLContext`` because that is what puts PyMySQL in
        REQUIRED mode. An empty ``ssl`` dict is falsy, so PyMySQL treats it as
        no TLS options at all and silently downgrades to plaintext when the
        server does not offer TLS.
        """
        if self.is_sqlite:
            return {"check_same_thread": False}
        if not self.DB_REQUIRE_SSL:
            return {}
        return {"ssl": _db_ssl_context(self.DB_SSL_CA)}


settings = Settings()
