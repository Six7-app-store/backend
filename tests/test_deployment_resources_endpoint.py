"""API-level tests for the per-deployment resource endpoints.

Covers:
 * GET /deployments/{id}/resources                       (Stage-1)
 * GET /deployments/{id}/resources/{address}             (Stage-2)
 * POST /deployments/{id}/resources/{address}/redeploy   (whitelist + dispatch)

The OpenStack ``Connection`` is mocked end-to-end via
``app.services.openstack_client.user_connection`` so tests don't need a
real cloud — we control what the live-fetch returns, including the
"server gone" case that drives ``drift=missing``.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.models import (
    App,
    Deployment,
    OpenStackAuthType,
    Task,
    TaskStatus,
    TaskType,
    User,
    UserOpenStackCredential,
)

# ----------------------------------------------------------------
# Helpers / fixtures
# ----------------------------------------------------------------
TEAM_A = "Team-A"
TEAM_B = "Team-B"
ADDR_A = f'openstack_compute_instance_v2.team_ide["{TEAM_A}"]'
ADDR_B = f'openstack_compute_instance_v2.team_ide["{TEAM_B}"]'
ADDR_NETWORK = "openstack_networking_network_v2.shared"

_DEMO_STATE = {
    "version": 4,
    "resources": [
        {
            "type": "openstack_compute_instance_v2",
            "name": "team_ide",
            "instances": [
                {
                    "index_key": TEAM_A,
                    "attributes": {
                        "id": "uuid-vm-a",
                        "name": "online-ide-Team-A",
                        "metadata": {"team": TEAM_A},
                    },
                },
                {
                    "index_key": TEAM_B,
                    "attributes": {
                        "id": "uuid-vm-b",
                        "name": "online-ide-Team-B",
                        "metadata": {"team": TEAM_B},
                    },
                },
            ],
        },
        {
            "type": "openstack_networking_network_v2",
            "name": "shared",
            "instances": [
                {"attributes": {"id": "uuid-net", "name": "shared-net"}},
            ],
        },
    ],
}


def _ensure_user_credentials(db, user):
    """User needs credentials so ``user_connection`` doesn't 412."""
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


def _seed_deployment_with_state(db, user: User, app_row: App) -> Deployment:
    """Create a deployment + a successful DEPLOY task carrying the
    demo TF state. The resource endpoint reads from the most-recent
    task with a non-null state, so one task is enough."""
    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name="resource-test",
        appId=app_row.appId,
        userId=user.userId,
        releaseTag="main",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)

    task = Task(
        taskId=uuid.uuid4(),
        deploymentId=deployment.deploymentId,
        celeryTaskId="fake-celery-id",
        type=TaskType.DEPLOY,
        status=TaskStatus.SUCCESS,
        tf_state=json.dumps(_DEMO_STATE),
    )
    db.add(task)
    db.commit()
    return deployment


def _make_server_mock(
    *,
    server_id: str,
    status: str = "ACTIVE",
    task_state: str | None = None,
    fault_message: str | None = None,
):
    """Build an SDK-shaped server mock the live-joiner accepts."""
    server = MagicMock()
    server.id = server_id
    server.status = status
    server.task_state = task_state
    server.vm_state = "active" if status == "ACTIVE" else "stopped"
    server.power_state = 1 if status == "ACTIVE" else 4
    server.fault = {"message": fault_message} if fault_message else None
    server.flavor = {
        "original_name": "m1.small",
        "ram": 2048,
        "vcpus": 1,
        "disk": 20,
    }
    server.image = {"id": "image-uuid"}
    server.availability_zone = "nova"
    server.launched_at = "2026-06-24T12:00:00Z"
    server.addresses = {
        "shared-net": [
            {"addr": "10.0.0.10", "OS-EXT-IPS:type": "fixed"},
            {"addr": "10.0.0.20", "OS-EXT-IPS:type": "floating"},
        ]
    }
    server.metadata = {"team": "Team-A"}
    return server


@pytest.fixture
def patched_user_connection():
    """Replace the ``user_connection`` contextmanager with a fake that
    yields a configured Mock-Connection. Tests can mutate
    ``conn.compute.find_server.side_effect`` per case."""
    conn = MagicMock()

    @contextmanager
    def _fake_conn(_db, _user):
        yield conn

    # Patch both call sites — the resource endpoint and any other
    # importers — so the import-time binding doesn't slip past the
    # mock.
    with patch(
        "app.services.deployment_status.user_connection", _fake_conn
    ):
        yield conn


@pytest.fixture
def patched_celery_send():
    class _FakeAsyncResult:
        id = "fake-celery-task-id"

    with patch(
        "app.services.task_service.celery_app.send_task",
        return_value=_FakeAsyncResult(),
    ) as m:
        yield m


# ----------------------------------------------------------------
# GET /resources — Stage 1
# ----------------------------------------------------------------
@pytest.mark.integration
def test_list_resources_joins_live_status(
    client, db, mock_user, patched_user_connection
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    # Two VMs: A is healthy, B was deleted out-of-band so live-fetch
    # returns None → drift=missing.
    def _find_server(server_id, ignore_missing=True):
        if server_id == "uuid-vm-a":
            return _make_server_mock(server_id="uuid-vm-a", status="ACTIVE")
        return None

    patched_user_connection.compute.find_server.side_effect = _find_server

    response = client.get(f"/deployments/{deployment.deploymentId}/resources")
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is True
    by_team = {r["team"]: r for r in body["resources"] if r["category"] == "instance"}
    assert by_team[TEAM_A]["drift"] == "in_sync"
    assert by_team[TEAM_A]["lifecycle"]["status"] == "ACTIVE"
    assert by_team[TEAM_A]["hardware"]["flavor_name"] == "m1.small"
    assert by_team[TEAM_B]["drift"] == "missing"
    # Network resource travels through with no live data
    network = next(r for r in body["resources"] if r["category"] == "network")
    assert network["address"] == ADDR_NETWORK
    assert network["lifecycle"] is None


@pytest.mark.integration
def test_list_resources_skip_refresh_returns_cached_only(
    client, db, mock_user, patched_user_connection
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    response = client.get(
        f"/deployments/{deployment.deploymentId}/resources",
        params={"refresh": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is False
    # ``find_server`` MUST NOT have been called
    assert not patched_user_connection.compute.find_server.called
    # Cached fields survive without lifecycle/hardware enrichment
    for r in body["resources"]:
        assert r["lifecycle"] is None


@pytest.mark.integration
def test_list_resources_owner_only(
    client, db, mock_user, mock_student, patched_user_connection
):
    """A student who doesn't own the deployment must not see the
    resource panel — the data exposed (live OpenStack lifecycle) is
    owner-scope."""
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    # Re-patch the auth to the student
    from app.main import app
    from app.utils.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: mock_student
    try:
        response = client.get(f"/deployments/{deployment.deploymentId}/resources")
    finally:
        app.dependency_overrides[get_current_user] = lambda: mock_user
    assert response.status_code == 403


# ----------------------------------------------------------------
# GET /resources/{address} — Stage 2
# ----------------------------------------------------------------
@pytest.mark.integration
def test_resource_detail_loads_stage2(
    client, db, mock_user, patched_user_connection
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    patched_user_connection.compute.find_server.return_value = _make_server_mock(
        server_id="uuid-vm-a"
    )
    # Stage-2 specific mocks
    port = MagicMock()
    port.id = "port-1"
    port.network_id = "net-1"
    port.status = "ACTIVE"
    port.mac_address = "fa:16:00:00:00:01"
    port.fixed_ips = [{"ip_address": "10.0.0.10", "subnet_id": "sn-1"}]
    port.security_group_ids = ["sg-1"]
    patched_user_connection.network.ports.return_value = iter([port])

    sg = MagicMock()
    sg.id = "sg-1"
    sg.name = "default"
    sg.description = "Default SG"
    sg.security_group_rules = [
        {"direction": "ingress"},
        {"direction": "ingress"},
        {"direction": "egress"},
    ]
    patched_user_connection.network.find_security_group.return_value = sg

    image = MagicMock()
    image.name = "ubuntu-22.04"
    patched_user_connection.image.find_image.return_value = image

    attachment = MagicMock()
    attachment.volume_id = "vol-1"
    attachment.device = "/dev/vdb"
    patched_user_connection.compute.volume_attachments.return_value = iter([attachment])

    volume = MagicMock()
    volume.size = 50
    volume.is_bootable = True
    volume.status = "in-use"
    volume.name = "data-disk"
    patched_user_connection.block_storage.find_volume.return_value = volume

    response = client.get(
        f"/deployments/{deployment.deploymentId}/resources/{ADDR_A}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["address"] == ADDR_A
    assert body["hardware"]["image_name"] == "ubuntu-22.04"
    assert len(body["ports"]) == 1
    assert body["ports"][0]["status"] == "ACTIVE"
    assert len(body["security_groups"]) == 1
    assert body["security_groups"][0]["ingress_rules"] == 2
    assert body["security_groups"][0]["egress_rules"] == 1
    assert len(body["volumes"]) == 1
    assert body["volumes"][0]["size_gb"] == 50
    assert body["volumes"][0]["bootable"] is True


@pytest.mark.integration
def test_resource_detail_unknown_address_returns_404(
    client, db, mock_user, patched_user_connection
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    bogus = 'openstack_compute_instance_v2.team_ide["Team-Z"]'
    response = client.get(
        f"/deployments/{deployment.deploymentId}/resources/{bogus}"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "resource_not_in_state"


@pytest.mark.integration
def test_resource_detail_invalid_address_returns_422(
    client, db, mock_user, patched_user_connection
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    # Pipes/semicolons aren't valid in a TF address — must be rejected
    # by the regex before the state lookup even runs.
    response = client.get(
        f"/deployments/{deployment.deploymentId}/resources/foo;rm -rf /"
    )
    assert response.status_code == 422


# ----------------------------------------------------------------
# POST /resources/{address}/redeploy — whitelist + dispatch
# ----------------------------------------------------------------
@pytest.mark.integration
def test_redeploy_resource_dispatches_celery_task(
    client, db, mock_user, patched_user_connection, patched_celery_send
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    response = client.post(
        f"/deployments/{deployment.deploymentId}/resources/{ADDR_A}/redeploy"
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "redeploying"

    # Celery was called with the redeploy task name and the address as
    # the extra positional arg.
    patched_celery_send.assert_called_once()
    name = patched_celery_send.call_args.args[0]
    args = patched_celery_send.call_args.kwargs["args"]
    assert name == "tasks.redeploy_resource"
    # 7 standard args + 1 extra (resource_address)
    assert len(args) == 8
    assert args[-1] == ADDR_A


@pytest.mark.integration
def test_redeploy_rejects_network_address(
    client, db, mock_user, patched_user_connection, patched_celery_send
):
    """Redeploying a network would tear down all team VMs — backend
    must reject it before the Celery dispatch."""
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    response = client.post(
        f"/deployments/{deployment.deploymentId}/resources/{ADDR_NETWORK}/redeploy"
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "non_redeployable_resource_type"
    assert not patched_celery_send.called


@pytest.mark.integration
def test_redeploy_rejects_unknown_address(
    client, db, mock_user, patched_user_connection, patched_celery_send
):
    _ensure_user_credentials(db, mock_user)
    app_row = _ensure_app(db, mock_user)
    deployment = _seed_deployment_with_state(db, mock_user, app_row)

    bogus = 'openstack_compute_instance_v2.team_ide["Team-Z"]'
    response = client.post(
        f"/deployments/{deployment.deploymentId}/resources/{bogus}/redeploy"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "resource_not_in_state"
    assert not patched_celery_send.called
