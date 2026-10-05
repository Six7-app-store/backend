"""
Tests for the app release workflow (issue #70).

Coverage:
- Version submission (happy path, duplicate, resubmit-after-reject)
- Marker validation blocking submit (issue #82 follow-up)
- Admin approve / reject / revoke
- App listing visibility rules (public+approved vs private)
- git_link immutability via PUT /apps/{id}
- is_private field on create and update
"""
from unittest.mock import patch

import pytest

from app.crud import app_version_approvals as crud_approvals
from tests.conftest import create_app_in_db

# ================================================================
# APP LISTING VISIBILITY
# ================================================================

@pytest.mark.integration
def test_owner_always_sees_own_private_app(client, mock_user, db):
    create_app_in_db(db, mock_user, name="My Private App", is_private=True)
    resp = client.get("/apps/")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "My Private App" in names


@pytest.mark.integration
def test_student_cannot_see_public_app_without_approved_version(student_client, mock_user, db):
    create_app_in_db(db, mock_user, name="Public No Approval", is_private=False)
    resp = student_client.get("/apps/")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "Public No Approval" not in names


@pytest.mark.integration
def test_student_sees_public_app_after_approval(student_client, mock_admin, mock_user, db):
    app_obj = create_app_in_db(db, mock_user, name="Public Approved App", is_private=False)

    # Directly create approval via CRUD (avoids multi-client override conflicts)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")
    crud_approvals.approve(db, app_obj.appId, "v1.0", mock_admin.userId)

    resp = student_client.get("/apps/")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()]
    assert "Public Approved App" in names


# ================================================================
# VERSION SUBMISSION
# ================================================================

@pytest.mark.integration
def test_submit_version_creates_pending_entry(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    resp = client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["version_tag"] == "v1.0"


@pytest.mark.integration
def test_submit_version_duplicate_pending_returns_409(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    resp = client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    assert resp.status_code == 409


@pytest.mark.integration
def test_resubmit_after_rejection_succeeds(client, mock_admin, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    crud_approvals.submit_version(db, app_obj.appId, "v1.0")
    crud_approvals.reject(db, app_obj.appId, "v1.0", mock_admin.userId, "bad tofu")

    resp = client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


# ================================================================
# MARKER VALIDATION ON SUBMIT
# ================================================================

@pytest.mark.integration
def test_submit_blocked_when_marker_errors(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    broken_vars = [{"name": "flavor", "markerError": {
        "variable": "flavor",
        "message": "Unknown OS type: 'flaavoor'. Did you mean 'flavor'?",
        "location": "tofu/variables.tofu:5",
        "code": "MARKER_UNKNOWN_OS_TYPE",
    }}]
    with patch("app.routers.apps.load_variable_definitions", return_value=broken_vars):
        resp = client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "marker_errors" in detail
    assert detail["marker_errors"][0]["code"] == "MARKER_UNKNOWN_OS_TYPE"


@pytest.mark.integration
def test_submit_succeeds_with_clean_markers(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    clean_vars = [{"name": "flavor", "description": "@openstack:flavor", "osType": "flavor"}]
    with patch("app.routers.apps.load_variable_definitions", return_value=clean_vars):
        resp = client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


# ================================================================
# ADMIN APPROVE / REJECT / REVOKE
# ================================================================

@pytest.mark.integration
def test_admin_approve_sets_status_approved(client, admin_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    resp = admin_client.post(f"/admin/apps/{app_obj.appId}/versions/v1.0/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.integration
def test_admin_reject_requires_reason(client, admin_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    resp = admin_client.post(
        f"/admin/apps/{app_obj.appId}/versions/v1.0/reject",
        json={"rejection_reason": "insecure config"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["rejection_reason"] == "insecure config"


@pytest.mark.integration
def test_admin_reject_without_reason_returns_422(client, admin_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    resp = admin_client.post(
        f"/admin/apps/{app_obj.appId}/versions/v1.0/reject", json={}
    )
    assert resp.status_code == 422


@pytest.mark.integration
def test_admin_revoke_approved_version(client, admin_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    client.post(f"/apps/{app_obj.appId}/versions/v1.0/submit", json={})
    admin_client.post(f"/admin/apps/{app_obj.appId}/versions/v1.0/approve")
    resp = admin_client.post(
        f"/admin/apps/{app_obj.appId}/versions/v1.0/revoke",
        json={"rejection_reason": "security issue discovered post-approval"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["rejection_reason"] == "security issue discovered post-approval"


@pytest.mark.integration
def test_non_admin_cannot_access_admin_endpoints(client):
    resp = client.get("/admin/apps/versions/pending")
    assert resp.status_code == 403


@pytest.mark.integration
def test_admin_pending_queue_contains_submission(client, admin_client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    client.post(f"/apps/{app_obj.appId}/versions/v2.0/submit", json={})
    resp = admin_client.get("/admin/apps/versions/pending")
    assert resp.status_code == 200
    tags = [e["version_tag"] for e in resp.json()]
    assert "v2.0" in tags


# ================================================================
# GIT LINK IMMUTABILITY
# ================================================================

@pytest.mark.integration
def test_update_app_git_link_is_ignored(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    original_git_link = app_obj.git_link
    resp = client.put(
        f"/apps/{app_obj.appId}",
        json={"git_link": "https://github.com/evil/repo"},
    )
    assert resp.status_code == 200
    assert resp.json()["git_link"] == original_git_link


@pytest.mark.integration
def test_update_app_name_still_works(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user)
    resp = client.put(f"/apps/{app_obj.appId}", json={"name": "Renamed App"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed App"


# ================================================================
# IS_PRIVATE FIELD
# ================================================================

@pytest.mark.integration
def test_create_app_with_is_private(client):
    resp = client.post("/apps/", json={"name": "Secret App", "is_private": True})
    assert resp.status_code == 201
    assert resp.json()["is_private"] is True


@pytest.mark.integration
def test_toggle_privacy_via_put(client, mock_user, db):
    app_obj = create_app_in_db(db, mock_user, is_private=False)
    resp = client.put(f"/apps/{app_obj.appId}", json={"is_private": True})
    assert resp.status_code == 200
    assert resp.json()["is_private"] is True
