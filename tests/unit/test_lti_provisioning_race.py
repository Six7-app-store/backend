"""Regression tests for H-8: concurrent first-launches must not 500.

``provision_user`` and ``record_context`` follow a read-check-then-insert
pattern. Two simultaneous first-launches both pass the read check, both
insert, and the second one hits the unique constraint. Before the fix that
propagated as an unhandled ``IntegrityError`` (HTTP 500). After the fix
the loser rolls back and re-fetches the winner's row.

These tests are DB-less: they patch the session to inject an
``IntegrityError`` at commit time and verify that the functions recover
gracefully rather than re-raising.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import LtiContext, User, UserIdentity, UserRole
from app.services.lti_service import LaunchIdentity, provision_user, record_context

pytestmark = pytest.mark.unit

ISSUER = "https://moodle.test"


def _identity(**overrides):
    base = {
        "issuer": ISSUER,
        "subject": "sub-abc",
        "email": "anna@dhbw.de",
        "first_name": "Anna",
        "last_name": "Müller",
        "context_id": None,
        "context_title": None,
        "context_label": None,
        "roles": [],
        "deployment_id": "1",
    }
    base.update(overrides)
    return LaunchIdentity(**base)


# ----------------------------------------------------------------
# provision_user — IntegrityError recovery
# ----------------------------------------------------------------
def test_provision_user_recovers_from_concurrent_insert():
    """When the commit raises IntegrityError (concurrent first-launch),
    provision_user must roll back and return the winner's User row."""
    existing_user = MagicMock(spec=User)
    existing_user.userId = uuid.uuid4()
    existing_user.role = UserRole.STUDENT
    existing_user.firstName = "Anna"
    existing_user.lastName = "Müller"

    existing_link = MagicMock(spec=UserIdentity)
    existing_link.user = existing_user

    db = MagicMock()
    # First query (UserIdentity lookup) → no existing link yet
    # Second query (re-fetch after rollback) → the winning row
    db.query.return_value.filter.return_value.first.side_effect = [
        None,       # initial link lookup → not found
        existing_link,  # re-fetch after rollback → winner's row
    ]
    db.commit.side_effect = [IntegrityError("unique", {}, None), None]

    identity = _identity()
    result = provision_user(db, identity)

    db.rollback.assert_called_once()
    assert result is existing_user


def test_provision_user_reraises_if_refetch_returns_nothing():
    """If rollback + re-fetch finds no row (shouldn't happen in practice),
    the original IntegrityError propagates rather than hiding it."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [
        None,  # initial link lookup
        None,  # re-fetch after rollback — nothing found
    ]
    db.commit.side_effect = IntegrityError("unique", {}, None)

    with pytest.raises(IntegrityError):
        provision_user(db, _identity())

    db.rollback.assert_called_once()


# ----------------------------------------------------------------
# record_context — IntegrityError recovery
# ----------------------------------------------------------------
def test_record_context_recovers_from_concurrent_insert():
    """Same pattern for record_context: IntegrityError → rollback →
    re-fetch wins."""
    existing_ctx = MagicMock(spec=LtiContext)
    existing_ctx.issuer = ISSUER
    existing_ctx.context_id = "ctx-42"

    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [
        None,           # initial context lookup → not found
        existing_ctx,   # re-fetch after rollback
    ]
    db.commit.side_effect = [IntegrityError("unique", {}, None), None]

    identity = _identity(context_id="ctx-42", context_title="Cloud", context_label="CC")
    result = record_context(db, identity)

    db.rollback.assert_called_once()
    assert result is existing_ctx


def test_record_context_skips_when_no_context_id():
    """record_context returns None immediately when the launch has no
    context_id — no DB calls, no crash."""
    db = MagicMock()
    result = record_context(db, _identity(context_id=None))
    assert result is None
    db.query.assert_not_called()
