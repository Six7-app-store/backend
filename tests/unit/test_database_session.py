"""Unit tests for the ``get_db`` session dependency.

``SessionLocal`` is patched, so no database is involved. What matters
here is not the query path but the contract FastAPI relies on: exactly
one session per request, and it is closed afterwards - including when
the handler raises. A leaked session holds a pooled connection, and the
pool in :mod:`app.database` is only ``pool_size=5`` plus ten overflow.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import database

pytestmark = pytest.mark.unit


def test_get_db_yields_the_session_and_closes_it():
    """One session, handed out once, closed when the generator is exhausted."""
    session = MagicMock()

    with patch.object(database, "SessionLocal", return_value=session) as factory:
        gen = database.get_db()
        handed_out = next(gen)

        assert handed_out is session
        factory.assert_called_once_with()
        session.close.assert_not_called()

        with pytest.raises(StopIteration):
            next(gen)

    session.close.assert_called_once_with()


def test_get_db_closes_the_session_when_the_consumer_raises():
    """The ``finally`` must hold - otherwise a failing request leaks a connection."""
    session = MagicMock()

    with patch.object(database, "SessionLocal", return_value=session):
        gen = database.get_db()
        next(gen)

        with pytest.raises(RuntimeError, match="handler blew up"):
            gen.throw(RuntimeError("handler blew up"))

    session.close.assert_called_once_with()


def test_get_db_closes_the_session_when_the_request_is_abandoned():
    """Closing the generator early (client disconnect) still runs the cleanup."""
    session = MagicMock()

    with patch.object(database, "SessionLocal", return_value=session):
        gen = database.get_db()
        next(gen)
        gen.close()

    session.close.assert_called_once_with()
