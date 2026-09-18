"""Unit test for the public auth health endpoint.

DB-less and deliberately so: the handler exists to be probed *without*
authenticating and without a database, so an operator can tell "auth is
wired up" apart from "the DB is down". Calling the function directly is
the honest test - routing it through a TestClient would drag in the app
lifespan and the very dependencies this endpoint is meant to sidestep.
"""
from __future__ import annotations

import pytest

from app.routers.auth_keycloak import auth_health, router

pytestmark = pytest.mark.unit


def test_auth_health_reports_keycloak_as_the_auth_method():
    """The payload is contract: operators and probes read these keys."""
    body = auth_health()

    assert body["status"] == "healthy"
    assert body["auth_method"] == "keycloak"
    assert body["message"]


def test_auth_health_is_registered_as_an_unauthenticated_get():
    """No dependencies on the route - anything else defeats its purpose."""
    routes = [r for r in router.routes if getattr(r, "path", None) == "/health"]

    assert len(routes) == 1
    route = routes[0]
    assert "GET" in route.methods
    assert route.dependant.dependencies == []
