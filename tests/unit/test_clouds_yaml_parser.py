"""Unit tests for :mod:`app.services.clouds_yaml_parser`.

DB-less: the parser is pure (raw YAML in, Pydantic schema out), so no
fixtures from the integration conftest are touched. All error paths
surface as :class:`fastapi.HTTPException` with status 422 — the parser
never lets a raw :class:`yaml.YAMLError` escape.
"""
from __future__ import annotations

import textwrap

import pytest
from fastapi import HTTPException

from app.models import OpenStackAuthType
from app.schemas import OpenStackCredentialUpsert
from app.services.clouds_yaml_parser import parse

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
_PASSWORD_YAML = textwrap.dedent(
    """
    clouds:
      mycloud:
        region_name: RegionOne
        interface: public
        identity_api_version: 3
        auth:
          auth_url: https://keystone.example.com:5000/v3
          username: alice
          password: s3cret
          project_id: proj-abc
          project_name: demo
          user_domain_name: Default
          project_domain_name: Default
    """
).strip()

_MULTI_CLOUD_YAML = textwrap.dedent(
    """
    clouds:
      alpha:
        auth_type: password
        region_name: RegionOne
        auth:
          auth_url: https://alpha.example.com:5000/v3
          username: alice
          password: s3cret
          project_name: demo
          user_domain_name: Default
      beta:
        auth_type: password
        region_name: RegionTwo
        auth:
          auth_url: https://beta.example.com:5000/v3
          username: bob
          password: hunter2
          project_id: beta-proj
          user_domain_name: Default
    """
).strip()

_APP_CRED_YAML = textwrap.dedent(
    """
    clouds:
      mycloud:
        auth_type: v3applicationcredential
        region_name: RegionOne
        interface: internal
        identity_api_version: 3
        auth:
          auth_url: https://keystone.example.com:5000/v3
          application_credential_id: cred-id-123
          application_credential_secret: cred-secret-xyz
    """
).strip()


# ----------------------------------------------------------------
# Tests
# ----------------------------------------------------------------
def test_parse_canonical_clouds_yaml_with_password_auth():
    """A single-cloud password file with no explicit auth_type yields a
    PASSWORD upsert with all common fields propagated."""
    result = parse(_PASSWORD_YAML)

    assert isinstance(result, OpenStackCredentialUpsert)
    assert result.auth_type == OpenStackAuthType.PASSWORD
    assert result.auth_url == "https://keystone.example.com:5000/v3"
    assert result.identifier == "alice"
    assert result.secret == "s3cret"
    assert result.region_name == "RegionOne"
    assert result.interface == "public"
    assert result.identity_api_version == "3"
    assert result.project_id == "proj-abc"
    assert result.project_name == "demo"
    assert result.user_domain_name == "Default"
    assert result.project_domain_name == "Default"


def test_parse_picks_named_cloud_from_multi_cloud_file():
    """When the YAML defines multiple clouds, the explicit cloud_name
    selects the right block and ignores the others."""
    result = parse(_MULTI_CLOUD_YAML, cloud_name="beta")

    assert result.auth_type == OpenStackAuthType.PASSWORD
    assert result.auth_url == "https://beta.example.com:5000/v3"
    assert result.identifier == "bob"
    assert result.secret == "hunter2"
    assert result.region_name == "RegionTwo"
    assert result.project_id == "beta-proj"


def test_parse_missing_clouds_root_raises():
    """A YAML doc without a top-level `clouds:` mapping is rejected."""
    yaml_blob = "not_clouds:\n  foo: bar\n"

    with pytest.raises(HTTPException) as exc_info:
        parse(yaml_blob)

    assert exc_info.value.status_code == 422
    assert "clouds" in exc_info.value.detail


def test_parse_unknown_cloud_name_raises():
    """Selecting a cloud name that is not in the file lists the
    available names in the error so the UI can render them."""
    with pytest.raises(HTTPException) as exc_info:
        parse(_MULTI_CLOUD_YAML, cloud_name="gamma")

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert "gamma" in detail
    assert "alpha" in detail
    assert "beta" in detail


def test_parse_application_credential_auth_flow():
    """An application-credential cloud maps the credential id/secret
    onto identifier/secret and uses APPLICATION_CREDENTIAL."""
    result = parse(_APP_CRED_YAML)

    assert result.auth_type == OpenStackAuthType.APPLICATION_CREDENTIAL
    assert result.identifier == "cred-id-123"
    assert result.secret == "cred-secret-xyz"
    assert result.auth_url == "https://keystone.example.com:5000/v3"
    assert result.interface == "internal"
    # application-credential auth need not carry project/domain info
    assert result.project_id is None
    assert result.user_domain_name is None


def test_parse_rejects_unsupported_auth_type():
    """Only `password` and `v3applicationcredential` are accepted —
    anything else raises a 422 listing the supported types."""
    yaml_blob = textwrap.dedent(
        """
        clouds:
          mycloud:
            auth_type: v3token
            auth:
              auth_url: https://keystone.example.com:5000/v3
              token: abc
        """
    ).strip()

    with pytest.raises(HTTPException) as exc_info:
        parse(yaml_blob)

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert "v3token" in detail
    assert "password" in detail
    assert "v3applicationcredential" in detail


def test_parse_yaml_syntax_error_propagates_as_value_error():
    """A malformed YAML blob is caught and re-raised as a 422
    HTTPException whose detail starts with `Invalid YAML:` — the raw
    :class:`yaml.YAMLError` never reaches the caller."""
    yaml_blob = "clouds:\n  mycloud: [unterminated\n"

    with pytest.raises(HTTPException) as exc_info:
        parse(yaml_blob)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail.startswith("Invalid YAML:")


# ----------------------------------------------------------------
# Remaining error branches
#
# Every one of these ends in ``_bad()`` and therefore a 422 whose detail
# the UI renders verbatim. That is the whole contract of this parser, so
# each branch is asserted on its message, not just on the status code.
# ----------------------------------------------------------------
def test_parse_empty_clouds_mapping_raises():
    """``clouds:`` present but empty is a different mistake than a missing root."""
    with pytest.raises(HTTPException) as exc:
        parse("clouds: {}")

    assert exc.value.status_code == 422
    assert "no clouds defined" in exc.value.detail


def test_parse_multi_cloud_without_name_lists_the_choices():
    """Ambiguity must name the options - the user cannot guess our iteration order."""
    yaml_text = textwrap.dedent(
        """
        clouds:
          zeta:
            auth_type: password
            auth:
              auth_url: https://keystone.example/v3
              username: u
              password: p
              user_domain_name: Default
              project_name: demo
          alpha:
            auth_type: password
            auth:
              auth_url: https://keystone.example/v3
              username: u
              password: p
              user_domain_name: Default
              project_name: demo
        """
    )

    with pytest.raises(HTTPException) as exc:
        parse(yaml_text)

    assert exc.value.status_code == 422
    assert "specify cloud_name" in exc.value.detail
    # sorted(), so the order is stable regardless of YAML order
    assert "alpha, zeta" in exc.value.detail


def test_parse_cloud_entry_that_is_not_a_mapping_raises():
    """A scalar under the cloud name would otherwise blow up on ``.get``."""
    with pytest.raises(HTTPException) as exc:
        parse("clouds:\n  mycloud: just-a-string\n")

    assert exc.value.status_code == 422
    assert "is not a mapping" in exc.value.detail


def test_parse_missing_auth_block_raises():
    """No ``auth:`` at all - named separately from a malformed one."""
    with pytest.raises(HTTPException) as exc:
        parse("clouds:\n  mycloud:\n    region_name: RegionOne\n")

    assert exc.value.status_code == 422
    assert "missing 'auth:' block" in exc.value.detail


def test_parse_missing_auth_url_raises():
    """``auth_url`` is the one field with no sensible default."""
    yaml_text = textwrap.dedent(
        """
        clouds:
          mycloud:
            auth_type: password
            auth:
              username: alice
              password: s3cret
        """
    )

    with pytest.raises(HTTPException) as exc:
        parse(yaml_text)

    assert exc.value.status_code == 422
    assert "auth.auth_url is required" in exc.value.detail


def test_parse_application_credential_without_secret_raises():
    """Horizon writes both fields; half of them means a truncated paste."""
    yaml_text = textwrap.dedent(
        """
        clouds:
          mycloud:
            auth_type: v3applicationcredential
            auth:
              auth_url: https://keystone.example/v3
              application_credential_id: abc123
        """
    )

    with pytest.raises(HTTPException) as exc:
        parse(yaml_text)

    assert exc.value.status_code == 422
    assert "application_credential_secret" in exc.value.detail


def test_parse_password_auth_without_password_raises():
    """Same for the password flow - Horizon never fills in the password."""
    yaml_text = textwrap.dedent(
        """
        clouds:
          mycloud:
            auth_type: password
            auth:
              auth_url: https://keystone.example/v3
              username: alice
              user_domain_name: Default
              project_name: demo
        """
    )

    with pytest.raises(HTTPException) as exc:
        parse(yaml_text)

    assert exc.value.status_code == 422
    assert "auth.username and auth.password" in exc.value.detail


def test_parse_translates_a_schema_rejection_into_the_same_422():
    """A Pydantic ValueError must not escape as a 500.

    Password auth needs a project and a user domain. The YAML below is
    well-formed and passes every check in this module, so the rejection
    can only come from ``OpenStackCredentialUpsert`` - and it has to
    arrive as the same 422 with a readable detail.
    """
    yaml_text = textwrap.dedent(
        """
        clouds:
          mycloud:
            auth_type: password
            auth:
              auth_url: https://keystone.example/v3
              username: alice
              password: s3cret
        """
    )

    with pytest.raises(HTTPException) as exc:
        parse(yaml_text)

    assert exc.value.status_code == 422
    assert exc.value.detail
