"""Unit tests for ``app.services.email_service``.

Two layers are exercised here without any SMTP socket I/O:

* ``is_smtp_enabled()`` — the predicate the resend-access endpoint
  consults to decide between 503 (configuration) and 502 (delivery
  failure). The truth table needs to be locked down because both
  ``SMTP_ENABLED`` and the credentials must be present; flipping
  either independently was previously possible and produced
  surprising states (mail attempted with empty creds, or mail
  silently off while creds were populated).
* ``send_email()`` — the kill-switch must short-circuit BEFORE any
  ``smtplib`` access. We patch ``smtplib.SMTP_SSL`` / ``smtplib.SMTP``
  and assert the constructor is never called when the gate is off.
"""
from __future__ import annotations

import smtplib
from unittest.mock import patch

import pytest

from app.config import settings
from app.services import email_service


# ----------------------------------------------------------------
# is_smtp_enabled — truth table
# ----------------------------------------------------------------
@pytest.fixture
def smtp_settings(monkeypatch):
    """Set every SMTP knob explicitly so the container's env can't leak in."""

    def apply(**values):
        defaults = {
            "SMTP_ENABLED": True,
            "SMTP_HOST": "mail.example.org",
            "SMTP_PORT": 587,
            "SMTP_SECURITY": "auto",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM_EMAIL": "",
        }
        defaults.update(values)
        for key, value in defaults.items():
            monkeypatch.setattr(settings, key, value, raising=False)

    return apply


@pytest.mark.parametrize(
    "enabled,host,user,password,from_email,expected",
    [
        # Account with credentials, sender falls back to the login.
        (True, "mail.example.org", "u@example.com", "secret", "", True),
        # Relay that admits the platform by address: no credentials,
        # only an explicit sender.
        (True, "mail.example.org", "", "", "noreply@example.org", True),
        # Kill-switch overrides everything else — operator chose "off".
        (False, "mail.example.org", "u@example.com", "secret", "", False),
        # No host: nothing to connect to.
        (True, "", "u@example.com", "secret", "", False),
        # No credentials and no sender: nothing to put in From.
        (True, "mail.example.org", "", "", "", False),
        # Half a credential pair is "configuration in progress"; we
        # treat it as off so the resend endpoint returns 503 instead of
        # a 502 at submit-time auth failure.
        (True, "mail.example.org", "", "secret", "noreply@example.org", False),
        (True, "mail.example.org", "u@example.com", "", "", False),
    ],
)
def test_is_smtp_enabled_truth_table(
    smtp_settings, enabled, host, user, password, from_email, expected
):
    smtp_settings(
        SMTP_ENABLED=enabled,
        SMTP_HOST=host,
        SMTP_USER=user,
        SMTP_PASSWORD=password,
        SMTP_FROM_EMAIL=from_email,
    )
    assert email_service.is_smtp_enabled() is expected


# ----------------------------------------------------------------
# send_email — short-circuit semantics
# ----------------------------------------------------------------
def test_send_email_no_op_when_disabled(monkeypatch):
    """When ``SMTP_ENABLED=False``, send_email must return False
    WITHOUT touching ``smtplib``. We patch both transports — neither
    should be constructed. This guards the kill-switch against a
    future refactor that accidentally moves the gate AFTER the
    connection attempt (where it would still leak DNS lookups / TCP
    connects to ``smtp.gmail.com`` in air-gapped environments).
    """
    monkeypatch.setattr(settings, "SMTP_ENABLED", False, raising=False)
    # Credentials populated to prove the kill-switch wins regardless.
    monkeypatch.setattr(settings, "SMTP_USER", "u@example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret", raising=False)

    with patch("smtplib.SMTP_SSL") as ssl_cls, patch("smtplib.SMTP") as plain_cls:
        result = email_service.send_email(
            to="recipient@example.com",
            subject="hi",
            html_body="<p>hi</p>",
            text_body="hi",
        )

    assert result is False
    ssl_cls.assert_not_called()
    plain_cls.assert_not_called()


def test_send_email_no_op_when_credentials_missing(smtp_settings):
    """``SMTP_ENABLED=True`` but nothing to send as, or half a
    credential pair, must also be a no-op.
    """
    for values in (
        {"SMTP_USER": "", "SMTP_PASSWORD": "", "SMTP_FROM_EMAIL": ""},
        {"SMTP_USER": "u@example.com", "SMTP_PASSWORD": ""},
    ):
        smtp_settings(**values)
        with patch("smtplib.SMTP_SSL") as ssl_cls, patch("smtplib.SMTP") as plain_cls:
            result = email_service.send_email(
                to="recipient@example.com",
                subject="hi",
                html_body="<p>hi</p>",
                text_body="hi",
            )

        assert result is False
        ssl_cls.assert_not_called()
        plain_cls.assert_not_called()


# ----------------------------------------------------------------
# send_email — transport and authentication
# ----------------------------------------------------------------
def _conn(cls):
    """The connection object; ``with`` hands back the same one."""
    conn = cls.return_value
    conn.__enter__.return_value = conn
    return conn


def _send():
    return email_service.send_email(
        to="recipient@example.com",
        subject="hi",
        html_body="<p>hi</p>",
        text_body="hi",
    )


@pytest.mark.parametrize(
    "security,port,expected",
    [
        ("auto", 465, "ssl"),
        ("auto", 587, "starttls"),
        ("auto", 25, "starttls"),
        ("ssl", 2465, "ssl"),
        ("starttls", 465, "starttls"),
        ("none", 25, "none"),
    ],
)
def test_send_email_transport(smtp_settings, security, port, expected):
    smtp_settings(
        SMTP_SECURITY=security,
        SMTP_PORT=port,
        SMTP_USER="u@example.com",
        SMTP_PASSWORD="secret",
    )

    with patch("smtplib.SMTP_SSL") as ssl_cls, patch("smtplib.SMTP") as plain_cls:
        ssl_conn, plain_conn = _conn(ssl_cls), _conn(plain_cls)
        assert _send() is True

    if expected == "ssl":
        plain_cls.assert_not_called()
        conn = ssl_conn
        conn.starttls.assert_not_called()
    else:
        ssl_cls.assert_not_called()
        conn = plain_conn
        if expected == "starttls":
            conn.starttls.assert_called_once()
        else:
            conn.starttls.assert_not_called()
    conn.login.assert_called_once_with("u@example.com", "secret")
    conn.sendmail.assert_called_once()
    assert conn.sendmail.call_args.args[0] == "u@example.com"


def test_send_email_relay_without_login(smtp_settings):
    """A relay that admits the platform by address gets no AUTH at all,
    and the envelope sender is the configured From address.
    """
    smtp_settings(SMTP_PORT=25, SMTP_SECURITY="none", SMTP_FROM_EMAIL="noreply@example.org")

    with patch("smtplib.SMTP") as plain_cls:
        conn = _conn(plain_cls)
        assert _send() is True

    conn.login.assert_not_called()
    assert conn.sendmail.call_args.args[:2] == ("noreply@example.org", ["recipient@example.com"])


def test_send_email_closes_connection_when_login_fails(smtp_settings):
    """A refused login returns False and does not leak the socket."""
    smtp_settings(SMTP_USER="u@example.com", SMTP_PASSWORD="wrong")

    with patch("smtplib.SMTP") as plain_cls:
        plain_cls.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
        assert _send() is False

    plain_cls.return_value.close.assert_called_once()
    plain_cls.return_value.sendmail.assert_not_called()
