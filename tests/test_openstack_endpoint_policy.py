"""Keystone endpoint restrictions apply before an SDK request is made."""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import OpenStackAuthType
from app.schemas import OpenStackCredentialUpsert
from app.services.openstack_validator import validate
from app.utils.openstack_endpoints import ensure_allowed_auth_url


@pytest.mark.unit
def test_blocked_url_never_reaches_openstack(monkeypatch):
    monkeypatch.setattr(settings, "OPENSTACK_ALLOWED_AUTH_URLS", ["https://keystone.example/v3"])
    payload = OpenStackCredentialUpsert(
        auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
        auth_url="https://keystone.example@169.254.169.254/v3",
        identifier="user", secret="secret",
    )
    with (
        patch("app.services.openstack_validator.openstack.connect") as connect,
        pytest.raises(HTTPException) as exc,
    ):
        validate(payload)
    assert exc.value.status_code == 422
    connect.assert_not_called()


@pytest.mark.unit
def test_exact_endpoint_and_fail_closed_policy(monkeypatch):
    monkeypatch.setattr(settings, "OPENSTACK_ALLOWED_AUTH_URLS", ["https://keystone.example:5000/v3"])
    ensure_allowed_auth_url("https://keystone.example:5000/v3/")
    for url in ("http://keystone.example:5000/v3", "https://keystone.example:5001/v3", "https://keystone.example:5000/v3?x=1"):
        with pytest.raises(HTTPException):
            ensure_allowed_auth_url(url)
    monkeypatch.setattr(settings, "OPENSTACK_ENFORCE_ALLOWED_AUTH_URLS", True)
    monkeypatch.setattr(settings, "OPENSTACK_ALLOWED_AUTH_URLS", [])
    with pytest.raises(HTTPException) as exc:
        ensure_allowed_auth_url("https://keystone.example/v3")
    assert exc.value.status_code == 503
