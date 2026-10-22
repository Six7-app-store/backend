"""Integration tests for the OpenTofu variable scan behind
``GET /apps/{id}/variables``.

The app contract is one ``tofu/`` directory (ADR 0010 in the deployment
repository). These tests pin what the endpoint makes of a release:

  * ``tofu/variables.tofu`` is parsed; ``users`` stays hidden because the
    worker injects it from the deployment's teams.
  * A release without ``tofu/variables.tofu`` — including one still on the
    old ``terraform/`` + ``packer/`` layout — is a 422 naming the contract,
    not an empty variable list that only fails at deploy time.
  * The response no longer carries ``source`` or ``template_key``.

Setup: every test materialises a small repo under ``tmp_path`` and patches
``clone_release_vars`` to hand that path back to the endpoint.
"""

import os
from unittest.mock import patch

import pytest

from app.services.git_service import GitService
from tests.conftest import create_app_in_db


def _touch(path: str, content: str = "") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


@pytest.fixture
def repo(tmp_path):
    with patch(
        "app.routers.apps.git_service.clone_release_vars",
        return_value=str(tmp_path),
    ), patch(
        "app.routers.apps.git_service.cleanup_repository",
        return_value=None,
    ):
        yield str(tmp_path)


_VARIABLES = '''
variable "users" {
  type = map(list(object({ email = string })))
}

variable "flavor" {
  type        = string
  description = "VM-Groesse @openstack:flavor"
  default     = "gp1.small"
}

variable "motd" {
  type = string
}
'''


@pytest.mark.integration
def test_variables_are_read_from_tofu_dir(client, db, mock_user, repo):
    _touch(os.path.join(repo, "tofu", "variables.tofu"), _VARIABLES)
    app_obj = create_app_in_db(db, mock_user, name="tofu-app", git_link="https://example.com/a.git")

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")

    assert response.status_code == 200, response.text
    by_name = {v["name"]: v for v in response.json()}
    assert set(by_name) == {"flavor", "motd"}
    assert by_name["flavor"]["osType"] == "flavor"
    assert by_name["flavor"]["default"] == "gp1.small"
    assert by_name["flavor"]["required"] is False
    assert by_name["motd"]["required"] is True
    for v in by_name.values():
        assert "source" not in v
        assert "template_key" not in v


@pytest.mark.integration
def test_marker_error_points_at_tofu_file(client, db, mock_user, repo):
    _touch(
        os.path.join(repo, "tofu", "variables.tofu"),
        'variable "net" {\n  type = string\n  description = "@openstack:netwrok"\n}\n',
    )
    app_obj = create_app_in_db(db, mock_user, name="typo-app", git_link="https://example.com/b.git")

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")

    assert response.status_code == 200, response.text
    error = response.json()[0]["markerError"]
    assert error["location"].startswith("tofu/variables.tofu:")


@pytest.mark.integration
@pytest.mark.parametrize(
    "legacy_file",
    [
        os.path.join("terraform", "variables.tf"),
        os.path.join("packer", "variables.pkr.hcl"),
        None,
    ],
)
def test_release_without_tofu_variables_is_422(client, db, mock_user, repo, legacy_file):
    if legacy_file:
        _touch(os.path.join(repo, legacy_file), 'variable "x" {\n  type = string\n}\n')
    app_obj = create_app_in_db(db, mock_user, name="old-app", git_link="https://example.com/c.git")

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")

    assert response.status_code == 422, response.text
    assert "tofu/variables.tofu" in response.json()["detail"]


@pytest.mark.unit
def test_sparse_checkout_fetches_only_the_tofu_variables_file():
    assert GitService.SPARSE_CHECKOUT_FILES == ["tofu/variables.tofu"]


@pytest.mark.integration
def test_app_without_git_link_is_400(client, db, mock_user):
    app_obj = create_app_in_db(db, mock_user, name="no-git", git_link="")

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")

    assert response.status_code == 400, response.text


@pytest.mark.integration
def test_clone_failure_is_500(client, db, mock_user):
    app_obj = create_app_in_db(db, mock_user, name="broken", git_link="https://example.com/d.git")
    with patch(
        "app.routers.apps.git_service.clone_release_vars",
        side_effect=RuntimeError("tag not found"),
    ):
        response = client.get(f"/apps/{app_obj.appId}/variables?version=v9.9")

    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "Failed to fetch variables"


@pytest.mark.integration
def test_cleanup_failure_does_not_hide_the_result(client, db, mock_user, tmp_path):
    _touch(os.path.join(str(tmp_path), "tofu", "variables.tofu"), _VARIABLES)
    app_obj = create_app_in_db(db, mock_user, name="sticky", git_link="https://example.com/e.git")
    with patch(
        "app.routers.apps.git_service.clone_release_vars",
        return_value=str(tmp_path),
    ), patch(
        "app.routers.apps.git_service.cleanup_repository",
        side_effect=OSError("busy"),
    ):
        response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")

    assert response.status_code == 200, response.text
    assert {v["name"] for v in response.json()} == {"flavor", "motd"}
