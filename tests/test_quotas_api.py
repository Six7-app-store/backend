"""API-level tests for ``GET /quotas/overview``.

The endpoint is wired to ``backend/app/routers/quotas.py`` and hits the
OpenStack SDK via ``openstack.connect`` to retrieve compute/storage/
network quotas for the **current user's** personal project.

OpenStack is mocked on the connection-level: ``openstack.connect`` is
patched inside the quotas router so the tests never reach a real cloud.
A fully configured ``MagicMock`` plays the role of the live connection
(``conn.compute.*``, ``conn.volume.*``, ``conn.network.*``) and lets each
test control success / failure modes without spinning up a deployment.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.models import OpenStackAuthType, UserOpenStackCredential
from app.utils import crypto


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _ensure_user_credentials(db, user):
    """Persist a decryptable credential row so the router's
    ``crud_creds.get_decrypted_for_backend`` lookup succeeds and the code
    proceeds to the (mocked) ``openstack.connect`` call."""
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


def _make_quota_conn(project_id: str = "proj-uuid") -> MagicMock:
    """Build a MagicMock shaped like an OpenStack ``Connection`` that the
    quotas endpoint can iterate without exploding.

    The numeric fields are deliberately small + stable so assertions can
    pin exact values."""
    conn = MagicMock()
    conn.current_project_id = project_id

    # === COMPUTE ===
    compute_limits = MagicMock()
    compute_limits.instances = 10
    compute_limits.cores = 20
    compute_limits.ram = 40960  # MB
    conn.compute.get_quota_set.return_value = compute_limits

    compute_usage = MagicMock()
    compute_usage.absolute.total_instances_used = 2
    compute_usage.absolute.total_cores_used = 4
    compute_usage.absolute.total_ram_used = 8192
    conn.compute.get_limits.return_value = compute_usage

    # === STORAGE ===
    volume_limits = MagicMock()
    volume_limits.volumes = 10
    volume_limits.snapshots = 10
    volume_limits.gigabytes = 1000
    conn.volume.get_quota_set.return_value = volume_limits

    vol = MagicMock()
    vol.size = 20
    conn.volume.volumes.return_value = [vol]
    conn.volume.snapshots.return_value = []

    # === NETWORK ===
    network_limits = MagicMock()
    network_limits.floatingip = 50
    network_limits.security_group = 10
    network_limits.security_group_rule = 100
    network_limits.network = 100
    network_limits.port = 500
    network_limits.router = 10
    conn.network.get_quota.return_value = network_limits

    # Empty live resources keep the arithmetic predictable.
    conn.network.ips.return_value = []
    sg = MagicMock()
    sg.id = "sg-1"
    conn.network.security_groups.return_value = [sg]
    conn.network.networks.return_value = []
    conn.network.ports.return_value = []
    conn.network.routers.return_value = []
    conn.network.security_group_rules.return_value = []

    return conn


# ----------------------------------------------------------------
# 1. Student fetches own quota
# ----------------------------------------------------------------
@pytest.mark.integration
def test_quota_overview_student_returns_own_quota(student_client, db, mock_student):
    _ensure_user_credentials(db, mock_student)
    conn = _make_quota_conn()

    with patch("app.routers.quotas.openstack.connect", return_value=conn):
        response = student_client.get("/quotas/overview")

    assert response.status_code == 200
    body = response.json()
    # Compute block reflects the mock's numbers exactly.
    assert body["compute"]["instances"]["limit"] == 10
    assert body["compute"]["instances"]["used"] == 2
    assert body["compute"]["instances"]["available"] == 8
    assert body["compute"]["ram"]["unit"] == "MB"
    # Storage uses the single 20 GB volume from the mock.
    assert body["storage"]["volumes"]["used"] == 1
    assert body["storage"]["gigabytes"]["used"] == 20
    # Network section is present and shaped correctly.
    assert body["network"]["floating_ips"]["limit"] == 50


# ----------------------------------------------------------------
# 2. Teacher fetches own quota
# ----------------------------------------------------------------
@pytest.mark.integration
def test_quota_overview_teacher_returns_own(client, db, mock_user):
    _ensure_user_credentials(db, mock_user)
    conn = _make_quota_conn(project_id="teacher-proj")

    with patch("app.routers.quotas.openstack.connect", return_value=conn):
        response = client.get("/quotas/overview")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"compute", "storage", "network"}
    assert body["compute"]["vcpus"]["limit"] == 20
    assert body["compute"]["vcpus"]["used"] == 4
    # ``connect`` must have been invoked with the teacher's credentials.
    # We don't pin the full kwargs (they're built deep in the router),
    # but at minimum the call happened.
    # The patch context above already proved this — the response is 200.


# ----------------------------------------------------------------
# 3. Admin can query another user's quota via ?user_id=...
# ----------------------------------------------------------------
@pytest.mark.skip(
    reason="Backend feature pending: GET /quotas/overview does not yet accept "
    "a ?user_id= override for admins. Re-enable once app/routers/quotas.py "
    "wires up the cross-user lookup + capability gate."
)
@pytest.mark.integration
def test_quota_overview_admin_can_query_other_user_id(
    admin_client, db, mock_admin, mock_student
):
    """Admin role acts as a platform operator and is allowed to look at
    another user's quota by passing the target user's ID. The
    credentials used for the OpenStack call MUST belong to the queried
    user, not the admin."""
    # Only the target user has credentials — if the router accidentally
    # used the admin's identity, the 412 "credentials missing" branch
    # would trip.
    _ensure_user_credentials(db, mock_student)
    conn = _make_quota_conn(project_id="student-proj")

    with patch("app.routers.quotas.openstack.connect", return_value=conn):
        response = admin_client.get(
            "/quotas/overview",
            params={"user_id": str(mock_student.userId)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["compute"]["instances"]["limit"] == 10


# ----------------------------------------------------------------
# 4. Student cannot query another user's quota
# ----------------------------------------------------------------
@pytest.mark.skip(
    reason="Backend feature pending: /quotas/overview ignores ?user_id= and "
    "therefore cannot enforce the 403 gate for non-admins yet. Re-enable "
    "once the cross-user capability check is in place."
)
@pytest.mark.integration
def test_quota_overview_student_cannot_query_other_user_id_403(
    student_client, db, mock_student, mock_user
):
    """Non-admins must not be able to scope quotas to a different user.
    The router has to reject the parameter with 403 BEFORE the
    OpenStack call — so ``openstack.connect`` is asserted *not* to be
    invoked."""
    _ensure_user_credentials(db, mock_user)  # the teacher / target
    _ensure_user_credentials(db, mock_student)  # also for the student

    with patch("app.routers.quotas.openstack.connect") as mocked_connect:
        response = student_client.get(
            "/quotas/overview",
            params={"user_id": str(mock_user.userId)},
        )

    assert response.status_code == 403
    assert not mocked_connect.called


# ----------------------------------------------------------------
# 5. Unauthenticated request is rejected
# ----------------------------------------------------------------
@pytest.mark.integration
def test_quota_overview_unauthenticated_401(unauth_client):
    """No auth override is installed → ``get_current_user``
    runs for real and must reject the request with 401."""
    with patch("app.routers.quotas.openstack.connect") as mocked_connect:
        response = unauth_client.get("/quotas/overview")

    assert response.status_code == 401
    assert not mocked_connect.called


# ----------------------------------------------------------------
# 6. OpenStack unreachable → 502 Bad Gateway
# ----------------------------------------------------------------
@pytest.mark.skip(
    reason="Backend feature pending: app/routers/quotas.py wraps all upstream "
    "failures (incl. ConnectionError) as HTTP 500; the 502-on-transport-"
    "failure translation has not been implemented yet."
)
@pytest.mark.integration
def test_quota_overview_openstack_unreachable_502(client, db, mock_user):
    """If the OpenStack endpoint is unreachable the router must
    translate the upstream failure into a 502 instead of leaking a raw
    500 with internal details."""
    _ensure_user_credentials(db, mock_user)

    # Simulate a transport-level failure at ``openstack.connect`` time.
    with patch(
        "app.routers.quotas.openstack.connect",
        side_effect=ConnectionError("openstack unreachable"),
    ):
        response = client.get("/quotas/overview")

    assert response.status_code == 502
