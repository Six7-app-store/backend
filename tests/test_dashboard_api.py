"""Tests for ``GET /dashboard/stats`` — Phase B2.

Covers the role-branched aggregation contract spelled out in
``app/routers/dashboard.py``:

  * Admin: counts every non-deleted app / their own deployments / all
    courses (apps mirrors ``crud_apps.get_apps`` — platform-wide view).
  * Teacher: counts own apps + public-approved apps (Bug #6 fix — the
    blanket staff view is gone) and deployments they own.
  * Student: counts own apps + public-approved apps, plus deployments
    they own OR are in a team of OR have a direct UserToDeployment
    mapping for. Soft-deleted rows excluded.
  * Unauthenticated callers must be rejected with 401.

The fixture below stands up five Apps and five Deployments with mixed
ownership/visibility/approval state so each test can read off the
exact integer expected for the role under test.
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app as fastapi_app
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Course,
    Deployment,
    Team,
    User,
    UserRole,
    UserToDeployment,
    UserToTeam,
)
from app.utils.auth import get_current_user
from tests.conftest import TestingSessionLocal


# ---------------------------------------------------------------------------
# Shared world builder
# ---------------------------------------------------------------------------
def _seed_world(db, *, teacher: User, admin: User, student: User):
    """Build the 5-apps / 5-deployments fixture used by all counter tests.

    Apps
    ----
    A1: owned by teacher, public, APPROVED   -> visible to teacher/student/admin
    A2: owned by teacher, public, PENDING    -> visible to teacher only (own)
    A3: owned by teacher, PRIVATE            -> visible to teacher only (own)
    A4: owned by admin,   public, APPROVED   -> visible to teacher/student/admin
    A5: owned by other-student, PRIVATE      -> visible to that student/admin
                                                (NOT teacher, NOT our student)

    Deployments
    -----------
    D1: owner=teacher
    D2: owner=admin
    D3: owner=student                         (own)
    D4: owner=other-student, student is in
        Team that references this deployment  (team membership)
    D5: owner=other-student, student is in
        UserToDeployment mapping              (direct mapping)
    """
    # Extra owner used for "data that does NOT belong to the test user".
    other_student = User(
        userId=uuid.uuid4(),
        keycloak_id="other-student-id",
        email="other-student@dhbw.de",
        username="otherstudent",
        firstName="Other",
        lastName="Student",
        role=UserRole.STUDENT,
    )
    db.add(other_student)

    # A couple of courses — courses counter is global for every role.
    db.add(Course(courseId=uuid.uuid4(), name="Course A"))
    db.add(Course(courseId=uuid.uuid4(), name="Course B"))
    db.commit()

    apps = {
        "A1": App(
            appId=uuid.uuid4(),
            name="A1-teacher-public-approved",
            userId=teacher.userId,
            git_link="https://example.com/a1.git",
            is_private=False,
        ),
        "A2": App(
            appId=uuid.uuid4(),
            name="A2-teacher-public-pending",
            userId=teacher.userId,
            git_link="https://example.com/a2.git",
            is_private=False,
        ),
        "A3": App(
            appId=uuid.uuid4(),
            name="A3-teacher-private",
            userId=teacher.userId,
            git_link="https://example.com/a3.git",
            is_private=True,
        ),
        "A4": App(
            appId=uuid.uuid4(),
            name="A4-admin-public-approved",
            userId=admin.userId,
            git_link="https://example.com/a4.git",
            is_private=False,
        ),
        "A5": App(
            appId=uuid.uuid4(),
            name="A5-otherstudent-private",
            userId=other_student.userId,
            git_link="https://example.com/a5.git",
            is_private=True,
        ),
    }
    for a in apps.values():
        db.add(a)
    db.commit()

    # Approvals — only A1 and A4 ever reach APPROVED; A2 stays PENDING.
    db.add(
        AppVersionApproval(
            approvalId=uuid.uuid4(),
            appId=apps["A1"].appId,
            version_tag="v1",
            status=AppVersionApprovalStatus.APPROVED,
            created_at=datetime.utcnow(),
        )
    )
    db.add(
        AppVersionApproval(
            approvalId=uuid.uuid4(),
            appId=apps["A2"].appId,
            version_tag="v1",
            status=AppVersionApprovalStatus.PENDING,
            created_at=datetime.utcnow(),
        )
    )
    db.add(
        AppVersionApproval(
            approvalId=uuid.uuid4(),
            appId=apps["A4"].appId,
            version_tag="v1",
            status=AppVersionApprovalStatus.APPROVED,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    deployments = {
        "D1": Deployment(
            deploymentId=uuid.uuid4(),
            name="D1-owned-by-teacher",
            userId=teacher.userId,
            appId=apps["A1"].appId,
        ),
        "D2": Deployment(
            deploymentId=uuid.uuid4(),
            name="D2-owned-by-admin",
            userId=admin.userId,
            appId=apps["A4"].appId,
        ),
        "D3": Deployment(
            deploymentId=uuid.uuid4(),
            name="D3-owned-by-student",
            userId=student.userId,
            appId=apps["A1"].appId,
        ),
        "D4": Deployment(
            deploymentId=uuid.uuid4(),
            name="D4-otherstudent-team-shared",
            userId=other_student.userId,
            appId=apps["A1"].appId,
        ),
        "D5": Deployment(
            deploymentId=uuid.uuid4(),
            name="D5-otherstudent-direct-shared",
            userId=other_student.userId,
            appId=apps["A1"].appId,
        ),
    }
    for d in deployments.values():
        db.add(d)
    db.commit()

    # Student gets D4 via Team membership.
    team = Team(
        teamId=uuid.uuid4(),
        name="Team-D4",
        deploymentId=deployments["D4"].deploymentId,
    )
    db.add(team)
    db.commit()
    db.add(UserToTeam(userToTeamId=uuid.uuid4(), userId=student.userId, teamId=team.teamId))

    # Student gets D5 via direct UserToDeployment mapping.
    db.add(
        UserToDeployment(
            userToDeploymentId=uuid.uuid4(),
            userId=student.userId,
            deploymentId=deployments["D5"].deploymentId,
        )
    )
    db.commit()

    return {"apps": apps, "deployments": deployments, "other_student": other_student}


def _client_for(user):
    """Return a TestClient whose auth dependency yields ``user``.

    Caller is responsible for clearing ``dependency_overrides`` after
    the test — handled via try/finally in each test below.
    """
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(fastapi_app)


# ---------------------------------------------------------------------------
# 1) Admin: global counters
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_dashboard_stats_admin_counts_global(db, mock_admin, mock_user, mock_student):
    """Admin gets the platform-wide app view (5 apps, deleted excluded),
    plus the deployments they own (1: D2) and the global course count (2)."""
    _seed_world(db, teacher=mock_user, admin=mock_admin, student=mock_student)

    try:
        client = _client_for(mock_admin)
        response = client.get("/dashboard/stats")
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    # 5 apps total (A1..A5), none soft-deleted.
    assert body["apps"] == 5
    # Admin only counts deployments they own — D2.
    assert body["deployments"] == 1
    assert body["courses"] == 2


# ---------------------------------------------------------------------------
# 2) Teacher apps: Bug #6 regression — no more blanket staff view
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_dashboard_stats_teacher_apps_filtered_own_plus_public_approved(
    db, mock_user, mock_admin, mock_student
):
    """Teacher must see: own apps (A1, A2, A3 — regardless of approval
    state or visibility) + public+approved foreign apps (A4). A5 is
    private and owned by someone else — must stay invisible. Result:
    4 apps. If this regresses to 5 the Bug #6 fix has been undone."""
    _seed_world(db, teacher=mock_user, admin=mock_admin, student=mock_student)

    try:
        client = _client_for(mock_user)
        response = client.get("/dashboard/stats")
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    # A1 (own, public, approved), A2 (own, public, pending — own override),
    # A3 (own, private — own override), A4 (foreign, public, approved).
    # NOT A5 (foreign, private).
    assert body["apps"] == 4, (
        "teacher dashboard apps counter must mirror /apps visibility: own + "
        "public-approved, NOT the staff-blanket view (Bug #6)."
    )


# ---------------------------------------------------------------------------
# 3) Teacher deployments: only own (no team/direct membership leak)
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_dashboard_stats_teacher_deployments_count_only_owned(
    db, mock_user, mock_admin, mock_student
):
    """Teacher deployments counter mirrors ``GET /deployments`` — owner
    only, no team/direct-mapping membership. Teacher owns D1 -> 1."""
    _seed_world(db, teacher=mock_user, admin=mock_admin, student=mock_student)

    try:
        client = _client_for(mock_user)
        response = client.get("/dashboard/stats")
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["deployments"] == 1


# ---------------------------------------------------------------------------
# 4) Student: counts only own apps + public-approved, and own/team/direct
#    deployments
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_dashboard_stats_student_counts_only_own_and_team(
    db, mock_user, mock_admin, mock_student
):
    """Student visibility (mirrors crud_apps.get_visible_apps and
    crud_deployments.get_deployments(member_user_id=...)):

      * apps: own (none) + public-approved (A1, A4) = 2
      * deployments: own (D3) + team (D4) + direct (D5) = 3
    """
    _seed_world(db, teacher=mock_user, admin=mock_admin, student=mock_student)

    try:
        client = _client_for(mock_student)
        response = client.get("/dashboard/stats")
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["apps"] == 2
    assert body["deployments"] == 3
    assert body["courses"] == 2


# ---------------------------------------------------------------------------
# 5) Student does not leak data they don't have a relationship to
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_dashboard_stats_student_does_not_leak_other_data(
    db, mock_user, mock_admin, mock_student
):
    """Negative-space sanity check: even though five deployments and
    five apps exist, the student must not see the admin-style totals.
    Strictly less than the platform total for both counters, and the
    course-scope counter must be 0 (students cannot be course-teachers)."""
    _seed_world(db, teacher=mock_user, admin=mock_admin, student=mock_student)

    try:
        client = _client_for(mock_student)
        response = client.get("/dashboard/stats")
    finally:
        fastapi_app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()

    # Student must NOT see admin-style totals.
    assert body["apps"] < 5, "student must not see private foreign apps"
    assert body["deployments"] < 5, (
        "student must only see own/team/direct deployments — no global count"
    )
    # Phase 3 — course-teacher scope is irrelevant for students.
    assert body["courseScopeDeployments"] == 0


# ---------------------------------------------------------------------------
# 6) Unauthenticated -> 401
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_dashboard_stats_unauthenticated_401(unauth_client):
    """No auth override -> Keycloak dependency rejects the call. The
    project uses 401/403 interchangeably for missing auth (see
    test_apps_unauthenticated), so accept either."""
    response = unauth_client.get("/dashboard/stats")
    assert response.status_code in (401, 403)
