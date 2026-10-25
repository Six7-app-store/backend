"""Tests for the VM-presence check and the entry-only delete.

Covers:
 * ``vms_missing`` verdict from per-instance drift values
 * ``check_deployments`` setting / clearing ``vm_missing_since``
 * ``DELETE /deployments/{id}?entry_only=true`` gating
 * ``vm_missing_since`` on the list response

The OpenStack ``Connection`` is mocked via
``app.services.deployment_status.user_connection``, same as the
resource-endpoint tests.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models import (
    App,
    Deployment,
    OpenStackAuthType,
    Task,
    TaskStatus,
    TaskType,
    UserOpenStackCredential,
)
from app.services.vm_presence import check_deployments, vms_missing

_STATE = {
    "version": 4,
    "resources": [
        {
            "type": "openstack_compute_instance_v2",
            "name": "vm",
            "instances": [
                {"index_key": 0, "attributes": {"id": "uuid-vm-0", "name": "vm-0"}},
                {"index_key": 1, "attributes": {"id": "uuid-vm-1", "name": "vm-1"}},
            ],
        },
    ],
}


def _seed(db, user, *, task_type=TaskType.DEPLOY, task_status=TaskStatus.SUCCESS):
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
        name="vm-presence",
        appId=app.appId,
        userId=user.userId,
        releaseTag="main",
    )
    db.add(deployment)
    db.flush()
    db.add(
        Task(
            taskId=uuid.uuid4(),
            deploymentId=deployment.deploymentId,
            celeryTaskId="fake-celery-id",
            type=task_type,
            status=task_status,
            tf_state=json.dumps(_STATE),
        )
    )
    db.commit()
    db.refresh(deployment)
    return deployment


def _server(server_id: str):
    """SDK-shaped server the stage-1 joiner can read without raising —
    a bare MagicMock trips ``_hardware_from`` and degrades to ``stale``."""
    server = MagicMock()
    server.id = server_id
    server.status = "ACTIVE"
    server.task_state = None
    server.vm_state = "active"
    server.power_state = 1
    server.fault = None
    server.flavor = {"original_name": "m1.small", "ram": 2048, "vcpus": 1, "disk": 20}
    server.image = {"id": "image-uuid"}
    server.availability_zone = "nova"
    server.launched_at = "2026-10-01T12:00:00Z"
    server.addresses = {}
    return server


@pytest.fixture
def conn():
    conn = MagicMock()

    @contextmanager
    def _fake_conn(_db, _user):
        yield conn

    with patch("app.services.deployment_status.user_connection", _fake_conn):
        yield conn


# ----------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("drifts", "expected"),
    [
        ([], None),
        (["missing", "missing"], True),
        (["missing", "in_sync"], False),
        (["stale", "in_sync"], False),
        (["missing", "stale"], None),
        (["stale"], None),
    ],
)
def test_vms_missing_verdict(drifts, expected):
    assert vms_missing(drifts) is expected


# ----------------------------------------------------------------
# check_deployments
# ----------------------------------------------------------------
@pytest.mark.integration
def test_marks_deployment_when_all_vms_gone(db, mock_user, conn):
    deployment = _seed(db, mock_user)
    conn.compute.find_server.return_value = None

    check_deployments(db)

    db.refresh(deployment)
    assert deployment.vm_missing_since is not None


@pytest.mark.integration
def test_does_not_mark_when_one_vm_remains(db, mock_user, conn):
    deployment = _seed(db, mock_user)
    conn.compute.find_server.side_effect = lambda sid, **_: (
        None if sid == "uuid-vm-0" else _server(sid)
    )

    check_deployments(db)

    db.refresh(deployment)
    assert deployment.vm_missing_since is None


@pytest.mark.integration
def test_lookup_error_never_marks(db, mock_user, conn):
    """An OpenStack outage must not flag deployments."""
    deployment = _seed(db, mock_user)
    conn.compute.find_server.side_effect = RuntimeError("keystone down")

    check_deployments(db)

    db.refresh(deployment)
    assert deployment.vm_missing_since is None


@pytest.mark.integration
def test_clears_marker_when_vm_reappears(db, mock_user, conn):
    deployment = _seed(db, mock_user)
    deployment.vm_missing_since = datetime(2026, 10, 1)
    db.commit()
    conn.compute.find_server.side_effect = lambda sid, **_: _server(sid)

    check_deployments(db)

    db.refresh(deployment)
    assert deployment.vm_missing_since is None


@pytest.mark.integration
def test_skips_deployments_without_live_resources(db, mock_user, conn):
    """A failed deploy may never have created a VM — no verdict."""
    deployment = _seed(db, mock_user, task_status=TaskStatus.FAILED)
    conn.compute.find_server.return_value = None

    check_deployments(db)

    db.refresh(deployment)
    assert deployment.vm_missing_since is None
    conn.compute.find_server.assert_not_called()


# ----------------------------------------------------------------
# API
# ----------------------------------------------------------------
@pytest.mark.integration
def test_entry_only_delete_rejected_without_marker(client, db, mock_user):
    deployment = _seed(db, mock_user)

    response = client.delete(f"/deployments/{deployment.deploymentId}?entry_only=true")

    assert response.status_code == 409
    db.refresh(deployment)
    assert deployment.deleted_at is None


@pytest.mark.integration
def test_entry_only_delete_soft_deletes_when_vms_missing(client, db, mock_user):
    deployment = _seed(db, mock_user)
    deployment.vm_missing_since = datetime(2026, 10, 1)
    db.commit()

    with patch("app.services.task_service.celery_app.send_task") as send:
        response = client.delete(f"/deployments/{deployment.deploymentId}?entry_only=true")

    assert response.status_code == 204
    send.assert_not_called()
    db.refresh(deployment)
    assert deployment.deleted_at is not None


@pytest.mark.integration
def test_entry_only_delete_still_blocked_while_task_in_flight(client, db, mock_user):
    deployment = _seed(db, mock_user, task_type=TaskType.DESTROY, task_status=TaskStatus.RUNNING)
    deployment.vm_missing_since = datetime(2026, 10, 1)
    db.commit()

    response = client.delete(f"/deployments/{deployment.deploymentId}?entry_only=true")

    assert response.status_code == 409


@pytest.mark.integration
def test_list_exposes_vm_missing_since(client, db, mock_user):
    deployment = _seed(db, mock_user)
    deployment.vm_missing_since = datetime(2026, 10, 1)
    db.commit()

    response = client.get("/deployments/")

    assert response.status_code == 200
    row = next(r for r in response.json() if r["deploymentId"] == str(deployment.deploymentId))
    assert row["vm_missing_since"] is not None
