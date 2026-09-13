"""Integration tests for the deployment file-upload contract.

Covers the round-trip from POST /deployments (with ``files`` field)
to GET detail (file bytes stripped) to GET /files/... (full payload
returned to owner). Celery is patched out — these tests don't go to
RabbitMQ.
"""
import base64
import json
import uuid
from unittest.mock import patch

import pytest

from app.models import (
    App,
    Deployment,
    OpenStackAuthType,
    User,
    UserOpenStackCredential,
    UserRole,
)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _ensure_user_credentials(db, user):
    from app.utils import crypto

    db.add(
        UserOpenStackCredential(
            credentialId=uuid.uuid4(),
            userId=user.userId,
            auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
            auth_url="https://keystone.example/v3",
            encrypted_identifier=crypto.encrypt("test-id"),
            encrypted_secret=crypto.encrypt("test-secret"),
        )
    )
    db.commit()


def _ensure_app(db, user) -> App:
    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=user.userId,
        git_link="https://example.com/repo.git",
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


@pytest.fixture
def patched_celery():
    class _FakeAsyncResult:
        id = "fake-celery-task-id"

    with patch(
        "app.services.task_service.celery_app.send_task",
        return_value=_FakeAsyncResult(),
    ) as m:
        yield m


@pytest.mark.integration
def test_post_deployment_persists_files_under_terraform(
    client, db, mock_user, patched_celery
):
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    pdf = b"%PDF-1.4\n%fake pdf bytes for the test\n"
    payload = {
        "name": "with-files",
        "appId": str(app.appId),
        "releaseTag": "v1",
        "userInputVar": {"terraform": {"some_other": "x"}, "packer": {}},
        "files": {
            "task_pdf": {
                "all": {
                    "name": "aufgabe.pdf",
                    "content_b64": _b64(pdf),
                    "size": len(pdf),
                    "content_type": "application/pdf",
                }
            }
        },
        "teams": [],
    }
    response = client.post("/deployments/", json=payload)
    assert response.status_code == 201, response.text

    body = response.json()
    # The list/detail responses strip file bytes — but metadata
    # survives so the UI can render "uploaded: aufgabe.pdf".
    file_var = body["userInputVar"]["terraform"]["task_pdf"]
    upload = file_var["all"]
    assert upload["name"] == "aufgabe.pdf"
    assert upload["size"] == len(pdf)
    assert upload["content_type"] == "application/pdf"
    assert "content_b64" not in upload  # stripped from response

    # The DB row carries the full base64 — the strip is a response-
    # shaping concern, not a persistence one.
    deployment = db.query(Deployment).filter(
        Deployment.deploymentId == uuid.UUID(body["deploymentId"])
    ).one()
    persisted = json.loads(deployment.userInputVar)
    assert persisted["terraform"]["task_pdf"]["all"]["content_b64"] == _b64(pdf)
    # Existing terraform vars were preserved through the merge.
    assert persisted["terraform"]["some_other"] == "x"

    # Celery args carry the same dict — the worker sees what's in
    # the DB.
    celery_args = patched_celery.call_args.kwargs.get("args") or patched_celery.call_args.args[1]
    user_vars_arg = celery_args[4]
    assert user_vars_arg["terraform"]["task_pdf"]["all"]["content_b64"] == _b64(pdf)


@pytest.mark.integration
def test_post_deployment_413_when_single_file_too_large(
    client, db, mock_user, patched_celery
):
    """One file over 2 MB → 413, no row created, no celery dispatch."""
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    big = b"x" * (2 * 1024 * 1024 + 10)
    response = client.post(
        "/deployments/",
        json={
            "name": "too-big",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "huge.bin",
                        "content_b64": _b64(big),
                        "size": len(big),
                    }
                }
            },
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "file_too_large"
    patched_celery.assert_not_called()
    # No deployment row should have been written.
    assert (
        db.query(Deployment)
        .filter(Deployment.name == "too-big")
        .first() is None
    )


@pytest.mark.integration
def test_post_deployment_422_on_invalid_base64(
    client, db, mock_user, patched_celery
):
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    response = client.post(
        "/deployments/",
        json={
            "name": "bad-b64",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "broken.bin",
                        "content_b64": "not^valid$base64",
                        "size": 12,
                    }
                }
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "file_b64_invalid"
    patched_celery.assert_not_called()


@pytest.mark.integration
def test_detail_endpoint_strips_file_bytes(
    client, db, mock_user, patched_celery
):
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    pdf = b"PDF data" * 100
    create = client.post(
        "/deployments/",
        json={
            "name": "stripped-detail",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "x.pdf",
                        "content_b64": _b64(pdf),
                        "size": len(pdf),
                        "content_type": "application/pdf",
                    }
                }
            },
        },
    ).json()

    detail = client.get(f"/deployments/{create['deploymentId']}").json()
    upload = detail["userInputVar"]["terraform"]["task_pdf"]["all"]
    assert upload["name"] == "x.pdf"
    assert upload["size"] == len(pdf)
    assert "content_b64" not in upload


@pytest.mark.integration
def test_download_endpoint_returns_owner_bytes(
    client, db, mock_user, patched_celery
):
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    payload_bytes = b"hello, world\n" * 10
    create = client.post(
        "/deployments/",
        json={
            "name": "downloadable",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "hello.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                        "content_type": "text/plain",
                    }
                }
            },
        },
    ).json()
    deployment_id = create["deploymentId"]

    response = client.get(f"/deployments/{deployment_id}/files/task_pdf/all")
    assert response.status_code == 200
    assert response.content == payload_bytes
    assert response.headers["content-type"].startswith("text/plain")
    assert "hello.txt" in response.headers["content-disposition"]


@pytest.mark.integration
def test_download_endpoint_404_for_unknown_slot(
    client, db, mock_user, patched_celery
):
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    payload_bytes = b"x" * 32
    create = client.post(
        "/deployments/",
        json={
            "name": "with-one-slot",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "h.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                    }
                }
            },
        },
    ).json()

    # Wrong variable name → 404.
    bad_var = client.get(
        f"/deployments/{create['deploymentId']}/files/no_such_var/all"
    )
    assert bad_var.status_code == 404
    # Right variable, wrong slot → 404.
    bad_slot = client.get(
        f"/deployments/{create['deploymentId']}/files/task_pdf/Team-X"
    )
    assert bad_slot.status_code == 404


@pytest.mark.integration
def test_download_endpoint_member_403(
    client, db, mock_user, patched_celery, unauth_client
):
    """A team member with read-access to the deployment must NOT be
    able to fetch file bytes — only the owner-view does."""
    _ensure_user_credentials(db, mock_user)
    app = _ensure_app(db, mock_user)

    payload_bytes = b"secret" * 100
    create = client.post(
        "/deployments/",
        json={
            "name": "owner-only-download",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "secret.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                    }
                }
            },
        },
    ).json()

    # Spawn a second user (a student member of the deployment) and
    # rebind the auth dependency to them. The deployment has no team
    # mapping, so we attach the student via UserToDeployment.
    from app.main import app as fastapi_app
    from app.models import UserToDeployment
    from app.utils.auth import get_current_user

    student = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-student-{uuid.uuid4().hex[:6]}",
        email="student@dhbw.de",
        username="stud",
        firstName="Stud",
        lastName="Ent",
        role=UserRole.STUDENT,
    )
    db.add(student)
    db.add(UserToDeployment(
        userToDeploymentId=uuid.uuid4(),
        userId=student.userId,
        deploymentId=uuid.UUID(create["deploymentId"]),
    ))
    db.commit()

    # Member access: download must 403 even though the deployment is
    # readable (read-access is a separate gate).
    fastapi_app.dependency_overrides[get_current_user] = lambda: student
    try:
        resp = client.get(
            f"/deployments/{create['deploymentId']}/files/task_pdf/all"
        )
    finally:
        # Hand back to the test client's mock_user so other tests
        # in the same session aren't affected.
        fastapi_app.dependency_overrides[get_current_user] = lambda: mock_user
    assert resp.status_code == 403
