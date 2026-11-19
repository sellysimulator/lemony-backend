"""The PostgreSQL connection: ``Settings.db_url``, ``Settings.db_connect_args`` (UTC
sessions, TLS) and the boot log."""

import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from sqlalchemy.engine import make_url

from app.config import Settings
from app.core.checks import db_tls_check

# A throwaway self-signed CA (its key was discarded). Only its parseability
# matters: no connection is ever verified against it.
_TEST_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIBmDCCAT2gAwIBAgIUGXD542pZtQa9SLnAxCsSm1qTBHwwCgYIKoZIzj0EAwIw
GDEWMBQGA1UEAwwNTm92dXMgdGVzdCBDQTAgFw0yNjEwMDIyMDU2NTZaGA8yMTI2
MDkwODIwNTY1NlowGDEWMBQGA1UEAwwNTm92dXMgdGVzdCBDQTBZMBMGByqGSM49
AgEGCCqGSM49AwEHA0IABHCAFZW6AwimQZTLzv3AWFqneF3rOhSXVy6aK0h6Fo5U
J20/SnI5CKMQwuqY6YSC2kuzF8UHqaEHiCLfyL7WFXyjYzBhMB0GA1UdDgQWBBQ8
FdhkXQM4IflDyllXsNm5kjFtMDAfBgNVHSMEGDAWgBQ8FdhkXQM4IflDyllXsNm5
kjFtMDAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAKBggqhkjOPQQD
AgNJADBGAiEAiY0rLhkAr086mBgEi/LBgn23cS3BLxLUsjLph2QblP8CIQCkPZEJ
KZ781IJHaBK1rqXuL8CPgjD3c3nx86yO47mExA==
-----END CERTIFICATE-----
"""

# Every form DB_SSL_CA accepts: a path, the PEM text as Render stores it, and
# the one-line `\n`-escaped text a dotenv file holds.
_CA_FORMS = ["path", "pem", "pem-one-line"]


def _pg_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Settings from declared defaults only, pointed at PostgreSQL rather than the
    suite's SQLite override."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _ca_value(form: str, tmp_path: Path) -> str:
    if form == "path":
        ca = tmp_path / "ca.pem"
        ca.write_text(_TEST_CA_PEM)
        return str(ca)
    if form == "pem":
        return _TEST_CA_PEM
    if form == "pem-one-line":
        return _TEST_CA_PEM.strip().replace("\n", "\\n")
    return ""


def test_url_is_psycopg_with_the_postgres_defaults(monkeypatch):
    url = make_url(_pg_settings(monkeypatch).db_url)
    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.host, url.port, url.database) == (
        "postgres",
        "localhost",
        5432,
        "lemony",
    )


@pytest.mark.parametrize("password", ["p@ss:w/rd%41?#", "100%", "plain"])
def test_credentials_are_percent_encoded(monkeypatch, password):
    s = _pg_settings(monkeypatch, DB_USER="app@lemony", DB_PASSWORD=password, DB_HOST="db.x")
    raw = s.db_url.split("://", 1)[1].rsplit("@", 1)[0]
    user, encoded = raw.split(":", 1)
    assert "@" not in raw and unquote(user) == "app@lemony" and unquote(encoded) == password
    url = make_url(s.db_url)
    assert (url.username, url.password, url.host) == ("app@lemony", password, "db.x")


def test_tls_is_required_by_default(monkeypatch):
    s = _pg_settings(monkeypatch)
    assert s.DB_REQUIRE_SSL is True
    assert s.db_connect_args == {"options": "-c timezone=UTC", "sslmode": "require"}


def test_sessions_always_run_in_utc(monkeypatch):
    s = _pg_settings(monkeypatch, DB_REQUIRE_SSL=False)
    assert s.db_connect_args == {"options": "-c timezone=UTC"}


@pytest.mark.parametrize(
    "url", ["sqlite:///x.db", "sqlite://", "postgres-not-a-url", "oracle://u:p@h/d"]
)
def test_non_postgres_urls_get_no_connect_args(monkeypatch, url):
    assert _pg_settings(monkeypatch, DB_URL_OVERRIDE=url).db_connect_args == {}


def test_a_postgres_override_gets_the_postgres_args(monkeypatch):
    s = _pg_settings(monkeypatch, DB_URL_OVERRIDE="postgresql+psycopg://u:p@h:5432/d")
    assert s.db_url == "postgresql+psycopg://u:p@h:5432/d"
    assert s.db_connect_args["options"] == "-c timezone=UTC"


@pytest.mark.parametrize("form", _CA_FORMS)
@pytest.mark.parametrize("require", [True, False])
def test_with_a_ca_the_server_is_verified_against_it(monkeypatch, tmp_path, form, require):
    s = _pg_settings(monkeypatch, DB_SSL_CA=_ca_value(form, tmp_path), DB_REQUIRE_SSL=require)
    args = s.db_connect_args
    assert args["sslmode"] == "verify-full"
    assert args["options"] == "-c timezone=UTC"
    root = Path(args["sslrootcert"])
    assert root.is_file()
    assert root.read_text().strip() == _TEST_CA_PEM.strip()
    if form == "path":
        assert root == tmp_path / "ca.pem"  # a path is passed through as is


def test_pem_text_is_written_once_per_value(monkeypatch):
    s = _pg_settings(monkeypatch, DB_SSL_CA=_TEST_CA_PEM)
    assert s.db_connect_args["sslrootcert"] == s.db_connect_args["sslrootcert"]


@pytest.mark.parametrize(
    "bad",
    [
        "/nonexistent/ca.pem",
        "-----BEGIN CERTIFICATE-----\\nnot a certificate\\n-----END CERTIFICATE-----",
    ],
)
def test_an_unloadable_ca_fails_loudly(monkeypatch, bad):
    s = _pg_settings(monkeypatch, DB_SSL_CA=bad)
    with pytest.raises(ValueError, match="DB_SSL_CA"):
        _ = s.db_connect_args


@pytest.mark.parametrize(
    ("require", "ca", "level", "text"),
    [
        (False, "", logging.WARNING, "DB_REQUIRE_SSL is false"),
        (True, "", logging.WARNING, "NOT verified"),
        (True, _TEST_CA_PEM, logging.INFO, "verified against DB_SSL_CA"),
        (False, _TEST_CA_PEM, logging.INFO, "verified against DB_SSL_CA"),
    ],
)
def test_boot_log_says_how_the_db_link_is_protected(monkeypatch, caplog, require, ca, level, text):
    monkeypatch.setattr(db_tls_check.settings, "DB_URL_OVERRIDE", "")
    monkeypatch.setattr(db_tls_check.settings, "DB_REQUIRE_SSL", require)
    monkeypatch.setattr(db_tls_check.settings, "DB_SSL_CA", ca)
    with caplog.at_level(logging.INFO, logger=db_tls_check.logger.name):
        db_tls_check.check_db_tls()
    [record] = [r for r in caplog.records if r.name == db_tls_check.logger.name]
    assert record.levelno == level
    assert text in record.getMessage()
    assert "PostgreSQL" in record.getMessage()


def test_boot_log_is_silent_on_sqlite(caplog):
    with caplog.at_level(logging.INFO, logger=db_tls_check.logger.name):
        db_tls_check.check_db_tls()
    assert not [r for r in caplog.records if r.name == db_tls_check.logger.name]


def test_pool_is_sized_for_a_20_connection_plan(monkeypatch):
    s = _pg_settings(monkeypatch)
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (3, 1)
    # One shared 20-connection service: Selly, Pulky and Beery at 3 each, Lemony,
    # plus a deploy's second instance of the largest (4), alembic and one psql (2).
    lemony = s.DB_POOL_SIZE + s.DB_MAX_OVERFLOW
    assert 3 * 3 + lemony + max(3, lemony) + 2 <= 20


def test_pool_size_comes_from_the_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_POOL_SIZE", "12")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (12, 4)
