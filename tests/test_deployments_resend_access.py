"""Integration tests for POST /deployments/{id}/teams/{team}/users/{user_id}/resend-access.

These tests cover the access-control matrix and the upstream-failure
mapping for the resend-access endpoint in
``app/routers/deployments.py``. The actual ``deployment_notifier.
resend_user_access`` call is patched out — we only assert that the
endpoint reaches the notifier with the right arguments (or short-
circuits with the correct HTTP code before reaching it).
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
    Team,
    UserOpenStackCredential,
    UserToTeam,
)


@pytest.fixture(autouse=True)
def _smtp_enabled(monkeypatch):
    """Default every resend-access test into 'SMTP is on' so the
    new kill-switch in the endpoint does not short-circuit with 503
    before the real test logic runs.

    Tests that specifically exercise the disabled path override this
    fixture explicitly by calling ``monkeypatch.setattr`` again on
    the same setting — pytest applies the closest override.

    ``raising=False`` lets us patch even when an older test config
    forgot to declare the field (defensive — the field exists today
    but we don't want a refactor surfacing here).
    """
    monkeypatch.setattr(settings, "SMTP_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SMTP_USER", "test@example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "test-password", raising=False)


def _seed_deployment_with_team(
    db,
    owner,
    member=None,
    *,
    deploy_status: TaskStatus = TaskStatus.SUCCESS,
):
    """Seed an app + deployment + team + (optional) team membership +
    one latest DEPLOY task with the given status. Mirrors the helper
    in test_deployments_pause_resume.py but adds team plumbing so the
    resend endpoint has a valid team/user pair to address.
    """
    from app.utils import crypto

    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=owner.userId,
        git_link="https://example.com/repo.git",
    )
    db.add(app)
    db.flush()

    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name=f"d-{uuid.uuid4().hex[:8]}",
        appId=app.appId,
        userId=owner.userId,
        releaseTag="v1.0.0",
        userInputVar=json.dumps({"terraform": {}, "packer": {}}),
    )
    db.add(deployment)
    db.flush()

    db.add(
        Task(
            taskId=uuid.uuid4(),
            deploymentId=deployment.deploymentId,
            type=TaskType.DEPLOY,
            status=deploy_status,
            created_at=datetime.utcnow(),
        )
    )

    team = Team(
        teamId=uuid.uuid4(),
        name="Team-1",
        deploymentId=deployment.deploymentId,
    )
    db.add(team)
    db.flush()

    if member is not None:
        db.add(
            UserToTeam(
                userToTeamId=uuid.uuid4(),
                userId=member.userId,
                teamId=team.teamId,
            )
        )

    db.add(
        UserOpenStackCredential(
            credentialId=uuid.uuid4(),
            userId=owner.userId,
            auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
            auth_url="https://keystone.example/v3",
            encrypted_identifier=crypto.encrypt("test-id"),
            encrypted_secret=crypto.encrypt("test-secret"),
        )
    )
    db.commit()
    db.refresh(deployment)
    return deployment, team


@pytest.mark.integration
def test_resend_access_owner_dispatches_notifier(client, db, mock_user):
    """Owner (creator) hits the endpoint for some member — notifier
    is called with the URL path arguments and 202 is returned."""
    deployment, team = _seed_deployment_with_team(db, mock_user, member=mock_user)
    target_user = mock_user.userId

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=True,
    ) as m_resend:
        response = client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{target_user}"
            f"/resend-access"
        )

    assert response.status_code == 202, response.text
    assert response.json() == {"status": "sent"}
    m_resend.assert_called_once()
    # Positional args: (db, deployment_id, team_id, user_id)
    call_args = m_resend.call_args.args
    assert str(call_args[1]) == str(deployment.deploymentId)
    assert str(call_args[2]) == str(team.teamId)
    assert str(call_args[3]) == str(target_user)


@pytest.mark.integration
def test_resend_access_team_member_self_dispatches(client, db, mock_user, mock_student):
    """A plain team member (non-owner-view) may resend the mail for
    themself. The deployment owner is ``mock_user``, the request comes
    from ``mock_student`` via the student_client fixture-pattern, but
    we just override the keycloak dependency inline here.
    """
    deployment, team = _seed_deployment_with_team(
        db, owner=mock_user, member=mock_student,
    )

    # Swap auth to the student so this is the "member self-resend" path.
    from app.main import app as fastapi_app
    from app.utils.auth import get_current_user
    fastapi_app.dependency_overrides[get_current_user] = lambda: mock_student

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=True,
    ) as m_resend:
        response = client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{mock_student.userId}"
            f"/resend-access"
        )

    assert response.status_code == 202, response.text
    assert response.json() == {"status": "sent"}
    m_resend.assert_called_once()


@pytest.mark.integration
def test_resend_access_admin_can_resend_for_other_user(
    admin_client, db, mock_user, mock_admin,
):
    """Admins get the owner view of every deployment, so they may
    resend the mail for any team member of any deployment — even one
    they didn't create."""
    deployment, team = _seed_deployment_with_team(
        db, owner=mock_user, member=mock_user,
    )

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=True,
    ) as m_resend:
        response = admin_client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{mock_user.userId}"
            f"/resend-access"
        )

    assert response.status_code == 202, response.text
    assert response.json() == {"status": "sent"}
    m_resend.assert_called_once()


@pytest.mark.integration
def test_resend_access_unrelated_student_403(
    student_client, db, mock_user, mock_student,
):
    """A student who has no relation to the deployment (not in any of
    its teams, no direct UserToDeployment row) must be rejected at the
    ``ensure_deployment_access`` gate with a 403 — not a 404, since
    leaking existence by error code would be an enumeration vector.
    """
    # Owner is mock_user; no membership added for mock_student.
    deployment, team = _seed_deployment_with_team(db, owner=mock_user)

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=True,
    ) as m_resend:
        response = student_client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{mock_student.userId}"
            f"/resend-access"
        )

    assert response.status_code == 403, response.text
    m_resend.assert_not_called()


@pytest.mark.integration
def test_resend_access_when_notifier_raises_returns_502(client, db, mock_user):
    """When the notifier returns ``False`` (template render OK but
    SMTP rejected the message), the endpoint must surface this as a
    502 Bad Gateway with ``reason=smtp_send_failed`` so the frontend
    can offer a retry instead of a misleading 4xx.
    """
    deployment, team = _seed_deployment_with_team(db, mock_user, member=mock_user)

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=False,
    ) as m_resend:
        response = client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{mock_user.userId}"
            f"/resend-access"
        )

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["detail"]["reason"] == "smtp_send_failed"
    m_resend.assert_called_once()


@pytest.mark.integration
def test_resend_access_404_for_unknown_user(client, db, mock_user):
    """Notifier raises ``ResendError('user_not_in_team')`` — the
    endpoint maps that ResendError reason to a 404 with the structured
    detail body. Same mapping covers ``team_not_in_deployment`` and
    ``deployment_not_found``; ``user_not_in_team`` is the realistic
    case here since the team exists but a random UUID was passed.
    """
    from app.services import deployment_notifier as notifier_module

    deployment, team = _seed_deployment_with_team(db, mock_user, member=mock_user)
    unknown_user_id = uuid.uuid4()

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        side_effect=notifier_module.ResendError("user_not_in_team"),
    ) as m_resend:
        response = client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{unknown_user_id}"
            f"/resend-access"
        )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["detail"]["reason"] == "user_not_in_team"
    m_resend.assert_called_once()


# ----------------------------------------------------------------
# SMTP kill-switch (``SMTP_ENABLED=False``) — endpoint short-circuits
# with 503 ``smtp_disabled`` BEFORE invoking the notifier. The
# distinction from 502 ``smtp_send_failed`` is intentional: 503 means
# "operator chose not to send" (configuration), 502 means "we tried
# and SMTP refused" (infrastructure). The frontend toast text branches
# on the reason so users understand whether to ping an admin or just
# retry.
# ----------------------------------------------------------------
@pytest.mark.integration
def test_resend_access_returns_503_when_smtp_disabled(
    client, db, mock_user, monkeypatch,
):
    """SMTP_ENABLED=false → 503 with reason=smtp_disabled. The notifier
    must NOT be called; the deployment lookup may happen first but
    the only externally visible effect is the 503 short-circuit.
    """
    monkeypatch.setattr(settings, "SMTP_ENABLED", False, raising=False)
    deployment, team = _seed_deployment_with_team(db, mock_user, member=mock_user)

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=True,
    ) as m_resend:
        response = client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{mock_user.userId}"
            f"/resend-access"
        )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["detail"]["reason"] == "smtp_disabled"
    # Retry-After header advertises the recommended back-off; clients
    # may use it to throttle a "try again later" affordance.
    assert "retry-after" in {k.lower() for k in response.headers}
    m_resend.assert_not_called()


@pytest.mark.integration
def test_resend_access_returns_503_when_smtp_credentials_missing(
    client, db, mock_user, monkeypatch,
):
    """SMTP_ENABLED=True but credentials empty → still 503 (treated as
    'configuration in progress'). Same reason code as the explicit
    kill-switch — the frontend doesn't need to distinguish, and
    leaking 'credentials are missing' through a separate reason
    would be a small information disclosure.
    """
    monkeypatch.setattr(settings, "SMTP_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SMTP_USER", "", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "", raising=False)
    deployment, team = _seed_deployment_with_team(db, mock_user, member=mock_user)

    with patch(
        "app.routers.deployments.deployment_notifier.resend_user_access",
        return_value=True,
    ) as m_resend:
        response = client.post(
            f"/deployments/{deployment.deploymentId}"
            f"/teams/{team.teamId}"
            f"/users/{mock_user.userId}"
            f"/resend-access"
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == "smtp_disabled"
    m_resend.assert_not_called()
