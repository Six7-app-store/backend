"""The approval is tied to the commit that was submitted for review."""
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.crud import app_version_approvals as approvals
from app.models import AppVersionApproval, AppVersionApprovalStatus, Deployment, UserRole
from tests.conftest import create_app_in_db
from tests.test_deployment_wizard_rules import _credentials
from tests.test_phase0_security import _app, _deploy, _user, as_user


@pytest.mark.integration
def test_review_and_approval_use_the_submitted_commit(client, admin_client, db, mock_user, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_PINNED_RELEASES", True)
    app = create_app_in_db(db, mock_user)
    sha = "a" * 40
    with (
        patch("app.routers.apps.resolve_tag", return_value=sha),
        patch("app.routers.apps.load_variable_definitions", return_value=[]) as submit_vars,
    ):
        response = client.post(f"/apps/{app.appId}/versions/v1/submit", json={})
    assert response.status_code == 201, response.text
    assert response.json()["commit_sha"] == sha
    assert submit_vars.call_args.args[1] == sha
    with patch("app.routers.admin_apps.load_variable_definitions", return_value=[]) as review_vars:
        response = admin_client.post(f"/admin/apps/{app.appId}/versions/v1/approve")
    assert response.status_code == 200, response.text
    assert response.json()["commit_sha"] == sha
    assert review_vars.call_args.args[1] == sha
    with patch("app.routers.apps.load_variable_definitions", return_value=[]) as fetched_vars:
        response = client.get(f"/apps/{app.appId}/variables", params={"version": "v1"})
    assert response.status_code == 200
    assert fetched_vars.call_args.args[1] == sha


@pytest.mark.integration
def test_legacy_approval_without_recorded_commit_is_not_approved(admin_client, db, mock_user, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_PINNED_RELEASES", True)
    app = create_app_in_db(db, mock_user)
    approvals.submit_version(db, app.appId, "old-tag")
    response = admin_client.post(f"/admin/apps/{app.appId}/versions/old-tag/approve")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "approval_commit_missing"


@pytest.mark.integration
def test_deploy_dispatches_the_approved_commit(client, db, mock_user, mock_admin, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_PINNED_RELEASES", True)
    app = create_app_in_db(db, mock_admin, is_private=False)
    sha = "b" * 40
    db.add(AppVersionApproval(
        appId=app.appId, version_tag="v1", commit_sha=sha,
        status=AppVersionApprovalStatus.APPROVED,
    ))
    db.commit()
    _credentials(db, mock_user)
    with (
        patch("app.routers.deployments.load_variable_definitions", return_value=[]) as variables,
        patch("app.services.task_service.celery_app.send_task", return_value=MagicMock(id="fake")) as send,
    ):
        response = client.post("/deployments/", json={
            "name": "Pinned", "appId": str(app.appId), "releaseTag": "v1",
            "userInputVar": {"tofu": {}}, "teams": [],
        })
    assert response.status_code == 201, response.text
    assert variables.call_args.args[1] == sha
    assert send.call_args.kwargs["args"][3] == sha
    assert db.query(Deployment).filter_by(name="Pinned").one().release_commit == sha


@pytest.mark.integration
def test_owner_deploys_approved_commit_even_if_tag_changes(client, db, mock_user, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_PINNED_RELEASES", True)
    app = create_app_in_db(db, mock_user)
    sha = "d" * 40
    db.add(AppVersionApproval(
        appId=app.appId, version_tag="v1", commit_sha=sha,
        status=AppVersionApprovalStatus.APPROVED,
    ))
    db.commit()
    _credentials(db, mock_user)
    with (
        patch("app.routers.deployments.resolve_tag") as resolve,
        patch("app.routers.deployments.load_variable_definitions", return_value=[]),
        patch("app.services.task_service.celery_app.send_task", return_value=MagicMock(id="fake")) as send,
    ):
        response = client.post("/deployments/", json={
            "name": "Owner pinned", "appId": str(app.appId), "releaseTag": "v1",
            "userInputVar": {"tofu": {}}, "teams": [],
        })
    assert response.status_code == 201, response.text
    resolve.assert_not_called()
    assert send.call_args.kwargs["args"][3] == sha


@pytest.mark.integration
def test_teacher_requests_admin_approval_before_running_the_commit(db, monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_PINNED_RELEASES", True)
    teacher = _user(db, UserRole.TEACHER)
    admin = _user(db, UserRole.ADMIN)
    app = _app(db, teacher)
    _credentials(db, teacher)
    sha = "e" * 40
    with (
        patch("app.routers.apps.resolve_tag", return_value=sha),
        patch("app.routers.apps.load_variable_definitions", return_value=[]),
        patch("app.routers.admin_apps.load_variable_definitions", return_value=[]),
        patch("app.routers.deployments.load_variable_definitions", return_value=[]),
        patch("app.services.task_service.celery_app.send_task", return_value=MagicMock(id="fake")) as send,
    ):
        with as_user(teacher) as client:
            assert _deploy(client, app, "v1").status_code == 403
            assert client.post(f"/apps/{app.appId}/versions/v1/submit", json={}).status_code == 201
            assert _deploy(client, app, "v1").status_code == 403
        send.assert_not_called()
        with as_user(admin) as client:
            pending = client.get("/admin/apps/versions/pending").json()
            assert any(item["appId"] == str(app.appId) and item["commit_sha"] == sha for item in pending)
            assert client.post(f"/admin/apps/{app.appId}/versions/v1/approve").status_code == 200
        with as_user(teacher) as client:
            assert _deploy(client, app, "v1").status_code == 201
        assert send.call_args.kwargs["args"][3] == sha
