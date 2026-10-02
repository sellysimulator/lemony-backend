"""TLS on the MySQL connection: ``Settings.db_connect_args`` and the boot log."""

import logging
import ssl
from pathlib import Path
from typing import Any

import pymysql  # type: ignore[import-untyped]
import pytest
from pymysql.constants import CLIENT  # type: ignore[import-untyped]

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
# the one-line `\n`-escaped text a `.env` holds.
_CA_FORMS = ["path", "pem", "pem-one-line"]


def _mysql_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Settings from declared defaults only, pointed at MySQL rather than the
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


def test_tls_is_required_by_default(monkeypatch):
    s = _mysql_settings(monkeypatch)
    assert s.DB_REQUIRE_SSL is True
    assert isinstance(s.db_connect_args["ssl"], ssl.SSLContext)


def test_no_tls_args_when_switched_off(monkeypatch):
    assert _mysql_settings(monkeypatch, DB_REQUIRE_SSL=False).db_connect_args == {}


def test_sqlite_gets_no_tls_args(monkeypatch):
    s = _mysql_settings(monkeypatch, DB_URL_OVERRIDE="sqlite:///x.db")
    assert s.db_connect_args == {"check_same_thread": False}


def test_without_a_ca_the_link_is_encrypted_but_unverified(monkeypatch):
    ctx = _mysql_settings(monkeypatch).db_connect_args["ssl"]
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


@pytest.mark.parametrize("form", _CA_FORMS)
def test_with_a_ca_the_server_is_verified_against_it(monkeypatch, tmp_path, form):
    ctx = _mysql_settings(monkeypatch, DB_SSL_CA=_ca_value(form, tmp_path)).db_connect_args["ssl"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.cert_store_stats()["x509_ca"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        "/nonexistent/ca.pem",
        "-----BEGIN CERTIFICATE-----\\nnot a certificate\\n-----END CERTIFICATE-----",
    ],
)
def test_an_unloadable_ca_fails_loudly(monkeypatch, bad):
    s = _mysql_settings(monkeypatch, DB_SSL_CA=bad)
    with pytest.raises(ValueError, match="DB_SSL_CA"):
        _ = s.db_connect_args


@pytest.mark.parametrize("form", ["none", *_CA_FORMS])
def test_pymysql_refuses_plaintext_with_these_connect_args(monkeypatch, tmp_path, form):
    """PyMySQL sets ``CLIENT.SSL`` up front only in REQUIRED mode; in PREFERRED
    mode it silently drops to plaintext if the server offers no TLS. An empty
    ``ssl`` dict lands in PREFERRED mode, which is why the args carry an
    ``SSLContext``."""
    s = _mysql_settings(monkeypatch, DB_SSL_CA=_ca_value(form, tmp_path))
    conn = pymysql.connections.Connection(
        host="db.example.com", defer_connect=True, **s.db_connect_args
    )
    assert conn.client_flag & CLIENT.SSL


@pytest.mark.parametrize(
    ("require", "ca", "level", "text"),
    [
        (False, "", logging.WARNING, "DB_REQUIRE_SSL is false"),
        (True, "", logging.WARNING, "NOT verified"),
        (True, _TEST_CA_PEM, logging.INFO, "verified against DB_SSL_CA"),
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


def test_boot_log_is_silent_on_sqlite(caplog):
    with caplog.at_level(logging.INFO, logger=db_tls_check.logger.name):
        db_tls_check.check_db_tls()
    assert not [r for r in caplog.records if r.name == db_tls_check.logger.name]
