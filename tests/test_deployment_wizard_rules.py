"""Rules ``POST /deployments`` enforces for the deployment wizard, and the
newest approved version the wizard preselects.

* the version is required and, for non-owners, must be approved;
* required variables and files are checked before the row is created,
  instead of failing minutes later in the worker;
* ``latestApprovedVersion`` on the app list and detail.

Git is patched out: ``load_variable_definitions`` returns what a test
declares, Celery's ``send_task`` returns a fake id.
"""
import base64
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.crud import app_version_approvals as crud_approvals
from app.models import (
    AppVersionApproval,
    AppVersionApprovalStatus,
    OpenStackAuthType,
    User,
    UserOpenStackCredential,
    UserRole,
)
from app.routers.deployments import _validate_required_inputs
from app.schemas import Team
from tests.conftest import create_app_in_db


def _user(db, username: str, role=UserRole.STUDENT, email: str | None = None) -> User:
    user = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-{username}",
        email=email or f"{username}@dhbw.de",
        username=username,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _approve(db, app, *tags: str, status=AppVersionApprovalStatus.APPROVED):
    for tag in tags:
        db.add(AppVersionApproval(appId=app.appId, version_tag=tag, status=status))
    db.commit()


def _credentials(db, user):
    from app.utils import crypto

    db.add(UserOpenStackCredential(
        credentialId=uuid.uuid4(),
        userId=user.userId,
        auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
        auth_url="https://keystone.example/v3",
        encrypted_identifier=crypto.encrypt("id"),
        encrypted_secret=crypto.encrypt("secret"),
    ))
    db.commit()


@pytest.fixture
def no_celery():
    class _Result:
        id = "fake-celery-task-id"

    with patch("app.services.task_service.celery_app.send_task", return_value=_Result()) as send:
        yield send


def _variables(definitions):
    return patch("app.routers.deployments.load_variable_definitions", return_value=definitions)


def _file(name: str) -> dict:
    content = b"public class Aufgabe {}"
    return {
        "name": name,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "size": len(content),
        "content_type": "text/plain",
    }


# ----------------------------------------------------------------
# Newest approved version
# ----------------------------------------------------------------
@pytest.mark.integration
def test_latest_approved_version_orders_numerically_and_ignores_unapproved(db, mock_user):
    app = create_app_in_db(db, mock_user)
    _approve(db, app, "v1.9.0", "v1.10.0", "main")
    _approve(db, app, "v2.0.0", status=AppVersionApprovalStatus.PENDING)

    assert crud_approvals.latest_approved_version(db, app.appId) == "v1.10.0"
    assert crud_approvals.latest_approved_versions(db, []) == {}


@pytest.mark.integration
def test_app_list_and_detail_carry_latest_approved_version(client, db, mock_user):
    approved = create_app_in_db(db, mock_user, name="Online-IDE")
    _approve(db, approved, "v1.0.0", "v1.2.0")
    create_app_in_db(db, mock_user, name="Entwurf")

    with patch("app.routers.apps.git_service.get_versions", return_value=[]):
        listed = {a["name"]: a["latestApprovedVersion"] for a in client.get("/apps/").json()}
        detail = client.get(f"/apps/{approved.appId}").json()

    assert listed == {"Online-IDE": "v1.2.0", "Entwurf": None}
    assert detail["latestApprovedVersion"] == "v1.2.0"
    assert set(detail["approvedVersions"]) == {"v1.0.0", "v1.2.0"}


@pytest.mark.integration
def test_app_detail_returns_git_versions_as_objects(client, db, mock_user):
    """Regression: the detail used to flatten the versions to strings,
    which the response schema rejects with a 500."""
    app = create_app_in_db(db, mock_user)
    versions = [
        {"version": "v1.0.1", "commit": "abc12345", "type": "tag", "description": None},
        {"version": "v1.0.0", "commit": "def67890", "type": "tag"},
    ]

    with patch("app.routers.apps.git_service.get_versions", return_value=versions):
        response = client.get(f"/apps/{app.appId}")

    assert response.status_code == 200
    assert [v["version"] for v in response.json()["versions"]] == ["v1.0.1", "v1.0.0"]


# ----------------------------------------------------------------
# Version on POST /deployments
# ----------------------------------------------------------------
@pytest.mark.integration
def test_create_refuses_a_deployment_without_version(client, db, mock_user):
    app = create_app_in_db(db, mock_user)
    response = client.post("/deployments/", json={"name": "x", "appId": str(app.appId)})

    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "version_required"}


@pytest.mark.integration
def test_non_owner_may_only_deploy_an_approved_version(client, db, mock_user, no_celery):
    author = _user(db, "author", role=UserRole.TEACHER)
    app = create_app_in_db(db, author)
    _approve(db, app, "v1.0.0")
    _approve(db, app, "v2.0.0", status=AppVersionApprovalStatus.PENDING)
    _credentials(db, mock_user)

    with _variables([]):
        refused = client.post("/deployments/", json={"name": "x", "appId": str(app.appId), "releaseTag": "v2.0.0"})
        created = client.post("/deployments/", json={"name": "x", "appId": str(app.appId), "releaseTag": "v1.0.0"})

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "version_not_approved"
    assert created.status_code == 201, created.text


@pytest.mark.integration
def test_owner_requires_approval_before_deploying(client, db, mock_user, no_celery):
    app = create_app_in_db(db, mock_user)
    _credentials(db, mock_user)

    with _variables([]):
        response = client.post("/deployments/", json={"name": "x", "appId": str(app.appId), "releaseTag": "v0.1.0"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "version_not_approved"
    no_celery.assert_not_called()
    _approve(db, app, "v0.1.0")
    with _variables([]):
        response = client.post("/deployments/", json={"name": "x", "appId": str(app.appId), "releaseTag": "v0.1.0"})
    assert response.status_code == 201, response.text


# ----------------------------------------------------------------
# Required inputs on POST /deployments
# ----------------------------------------------------------------
TASK_FILE = {"name": "task_files", "type": "map(string)", "osType": "file", "osScope": "user", "required": True}
COURSE_CODE = {"name": "course_code", "type": "string", "required": True}
FLAVOR = {"name": "flavor", "type": "string", "required": False, "default": "m1.small"}


@pytest.mark.integration
def test_create_rejects_a_missing_required_file_before_the_worker(client, db, mock_user, no_celery):
    app = create_app_in_db(db, mock_user)
    _approve(db, app, "v1.0.0")
    _credentials(db, mock_user)
    luca = _user(db, "luca.baeck")
    mika = _user(db, "mika.jun")
    payload = {
        "name": "Lab",
        "appId": str(app.appId),
        "releaseTag": "v1.0.0",
        "userInputVar": {"tofu": {"course_code": "WI23"}},
        "teams": [{"name": "Team-1", "userIds": [str(luca.userId), str(mika.userId)]}],
        "files": {"task_files": {"Team-1-luca.baeck": _file("luca.baeck.java")}},
    }

    with _variables([TASK_FILE, COURSE_CODE, FLAVOR]):
        response = client.post("/deployments/", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "reason": "required_missing",
        "missing": [{"variable": "task_files", "slot": "Team-1-mika.jun"}],
    }
    no_celery.assert_not_called()

    payload["files"]["task_files"]["Team-1-mika.jun"] = _file("mika.jun.java")
    with _variables([TASK_FILE, COURSE_CODE, FLAVOR]):
        response = client.post("/deployments/", json=payload)
    assert response.status_code == 201, response.text


@pytest.mark.integration
def test_create_rejects_a_missing_required_variable_without_inputs(client, db, mock_user, no_celery):
    app = create_app_in_db(db, mock_user)
    _approve(db, app, "v1")
    _credentials(db, mock_user)

    with _variables([COURSE_CODE, FLAVOR]):
        response = client.post("/deployments/", json={"name": "x", "appId": str(app.appId), "releaseTag": "v1"})

    assert response.status_code == 422
    assert response.json()["detail"]["missing"] == [{"variable": "course_code"}]


@pytest.mark.integration
def test_required_slots_follow_the_roster(db):
    anna = _user(db, "", email="anna.adler@dhbw.de")
    teams = [Team(name="Rot", userIds=[str(anna.userId)]), Team(name="Blau", userIds=[])]
    defs = [
        {"name": "quota", "type": "number", "varScope": "team", "required": True},
        {"name": "login", "type": "string", "varScope": "user", "required": True},
        {"name": "sheet", "osType": "file", "osScope": "all", "required": True},
    ]

    with pytest.raises(HTTPException) as caught:
        _validate_required_inputs({"tofu": {"quota": {"Rot": 5, "Blau": ""}}}, defs, teams, db)

    assert caught.value.detail["missing"] == [
        {"variable": "quota", "slot": "Blau"},
        # No username: the slot is keyed by the email's local part, as in the wizard.
        {"variable": "login", "slot": "Rot-anna.adler"},
        {"variable": "sheet"},
    ]

    complete = {"tofu": {
        "quota": {"Rot": 5, "Blau": 3},
        "login": {"Rot-anna.adler": "anna"},
        "sheet": {"all": _file("blatt.pdf")},
    }}
    _validate_required_inputs(complete, defs, teams, db)
    _validate_required_inputs(None, [FLAVOR], teams, db)
