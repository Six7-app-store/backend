"""Check runner's real psql gate against an isolated PostgreSQL database."""
import os
import subprocess

import pytest
from sqlalchemy import text

from app.models import TaskStatus, TaskType
from tests.test_deployments_pause_resume import _make_deployment_with_creds


def operator(action, owner="test-run", timeout=0):
    path = "/deployment/scripts/maintenance-operator.sh"
    if not os.path.exists(path):
        pytest.skip("runner scripts are only mounted in isolated deployment tests")
    return subprocess.run(
        ["bash", path, action, "experiment", owner, str(timeout)],
        capture_output=True, text=True, timeout=10,
    )


@pytest.mark.integration
def test_runner_claim_blocks_backend_and_requires_owner(db):
    from app.services.maintenance_gate import MaintenanceInProgress, guard_task_admission
    result = operator("begin")
    assert result.returncode == 0, result.stderr
    with pytest.raises(MaintenanceInProgress):
        guard_task_admission(db)
    db.rollback()
    assert operator("end", "wrong-owner").returncode != 0
    assert operator("end").returncode == 0
    guard_task_admission(db)


@pytest.mark.integration
def test_runner_timeout_releases_gate_without_modifying_task(db, mock_user):
    _make_deployment_with_creds(
        db, mock_user, latest_task_type=TaskType.DEPLOY, latest_task_status=TaskStatus.RUNNING,
    )
    result = operator("begin")
    assert result.returncode == 2
    assert db.execute(text("SELECT owner FROM platform_maintenance WHERE id=1")).scalar_one() is None
    assert db.execute(text("SELECT count(*) FROM tasks WHERE status='RUNNING'")).scalar_one() == 1
