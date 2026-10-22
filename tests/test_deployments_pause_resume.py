"""Integration tests for the pause / resume HTTP endpoints.

These tests run against a real Postgres so the partial unique index
on ``tasks(deploymentId) WHERE status IN ('PENDING','RUNNING')`` and
the per-user advisory locks can fire. The Celery dispatch is patched
to a no-op so RabbitMQ isn't required.
"""
import json
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from app.config import settings
from app.models import (
    App,
    Deployment,
    OpenStackAuthType,
    Task,
    TaskStatus,
    TaskType,
    UserOpenStackCredential,
)


@pytest.fixture(autouse=True)
def _smtp_enabled_default(monkeypatch):
    """Most tests in this file don't care about SMTP, but the
    ``test_resend_access_rejected_while_action_in_flight`` cases
    do — they hit the resend-access endpoint and expect a 409 from
    the in-flight gate. With ``SMTP_ENABLED`` defaulting to ``False``,
    the kill-switch gate (which sits before the in-flight check)
    would short-circuit with 503 first. Enabling SMTP by default
    here keeps every test in this file exercising the lifecycle
    semantics it was written for, without each one needing its own
    monkeypatch.
    """
    monkeypatch.setattr(settings, "SMTP_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SMTP_USER", "test@example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "test-password", raising=False)


def _make_deployment_with_creds(
    db,
    user,
    *,
    latest_task_type: TaskType,
    latest_task_status: TaskStatus,
):
    """Seed a deployment + app + credentials + one task on the user.

    The credential row needs valid ciphertext so
    ``crud_openstack_credentials.get_dispatch_envelope`` doesn't 412
    before the dispatch path even runs. The cleartext doesn't matter
    for these tests — Celery is patched out.
    """
    from app.utils import crypto

    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=user.userId,
        git_link="https://example.com/repo.git",
    )
    db.add(app)
    db.flush()

    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name=f"d-{uuid.uuid4().hex[:8]}",
        appId=app.appId,
        userId=user.userId,
        releaseTag="v1.0.0",
        userInputVar=json.dumps({"tofu": {}}),
    )
    db.add(deployment)
    db.flush()

    db.add(
        Task(
            taskId=uuid.uuid4(),
            deploymentId=deployment.deploymentId,
            type=latest_task_type,
            status=latest_task_status,
            created_at=datetime.utcnow(),
        )
    )

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
    db.refresh(deployment)
    return deployment


@pytest.fixture
def patched_celery():
    """Replace Celery's ``send_task`` with a stub that returns a fake id.

    The real path commits a task row before send_task runs, then stamps
    the celery_task_id from the result. We only need a sentinel object
    with an ``.id`` attribute.
    """
    class _FakeAsyncResult:
        id = "fake-celery-task-id"

    with patch("app.services.task_service.celery_app.send_task",
               return_value=_FakeAsyncResult()) as m:
        yield m


@pytest.mark.integration
def test_pause_dispatches_when_status_is_success(
    client, db, mock_user, patched_celery,
):
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=TaskType.DEPLOY,
        latest_task_status=TaskStatus.SUCCESS,
    )

    response = client.post(f"/deployments/{deployment.deploymentId}/pause")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pausing"
    assert "task_id" in body

    # Pause task row was inserted; effective status is now `pausing`.
    task = (
        db.query(Task)
        .filter(Task.deploymentId == deployment.deploymentId)
        .filter(Task.type == TaskType.PAUSE)
        .one()
    )
    assert task.status == TaskStatus.PENDING
    assert task.celeryTaskId == "fake-celery-task-id"
    patched_celery.assert_called_once()
    assert patched_celery.call_args.args[0] == "tasks.pause_deployment"


@pytest.mark.integration
@pytest.mark.parametrize(
    "task_type, task_status",
    [
        (TaskType.DEPLOY, TaskStatus.PENDING),   # currently deploying
        (TaskType.DEPLOY, TaskStatus.RUNNING),
        (TaskType.DEPLOY, TaskStatus.FAILED),    # failed deploy
        (TaskType.DEPLOY, TaskStatus.CANCELLED),
        (TaskType.PAUSE, TaskStatus.SUCCESS),    # already paused
        (TaskType.PAUSE, TaskStatus.PENDING),    # in-flight pause — must reject
        (TaskType.PAUSE, TaskStatus.RUNNING),    # in-flight pause — must reject
        (TaskType.RESUME, TaskStatus.PENDING),   # in-flight resume — must reject
        (TaskType.RESUME, TaskStatus.RUNNING),   # in-flight resume — must reject
        (TaskType.DESTROY, TaskStatus.PENDING),  # being destroyed
        (TaskType.DESTROY, TaskStatus.RUNNING),
    ],
)
def test_pause_rejected_when_not_success(
    client, db, mock_user, patched_celery, task_type, task_status,
):
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=task_type,
        latest_task_status=task_status,
    )
    response = client.post(f"/deployments/{deployment.deploymentId}/pause")
    assert response.status_code == 409, (
        f"Pause must be rejected when last task is "
        f"({task_type.value}, {task_status.value})"
    )
    patched_celery.assert_not_called()


@pytest.mark.integration
def test_resume_dispatches_when_status_is_paused(
    client, db, mock_user, patched_celery,
):
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=TaskType.PAUSE,
        latest_task_status=TaskStatus.SUCCESS,
    )

    response = client.post(f"/deployments/{deployment.deploymentId}/resume")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "resuming"

    task = (
        db.query(Task)
        .filter(Task.deploymentId == deployment.deploymentId)
        .filter(Task.type == TaskType.RESUME)
        .one()
    )
    assert task.status == TaskStatus.PENDING
    assert patched_celery.call_args.args[0] == "tasks.resume_deployment"


@pytest.mark.integration
def test_resume_rejected_when_not_paused(
    client, db, mock_user, patched_celery,
):
    """A successful deploy is not a valid resume target — only paused is."""
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=TaskType.DEPLOY,
        latest_task_status=TaskStatus.SUCCESS,
    )
    response = client.post(f"/deployments/{deployment.deploymentId}/resume")
    assert response.status_code == 409
    patched_celery.assert_not_called()


@pytest.mark.integration
def test_destroy_allowed_from_paused(
    client, db, mock_user, patched_celery,
):
    """Paused deployments still hold OpenStack resources — Destroy works."""
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=TaskType.PAUSE,
        latest_task_status=TaskStatus.SUCCESS,
    )
    response = client.delete(f"/deployments/{deployment.deploymentId}")
    assert response.status_code == 202
    assert response.json()["status"] == "destroying"


@pytest.mark.integration
def test_pause_404_for_unknown_deployment(client):
    bogus = uuid.uuid4()
    response = client.post(f"/deployments/{bogus}/pause")
    assert response.status_code == 404


# ----------------------------------------------------------------
# CROSS-ACTION GATING — "no parallel actions on a deployment"
# ----------------------------------------------------------------
#
# These tests pin down the user-facing rule that NO action — pause,
# resume, delete, resend-access — may proceed while another lifecycle
# task is in flight. Every in-flight status must produce a 409 with
# a structured detail; the partial-unique index on the tasks table
# is the DB backstop, but we want the friendly 409 to fire first.

IN_FLIGHT_TASK_FIXTURES = [
    (TaskType.DEPLOY, TaskStatus.PENDING, "pending"),
    (TaskType.DEPLOY, TaskStatus.RUNNING, "running"),
    (TaskType.DESTROY, TaskStatus.PENDING, "destroying"),
    (TaskType.DESTROY, TaskStatus.RUNNING, "destroying"),
    (TaskType.PAUSE, TaskStatus.PENDING, "pausing"),
    (TaskType.PAUSE, TaskStatus.RUNNING, "pausing"),
    (TaskType.RESUME, TaskStatus.PENDING, "resuming"),
    (TaskType.RESUME, TaskStatus.RUNNING, "resuming"),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    "task_type, task_status, expected_status_text", IN_FLIGHT_TASK_FIXTURES
)
def test_delete_rejected_while_action_in_flight(
    client, db, mock_user, patched_celery, task_type, task_status, expected_status_text,
):
    """DELETE must 409 while *any* lifecycle task is in flight,
    regardless of which kind. The current_status string in the
    detail message helps the frontend tell the user *why* they
    have to wait."""
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=task_type,
        latest_task_status=task_status,
    )
    response = client.delete(f"/deployments/{deployment.deploymentId}")
    assert response.status_code == 409, (
        f"DELETE must be rejected while ({task_type.value}, "
        f"{task_status.value}) is in flight"
    )
    body = response.json()
    assert expected_status_text in str(body.get("detail", ""))
    patched_celery.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize(
    "task_type, task_status, expected_status_text", IN_FLIGHT_TASK_FIXTURES
)
def test_resume_rejected_while_action_in_flight(
    client, db, mock_user, patched_celery, task_type, task_status, expected_status_text,
):
    """RESUME mirrors PAUSE — refused during any in-flight task."""
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=task_type,
        latest_task_status=task_status,
    )
    response = client.post(f"/deployments/{deployment.deploymentId}/resume")
    assert response.status_code == 409
    patched_celery.assert_not_called()


@pytest.mark.integration
@pytest.mark.parametrize(
    "task_type, task_status, expected_status_text", IN_FLIGHT_TASK_FIXTURES
)
def test_resend_access_rejected_while_action_in_flight(
    client, db, mock_user, patched_celery, task_type, task_status, expected_status_text,
):
    """The resend-access endpoint must mirror the lifecycle gate —
    sending a fresh credentials mail in the middle of a destroy or
    pause is misleading at best (instances may be SHUTOFF or gone)
    and a tiny SMTP-amplification vector at worst.
    """
    from app.models import Team

    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=task_type,
        latest_task_status=task_status,
    )
    # Resend needs an existing team + member entry to even reach
    # the in-flight gate. We don't actually expect the mail to be
    # rendered (the 409 must fire first), but the URL still needs
    # valid path components.
    team = Team(teamId=uuid.uuid4(), name="Team-1", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()

    response = client.post(
        f"/deployments/{deployment.deploymentId}"
        f"/teams/{team.teamId}"
        f"/users/{mock_user.userId}"
        f"/resend-access"
    )
    # 409 (in-flight gate fires) is the win. 404 (team-or-user
    # not in deployment, depending on test seed quirks) would also
    # be acceptable but is NOT what we're asserting here. We pin
    # 409 so a regression where the gate is skipped fails loudly.
    assert response.status_code == 409


@pytest.mark.integration
def test_pause_failed_status_allows_retry(client, db, mock_user, patched_celery):
    """A previously failed pause must still allow a fresh PAUSE
    attempt — the deployment itself is unaffected. Mirrors the
    lifecycle matrix entry for ``pause_failed``.
    """
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=TaskType.PAUSE,
        latest_task_status=TaskStatus.FAILED,
    )
    response = client.post(f"/deployments/{deployment.deploymentId}/pause")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pausing"


@pytest.mark.integration
def test_destroy_allowed_from_pause_failed(client, db, mock_user, patched_celery):
    """User can give up on a stuck pause and tear it down."""
    deployment = _make_deployment_with_creds(
        db, mock_user,
        latest_task_type=TaskType.PAUSE,
        latest_task_status=TaskStatus.FAILED,
    )
    response = client.delete(f"/deployments/{deployment.deploymentId}")
    assert response.status_code == 202
    assert response.json()["status"] == "destroying"
