"""Unit tests for the ``git_link`` validator on :class:`app.schemas.AppBase`.

DB-less: exercises the Pydantic field_validator directly.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import AppCreate

pytestmark = pytest.mark.unit


def _app(**overrides):
    base = {"name": "my-app", "description": "test"}
    base.update(overrides)
    return base


# ----------------------------------------------------------------
# Valid URLs
# ----------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://github.com/org/repo.git",
    "https://gitlab.com/org/repo",
    "git@github.com:org/repo.git",
    "git@gitlab.example.com:org/repo.git",
    None,
])
def test_valid_git_link_accepted(url):
    obj = AppCreate(**_app(git_link=url))
    assert obj.git_link == url


# ----------------------------------------------------------------
# Blocked: plain HTTP
# ----------------------------------------------------------------
def test_http_url_rejected():
    with pytest.raises(ValidationError, match="HTTPS"):
        AppCreate(**_app(git_link="http://github.com/org/repo.git"))


# ----------------------------------------------------------------
# Blocked: private / internal IP ranges
# ----------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://127.0.0.1/org/repo.git",
    "https://10.0.0.1/org/repo.git",
    "https://192.168.1.1/org/repo.git",
    "https://172.16.0.5/org/repo.git",
    "https://169.254.169.254/latest/meta-data/",
])
def test_private_ip_git_link_rejected(url):
    with pytest.raises(ValidationError, match="private|internal"):
        AppCreate(**_app(git_link=url))


# ----------------------------------------------------------------
# Blocked: unknown/non-http scheme
# ----------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "ftp://github.com/org/repo.git",
    "file:///etc/passwd",
])
def test_non_https_scheme_rejected(url):
    with pytest.raises(ValidationError):
        AppCreate(**_app(git_link=url))
