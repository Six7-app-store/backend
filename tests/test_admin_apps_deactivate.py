"""Tests for the admin emergency deactivation endpoint.

Endpoint under test: ``PUT /admin/apps/{app_id}`` (admin_apps.deactivate_app).

The endpoint flips the app's ``is_private`` flag to ``True`` so the app
disappears from the student-facing storefront immediately. It does **not**
delete the app, and existing deployments must remain intact. Re-activation
(making the app public again) happens via the regular owner/admin update
endpoint ``PUT /apps/{app_id}`` with ``is_private=False``.

Auth-Context-Switching: ``admin_client`` und ``student_client`` aus
``conftest.py`` mutieren beide den **gleichen** globalen Slot
``app.dependency_overrides[get_current_user]``. Wenn ein Test
beide Fixtures gleichzeitig anfordert, gewinnt die zuletzt aufgelöste
Fixture — alle nachfolgenden Requests laufen dann als deren User,
unabhängig davon, ob sie über ``admin_client`` oder ``student_client``
abgesetzt werden. Tests, die innerhalb eines Durchlaufs zwischen
Rollen wechseln müssen, nutzen deshalb ``_as(user)`` zum manuellen
Hot-Swap auf einem einzigen Client.
"""

import uuid
from datetime import datetime

import pytest

from app.main import app as fastapi_app
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Deployment,
)
from app.utils.auth import get_current_user
from tests.conftest import create_app_in_db


def _approve_version(db, app_id, reviewer_id, version_tag="v1.0.0"):
    """Create an APPROVED version record so the app would normally be
    visible to non-owners in the storefront."""
    approval = AppVersionApproval(
        approvalId=uuid.uuid4(),
        appId=app_id,
        version_tag=version_tag,
        status=AppVersionApprovalStatus.APPROVED,
        reviewed_by=reviewer_id,
        reviewed_at=datetime.utcnow(),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def _as(user):
    """Swap the active ``get_current_user`` override.

    Workaround für die Fixture-Override-Kollision (siehe Modul-
    Docstring): ein Test kann nicht zwei Clients gleichzeitig
    anfordern, weil beide am selben globalen Slot drehen. Stattdessen
    nehmen wir einen Client und tauschen den Auth-Kontext zwischen
    Requests aus."""
    fastapi_app.dependency_overrides[get_current_user] = lambda: user


# ----------------------------------------------------------------
# 1) ADMIN CAN DEACTIVATE — APP DISAPPEARS FROM STUDENT VIEW
# ----------------------------------------------------------------
@pytest.mark.integration
def test_admin_can_deactivate_app_hides_from_students(
    admin_client, db, mock_user, mock_admin, mock_student
):
    # arrange — public app with an approved version, so a student
    # would normally see it in ``GET /apps/``.
    app = create_app_in_db(
        db, mock_user, name="Public App", is_private=False
    )
    _approve_version(db, app.appId, mock_admin.userId)

    # sanity check: student sees the app before deactivation
    _as(mock_student)
    pre = admin_client.get("/apps/")
    assert pre.status_code == 200
    assert any(a["appId"] == str(app.appId) for a in pre.json())

    # act — admin deactivates the app
    _as(mock_admin)
    response = admin_client.put(f"/admin/apps/{app.appId}")

    # assert — endpoint returns the updated app, now is_private=True
    assert response.status_code == 200
    body = response.json()
    assert body["appId"] == str(app.appId)
    assert body["is_private"] is True

    # student no longer sees the app in the storefront
    _as(mock_student)
    post = admin_client.get("/apps/")
    assert post.status_code == 200
    assert all(a["appId"] != str(app.appId) for a in post.json())


# ----------------------------------------------------------------
# 2) DEACTIVATION PRESERVES EXISTING DEPLOYMENTS
# ----------------------------------------------------------------
@pytest.mark.integration
def test_deactivate_app_preserves_existing_deployments(
    admin_client, db, mock_user
):
    # arrange — app with a running deployment
    app = create_app_in_db(db, mock_user, name="With Deployment", is_private=False)
    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name="dep-1",
        userId=mock_user.userId,
        appId=app.appId,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    deployment_id = deployment.deploymentId

    # act — admin deactivates the app
    response = admin_client.put(f"/admin/apps/{app.appId}")
    assert response.status_code == 200
    assert response.json()["is_private"] is True

    # assert — the app row is still there and not soft-deleted, and
    # the deployment row is untouched
    db.expire_all()
    db_app = db.query(App).filter(App.appId == app.appId).one()
    assert db_app.is_private is True
    assert db_app.deleted_at is None

    db_dep = (
        db.query(Deployment)
        .filter(Deployment.deploymentId == deployment_id)
        .one_or_none()
    )
    assert db_dep is not None
    assert db_dep.appId == app.appId
    assert db_dep.deleted_at is None


# ----------------------------------------------------------------
# 3) NON-ADMIN FORBIDDEN
# ----------------------------------------------------------------
@pytest.mark.integration
def test_non_admin_deactivate_403(client, db, mock_user):
    # arrange — teacher owns an app and tries to hit the admin endpoint
    app = create_app_in_db(db, mock_user, name="Teacher App", is_private=False)

    # act
    response = client.put(f"/admin/apps/{app.appId}")

    # assert
    assert response.status_code == 403
    # app is unchanged
    db.expire_all()
    db_app = db.query(App).filter(App.appId == app.appId).one()
    assert db_app.is_private is False


# ----------------------------------------------------------------
# 4) UNKNOWN APP → 404
# ----------------------------------------------------------------
@pytest.mark.integration
def test_deactivate_unknown_app_404(admin_client):
    response = admin_client.put(f"/admin/apps/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "App not found"


# ----------------------------------------------------------------
# 5) RE-ACTIVATION RESTORES VISIBILITY
# ----------------------------------------------------------------
@pytest.mark.integration
def test_reactivate_after_deactivation_restores_visibility(
    admin_client, db, mock_user, mock_admin, mock_student
):
    # arrange — public app with an approved version, then deactivate
    app = create_app_in_db(db, mock_user, name="Toggleable", is_private=False)
    _approve_version(db, app.appId, mock_admin.userId)

    _as(mock_admin)
    deactivate = admin_client.put(f"/admin/apps/{app.appId}")
    assert deactivate.status_code == 200
    assert deactivate.json()["is_private"] is True

    _as(mock_student)
    hidden = admin_client.get("/apps/")
    assert all(a["appId"] != str(app.appId) for a in hidden.json())

    # act — admin re-activates via the regular update endpoint
    _as(mock_admin)
    reactivate = admin_client.put(
        f"/apps/{app.appId}", json={"is_private": False}
    )

    # assert — flag is back to False, student sees the app again
    assert reactivate.status_code == 200
    assert reactivate.json()["is_private"] is False

    _as(mock_student)
    visible = admin_client.get("/apps/")
    assert visible.status_code == 200
    assert any(a["appId"] == str(app.appId) for a in visible.json())
