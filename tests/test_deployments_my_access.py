"""Integration tests for GET /deployments/{id}/my-access.

The endpoint lets a team member (typically a student) fetch THEIR OWN
access credentials without the owner-view that gates the full outputs
payload. These tests cover the access-control matrix and — most
importantly — the guarantee that a member never receives a teammate's
credentials in the response.

Unlike the resend-access tests, the notifier is NOT patched out here:
we seed a real successful DEPLOY task carrying terraform ``user_accounts``
outputs and assert on the extracted, filtered response so the end-to-end
extraction path (crud → notifier._find_account_for_user) is exercised.
"""
import json
import uuid
from datetime import datetime

import pytest

from app.models import (
    App,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    Team,
    UserToTeam,
)


def _seed_deployment_with_outputs(db, owner, members, *, outputs=None, team_name="Team-1"):
    """Seed app + deployment + one team + memberships + a successful
    DEPLOY task carrying ``outputs`` (parsed dict → stored as JSON).

    ``members`` is a list of User objects added to the team. Pass
    ``outputs=None`` to seed a deploy task with no outputs (the
    "no credentials yet" case).
    """
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
            status=TaskStatus.SUCCESS,
            outputs=json.dumps(outputs) if outputs is not None else None,
            created_at=datetime.utcnow(),
        )
    )

    team = Team(
        teamId=uuid.uuid4(),
        name=team_name,
        deploymentId=deployment.deploymentId,
    )
    db.add(team)
    db.flush()

    for member in members:
        db.add(
            UserToTeam(
                userToTeamId=uuid.uuid4(),
                userId=member.userId,
                teamId=team.teamId,
            )
        )

    db.commit()
    db.refresh(deployment)
    return deployment, team


def _outputs_for(team_name, *accounts):
    """Build a terraform-outputs dict with a ``user_accounts`` map.

    Each ``accounts`` entry is ``(key_suffix, username, password)``; the
    map key is ``"<team>-<key_suffix>"`` mirroring the template contract.
    """
    user_accounts = {}
    for key_suffix, username, password in accounts:
        user_accounts[f"{team_name}-{key_suffix}"] = {
            "username": username,
            "auth": password,
            "type": "password",
            "ip": "1.2.3.4",
            "port": 8080,
        }
    return {
        "user_accounts": {"value": user_accounts},
        "team_vms": {
            "value": {team_name: {"url": "http://1.2.3.4:8080", "floating_ip": "1.2.3.4"}}
        },
    }


@pytest.mark.integration
def test_my_access_member_gets_own_account(client, db, mock_user, mock_student):
    """A team member retrieves their own credentials (200, one key)."""
    # mock_student.email == "student@dhbw.de" → local-part "student".
    outputs = _outputs_for(
        "Team-1",
        ("student", "student", "s3cret-student"),
        ("owner", "owneruser", "pw-owner"),
    )
    deployment, team = _seed_deployment_with_outputs(
        db, owner=mock_user, members=[mock_student, mock_user], outputs=outputs,
    )

    from app.main import app as fastapi_app
    from app.utils.auth import get_current_user
    fastapi_app.dependency_overrides[get_current_user] = lambda: mock_student

    response = client.get(f"/deployments/{deployment.deploymentId}/my-access")

    assert response.status_code == 200, response.text
    body = response.json()
    accounts = body["user_accounts"]
    assert len(accounts) == 1
    assert "Team-1-student" in accounts
    assert accounts["Team-1-student"]["auth"] == "s3cret-student"
    # The team VM block is included for the URL pill.
    assert "Team-1" in body["team_vms"]


@pytest.mark.integration
def test_my_access_never_leaks_teammate_credentials(client, db, mock_user, mock_student):
    """Core security guarantee: the response must NOT contain any other
    member's account key or password, even though the raw outputs carry
    every teammate's credentials."""
    outputs = _outputs_for(
        "Team-1",
        ("student", "student", "s3cret-student"),
        ("owner", "owneruser", "pw-owner-SECRET"),
    )
    deployment, _team = _seed_deployment_with_outputs(
        db, owner=mock_user, members=[mock_student, mock_user], outputs=outputs,
    )

    from app.main import app as fastapi_app
    from app.utils.auth import get_current_user
    fastapi_app.dependency_overrides[get_current_user] = lambda: mock_student

    response = client.get(f"/deployments/{deployment.deploymentId}/my-access")

    assert response.status_code == 200, response.text
    serialised = json.dumps(response.json())
    assert "Team-1-owner" not in serialised
    assert "pw-owner-SECRET" not in serialised


@pytest.mark.integration
def test_my_access_unrelated_student_403(student_client, db, mock_user, mock_student):
    """A student with no relation to the deployment (not in any team)
    is rejected at the member-view gate with 403 — not a data-less 200."""
    outputs = _outputs_for("Team-1", ("owner", "owneruser", "pw-owner"))
    # mock_student is deliberately NOT added as a member.
    deployment, _team = _seed_deployment_with_outputs(
        db, owner=mock_user, members=[mock_user], outputs=outputs,
    )

    response = student_client.get(f"/deployments/{deployment.deploymentId}/my-access")

    assert response.status_code == 403, response.text


@pytest.mark.integration
def test_my_access_empty_when_no_successful_deploy(client, db, mock_user, mock_student):
    """No outputs yet → 200 with empty maps (clean 'no credentials' state,
    not a 404/500)."""
    deployment, _team = _seed_deployment_with_outputs(
        db, owner=mock_user, members=[mock_student], outputs=None,
    )

    from app.main import app as fastapi_app
    from app.utils.auth import get_current_user
    fastapi_app.dependency_overrides[get_current_user] = lambda: mock_student

    response = client.get(f"/deployments/{deployment.deploymentId}/my-access")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_accounts"] == {}
    assert body["team_vms"] == {}


@pytest.mark.integration
def test_my_access_owner_gets_own_account(client, db, mock_user):
    """The owner uses the same endpoint and gets their own account back
    (no regression for the owner-view caller)."""
    # mock_user.email == "test@dhbw.de" → local-part "test".
    outputs = _outputs_for("Team-1", ("test", "testuser", "pw-owner"))
    deployment, _team = _seed_deployment_with_outputs(
        db, owner=mock_user, members=[mock_user], outputs=outputs,
    )

    response = client.get(f"/deployments/{deployment.deploymentId}/my-access")

    assert response.status_code == 200, response.text
    accounts = response.json()["user_accounts"]
    assert len(accounts) == 1
    assert "Team-1-test" in accounts


@pytest.mark.integration
def test_my_access_404_for_unknown_deployment(client):
    """Unknown deployment id → 404."""
    response = client.get(f"/deployments/{uuid.uuid4()}/my-access")
    assert response.status_code == 404
