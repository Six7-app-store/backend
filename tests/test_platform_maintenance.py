"""Real PostgreSQL tests for task admission and maintenance coordination."""
import threading
import time
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from app.models import Task, TaskStatus, TaskType
from app.services import maintenance_control, maintenance_gate
from tests.conftest import TestingSessionLocal
from tests.test_deployments_pause_resume import _make_deployment_with_creds


@pytest.fixture
def control_on_test_db(monkeypatch):
    # maintenance_control opens its own sessions from app.database, which is
    # bound to DATABASE_URL (the dev database), not to TEST_DATABASE_URL.
    monkeypatch.setattr(maintenance_control, "SessionLocal", TestingSessionLocal)


@pytest.mark.integration
def test_maintenance_blocks_admission_until_matching_owner_releases(db):
    assert maintenance_gate.acquire_maintenance(db, "update-a")
    db.commit()
    with TestingSessionLocal() as admission:
        with pytest.raises(maintenance_gate.MaintenanceInProgress):
            maintenance_gate.guard_task_admission(admission)
        admission.rollback()
    assert not maintenance_gate.release_maintenance(db, "wrong-owner")
    assert not maintenance_gate.acquire_maintenance(db, "update-b")
    db.rollback()
    assert maintenance_gate.release_maintenance(db, "update-a")
    db.commit()
    with TestingSessionLocal() as admission:
        maintenance_gate.guard_task_admission(admission)


@pytest.mark.integration
def test_admission_and_maintenance_are_serialized(db):
    """An in-flight admission transaction must commit before maintenance wins."""
    entered = threading.Event()
    acquired = threading.Event()
    errors = []

    def claim():
        try:
            with TestingSessionLocal() as updater:
                entered.set()
                assert maintenance_gate.acquire_maintenance(updater, "race-test")
                updater.commit()
                acquired.set()
        except Exception as error:
            errors.append(error)
            acquired.set()

    maintenance_gate.guard_task_admission(db)
    worker = threading.Thread(target=claim)
    worker.start()
    try:
        assert entered.wait(2)
        time.sleep(.2)
        assert not acquired.is_set(), "maintenance passed an open admission transaction"
        db.commit()
        assert acquired.wait(5)
        worker.join(5)
        assert not errors
        with TestingSessionLocal() as admission, pytest.raises(maintenance_gate.MaintenanceInProgress):
            maintenance_gate.guard_task_admission(admission)
    finally:
        db.rollback()
        worker.join(5)
        db.execute(text("UPDATE platform_maintenance SET owner = NULL WHERE id = 1"))
        db.commit()


@pytest.mark.integration
def test_missing_gate_is_not_silently_accepted(db):
    db.execute(text("DELETE FROM platform_maintenance WHERE id = 1"))
    with pytest.raises(RuntimeError, match="gate is missing"):
        maintenance_gate.guard_task_admission(db)


@pytest.mark.integration
def test_real_pause_endpoint_returns_503_without_dispatch_during_maintenance(
    client, db, mock_user, monkeypatch
):
    deployment = _make_deployment_with_creds(
        db, mock_user, latest_task_type=TaskType.DEPLOY, latest_task_status=TaskStatus.SUCCESS,
    )
    assert maintenance_gate.acquire_maintenance(db, "test-update")
    db.commit()
    calls = []
    monkeypatch.setattr("app.services.task_service.celery_app.send_task", lambda *a, **_k: calls.append(a))
    response = client.post(f"/deployments/{deployment.deploymentId}/pause")
    assert response.status_code == 503
    assert "maintenance" in response.json()["detail"].lower()
    assert not calls
    assert db.query(Task).filter(Task.deploymentId == deployment.deploymentId).count() == 1


@pytest.mark.integration
def test_control_timeout_releases_gate_without_touching_pending_task(
    db, mock_user, control_on_test_db
):
    deployment = _make_deployment_with_creds(
        db, mock_user, latest_task_type=TaskType.DEPLOY, latest_task_status=TaskStatus.PENDING,
    )
    assert maintenance_control.begin_and_drain("timed-out", timeout=0) is False
    assert db.execute(text("SELECT owner FROM platform_maintenance WHERE id=1")).scalar_one() is None
    assert db.query(Task).filter(Task.deploymentId == deployment.deploymentId).one().status == TaskStatus.PENDING


@pytest.mark.integration
def test_control_retains_gate_until_explicit_release(db, control_on_test_db):
    assert maintenance_control.begin_and_drain("successful-drain", timeout=0)
    assert db.execute(text("SELECT owner FROM platform_maintenance WHERE id=1")).scalar_one() == "successful-drain"
    db.rollback()
    assert not maintenance_control.end("other-owner")
    assert maintenance_control.end("successful-drain")


@pytest.mark.parametrize(
    "active,reserved,scheduled,expected",
    [
        ({"worker": []}, {"worker": []}, {"worker": []}, True),
        ({"worker": [{"id": "one"}]}, {"worker": []}, {"worker": []}, False),
        ({"worker": []}, {"worker": [{"id": "one"}]}, {"worker": []}, False),
        ({"worker": []}, {"worker": []}, {"worker": [{"id": "one"}]}, False),
        (None, None, None, False),
        ({"one": []}, {"two": []}, {"one": []}, False),
    ],
)
def test_worker_check_rejects_busy_missing_or_inconsistent_reports(
    monkeypatch, active, reserved, scheduled, expected
):
    inspector = Mock()
    inspector.active.return_value = active
    inspector.reserved.return_value = reserved
    inspector.scheduled.return_value = scheduled
    monkeypatch.setattr(maintenance_control.celery_app.control, "inspect", lambda **_: inspector)
    assert maintenance_control.workers_idle() is expected
