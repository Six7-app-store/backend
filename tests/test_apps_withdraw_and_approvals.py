"""
Phase B4 — Tests für Withdraw und Approval-History.

Coverage:
- Owner kann eine PENDING-Submission zurückziehen (204)
- Owner kann eine APPROVED-Version NICHT zurückziehen (409)
- Non-owner-Teacher kann nicht zurückziehen (403, Bug #2-Regression)
- Admin darf force-withdrawen
- Withdraw auf unbekannte Version → 404
- Listing der Approval-History für Owner, Admin und Studenten
  (privat vs. public+approved)
"""
import uuid

import pytest

from app.crud import app_version_approvals as crud_approvals
from app.database import get_db
from app.main import app as fastapi_app
from app.models import User, UserRole
from app.utils.auth import get_current_user
from tests.conftest import TestingSessionLocal, create_app_in_db


# ----------------------------------------------------------------
# Helper — zusätzlicher Teacher, der NICHT der App-Owner ist.
# Wir clonen die Override-Logik aus conftest hier minimal, damit
# wir gezielt einen Fremd-Teacher als aktiven User einsetzen können
# ohne die globalen Fixtures zu verändern.
# ----------------------------------------------------------------
def _make_other_teacher(db) -> User:
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="other-teacher-keycloak-id",
        email="other-teacher@dhbw.de",
        username="otherteacher",
        firstName="Other",
        lastName="Teacher",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _override_user(user: User) -> None:
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    def override_get_current_user():
        return user

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_current_user] = override_get_current_user


# ================================================================
# WITHDRAW
# ================================================================

@pytest.mark.integration
def test_owner_can_withdraw_pending_version(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")

    resp = client.delete(f"/apps/{app_obj.appId}/versions/v1.0/submit")
    assert resp.status_code == 204

    # Eintrag ist weg
    remaining = crud_approvals.get_approvals_for_app(db, app_obj.appId)
    assert not any(a.version_tag == "v1.0" for a in remaining)


@pytest.mark.integration
def test_owner_cannot_withdraw_approved_version_409(client, mock_user, mock_admin, db):
    app_obj = create_app_in_db(db, mock_user)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")
    crud_approvals.approve(db, app_obj.appId, "v1.0", mock_admin.userId)

    resp = client.delete(f"/apps/{app_obj.appId}/versions/v1.0/submit")
    assert resp.status_code == 409


@pytest.mark.integration
def test_non_owner_teacher_cannot_withdraw_403(client, mock_user, db):
    """Bug #2 Regression: ein Teacher darf eine fremde App nicht
    withdrawen — nur Owner oder Admin."""
    app_obj = create_app_in_db(db, mock_user)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")

    # Aktiven User auf einen anderen Teacher umstellen
    other = _make_other_teacher(db)
    _override_user(other)
    try:
        resp = client.delete(f"/apps/{app_obj.appId}/versions/v1.0/submit")
        assert resp.status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()

    # Eintrag existiert weiterhin
    remaining = crud_approvals.get_approvals_for_app(db, app_obj.appId)
    assert any(a.version_tag == "v1.0" for a in remaining)


@pytest.mark.integration
def test_admin_can_force_withdraw(admin_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")

    resp = admin_client.delete(f"/apps/{app_obj.appId}/versions/v1.0/submit")
    assert resp.status_code == 204

    remaining = crud_approvals.get_approvals_for_app(db, app_obj.appId)
    assert not any(a.version_tag == "v1.0" for a in remaining)


@pytest.mark.integration
def test_withdraw_unknown_version_404(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)

    resp = client.delete(f"/apps/{app_obj.appId}/versions/does-not-exist/submit")
    assert resp.status_code == 404


# ================================================================
# APPROVALS HISTORY
# ================================================================

@pytest.mark.integration
def test_list_version_approvals_returns_history_owner(client, mock_user, mock_admin, db):
    app_obj = create_app_in_db(db, mock_user)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")
    crud_approvals.reject(db, app_obj.appId, "v1.0", mock_admin.userId, "fix me")
    crud_approvals.submit_version(db, app_obj.appId, "v2.0")

    resp = client.get(f"/apps/{app_obj.appId}/versions")
    assert resp.status_code == 200

    tags = {e["version_tag"]: e["status"] for e in resp.json()}
    assert tags.get("v1.0") == "rejected"
    assert tags.get("v2.0") == "pending"


@pytest.mark.integration
def test_list_version_approvals_admin_sees_full_history(admin_client, mock_user, mock_admin, db):
    app_obj = create_app_in_db(db, mock_user, is_private=True)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")
    crud_approvals.approve(db, app_obj.appId, "v1.0", mock_admin.userId)
    crud_approvals.submit_version(db, app_obj.appId, "v2.0")

    resp = admin_client.get(f"/apps/{app_obj.appId}/versions")
    assert resp.status_code == 200

    tags = {e["version_tag"]: e["status"] for e in resp.json()}
    assert tags.get("v1.0") == "approved"
    assert tags.get("v2.0") == "pending"


@pytest.mark.integration
def test_list_version_approvals_student_403_for_private_app(student_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user, is_private=True)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")

    resp = student_client.get(f"/apps/{app_obj.appId}/versions")
    assert resp.status_code == 403


@pytest.mark.integration
def test_list_version_approvals_student_403_even_for_public_approved_app(
    student_client, mock_user, mock_admin, db
):
    """Phase-2-Bug-#2-fix: ``GET /apps/{id}/versions`` ist owner-/admin-
    only, unabhängig vom Veröffentlichungs-/Approval-Status der App.
    Selbst eine public+approved App liefert für Studenten 403 zurück —
    Version-History gehört nicht in den Storefront-View."""
    app_obj = create_app_in_db(db, mock_user, is_private=False)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")
    crud_approvals.approve(db, app_obj.appId, "v1.0", mock_admin.userId)

    resp = student_client.get(f"/apps/{app_obj.appId}/versions")
    assert resp.status_code == 403
