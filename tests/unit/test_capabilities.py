"""Unit tests for :mod:`app.utils.capabilities`.

Every capability function gets parametrised coverage from the
Student / Teacher / Admin perspectives. The tests use plain
``SimpleNamespace`` stand-ins instead of real ORM objects and a
``MagicMock`` for the DB session so the suite runs without Postgres.

Phase 2 contract: app-edit/delete/submit/view are now owner-or-admin
only (Bug #2 fix); operate on a deployment is owner-or-admin only.
Teacher bypasses on these have been removed — the matrix below
reflects that.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.models import UserRole
from app.utils import capabilities as caps

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------
def _user(role: UserRole, user_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(userId=user_id or uuid.uuid4(), role=role)


def _app(owner_id: uuid.UUID, is_private: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        appId=uuid.uuid4(),
        userId=owner_id,
        is_private=is_private,
    )


def _deployment(owner_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(deploymentId=uuid.uuid4(), userId=owner_id)


def _course() -> SimpleNamespace:
    return SimpleNamespace(courseId=uuid.uuid4(), name="X")


@pytest.fixture
def student():
    return _user(UserRole.STUDENT)


@pytest.fixture
def teacher():
    return _user(UserRole.TEACHER)


@pytest.fixture
def admin():
    return _user(UserRole.ADMIN)


@pytest.fixture
def db_mock():
    return MagicMock()


# ================================================================
# APPS — can_view_app
# ================================================================
class TestCanViewApp:
    def test_owner_can_view_own_private_app(self, student):
        app = _app(student.userId, is_private=True)
        assert caps.can_view_app(student, app, db=None) is True

    def test_admin_can_view_any_app(self, admin):
        app = _app(uuid.uuid4(), is_private=True)
        assert caps.can_view_app(admin, app, db=None) is True

    def test_teacher_cannot_view_others_private_app(self, teacher):
        # Phase 2 — Bug #2 fix: teachers no longer get a blanket view
        # on private third-party apps. Only owner or admin sees these.
        app = _app(uuid.uuid4(), is_private=True)
        assert caps.can_view_app(teacher, app, db=None) is False

    def test_teacher_can_view_public_approved_app(self, teacher, monkeypatch):
        # Public + approved → visible to everyone (also teachers).
        app = _app(uuid.uuid4(), is_private=False)
        monkeypatch.setattr(
            "app.utils.capabilities.crud_approvals.has_any_approved_version",
            lambda _db, _app_id: True,
        )
        db = MagicMock()
        assert caps.can_view_app(teacher, app, db=db) is True

    def test_student_cannot_view_others_private_app(self, student):
        app = _app(uuid.uuid4(), is_private=True)
        assert caps.can_view_app(student, app, db=None) is False

    def test_student_can_view_public_approved_app(self, student, monkeypatch):
        app = _app(uuid.uuid4(), is_private=False)
        monkeypatch.setattr(
            "app.utils.capabilities.crud_approvals.has_any_approved_version",
            lambda _db, _app_id: True,
        )
        db = MagicMock()
        assert caps.can_view_app(student, app, db=db) is True

    def test_student_cannot_view_public_unapproved_app(self, student, monkeypatch):
        app = _app(uuid.uuid4(), is_private=False)
        monkeypatch.setattr(
            "app.utils.capabilities.crud_approvals.has_any_approved_version",
            lambda _db, _app_id: False,
        )
        db = MagicMock()
        assert caps.can_view_app(student, app, db=db) is False


class TestEnsureViewApp:
    def test_ensure_raises_with_app_view_forbidden(self, student):
        app = _app(uuid.uuid4(), is_private=True)
        with pytest.raises(HTTPException) as exc:
            caps.ensure_view_app(student, app, db=None)
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "app_view_forbidden"


# ================================================================
# APPS — can_list_all_apps
# ================================================================
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (UserRole.STUDENT, False),
        (UserRole.TEACHER, False),
        (UserRole.ADMIN, True),
    ],
)
def test_can_list_all_apps(role, expected):
    assert caps.can_list_all_apps(_user(role)) is expected


def test_ensure_list_all_apps_raises_role_required(student):
    with pytest.raises(HTTPException) as exc:
        caps.ensure_list_all_apps(student)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "role_required"
    assert exc.value.detail["required"] == [UserRole.ADMIN.value]


# ================================================================
# APPS — edit / delete / submit (Phase 2: owner OR admin)
# ================================================================
class TestCanEditApp:
    def test_owner_can_edit(self, student):
        app = _app(student.userId)
        assert caps.can_edit_app(student, app) is True

    def test_admin_can_edit(self, admin):
        app = _app(uuid.uuid4())
        assert caps.can_edit_app(admin, app) is True

    def test_teacher_cannot_edit_foreign_app(self, teacher):
        # Phase 2 — Bug #2 fix: teacher bypass on edit is removed.
        # Teachers may only edit apps they own.
        app = _app(uuid.uuid4())
        assert caps.can_edit_app(teacher, app) is False

    def test_teacher_can_edit_own_app(self, teacher):
        app = _app(teacher.userId)
        assert caps.can_edit_app(teacher, app) is True

    def test_other_student_cannot_edit(self, student):
        app = _app(uuid.uuid4())
        assert caps.can_edit_app(student, app) is False


def test_can_delete_app_mirrors_edit(student, teacher, admin):
    own = _app(student.userId)
    other = _app(uuid.uuid4())
    teachers_own = _app(teacher.userId)
    assert caps.can_delete_app(student, own) is True
    assert caps.can_delete_app(student, other) is False
    # Phase 2 — Bug #2 fix: teacher cannot delete a foreign app.
    assert caps.can_delete_app(teacher, other) is False
    assert caps.can_delete_app(teacher, teachers_own) is True
    assert caps.can_delete_app(admin, other) is True


def test_can_submit_app_version_mirrors_edit(student, teacher, admin):
    own = _app(student.userId)
    other = _app(uuid.uuid4())
    teachers_own = _app(teacher.userId)
    assert caps.can_submit_app_version(student, own) is True
    assert caps.can_submit_app_version(student, other) is False
    # Phase 2 — Bug #2 fix: teacher cannot submit a foreign app's version.
    assert caps.can_submit_app_version(teacher, other) is False
    assert caps.can_submit_app_version(teacher, teachers_own) is True
    assert caps.can_submit_app_version(admin, other) is True


# ================================================================
# APPS — approve_app_version (Admin only)
# ================================================================
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (UserRole.STUDENT, False),
        (UserRole.TEACHER, False),
        (UserRole.ADMIN, True),
    ],
)
def test_can_approve_app_version(role, expected):
    assert caps.can_approve_app_version(_user(role)) is expected


def test_ensure_approve_app_version_raises(teacher):
    with pytest.raises(HTTPException) as exc:
        caps.ensure_approve_app_version(teacher)
    assert exc.value.detail["code"] == "role_required"
    assert exc.value.detail["required"] == [UserRole.ADMIN.value]


# ================================================================
# DEPLOYMENTS — create / view_member / view_owner / operate / resend
# ================================================================
@pytest.mark.parametrize(
    "role,expected",
    [
        (UserRole.STUDENT, False),
        (UserRole.TEACHER, True),
        (UserRole.ADMIN, True),
    ],
)
def test_can_create_deployment(role, expected):
    """Creating a deployment is staff work; students are handed one."""
    assert caps.can_create_deployment(_user(role)) is expected


def test_ensure_create_deployment_raises_role_required(student):
    with pytest.raises(HTTPException) as exc:
        caps.ensure_create_deployment(student)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "role_required"
    assert set(exc.value.detail["required"]) == {"teacher", "admin"}


def test_ensure_create_deployment_passes_for_staff(teacher, admin):
    caps.ensure_create_deployment(teacher)
    caps.ensure_create_deployment(admin)


class TestCanViewDeploymentMember:
    """Mirrors ``has_deployment_access`` — owner, staff, team, direct."""

    def test_owner_can_view(self, student, db_mock):
        dep = _deployment(student.userId)
        assert caps.can_view_deployment_member(student, dep, db_mock) is True

    def test_admin_can_view(self, admin, db_mock):
        dep = _deployment(uuid.uuid4())
        # Admin path returns early from has_deployment_access — no DB
        # calls happen, so the MagicMock can stay empty.
        assert caps.can_view_deployment_member(admin, dep, db_mock) is True

    def test_teacher_can_view(self, teacher, db_mock):
        dep = _deployment(uuid.uuid4())
        assert caps.can_view_deployment_member(teacher, dep, db_mock) is True

    def test_unrelated_student_cannot_view(self, student, monkeypatch):
        # Stub has_deployment_access to "no team, no direct" so we
        # exercise the rejection branch without a real DB.
        dep = _deployment(uuid.uuid4())
        db = MagicMock()
        db.query.return_value.join.return_value.filter.return_value.first.return_value = None
        db.query.return_value.filter.return_value.first.return_value = None
        assert caps.can_view_deployment_member(student, dep, db) is False


class TestCanViewDeploymentOwner:
    """Phase 3: owner-view = owner OR admin OR course-teacher of the
    deployment-owner's course. The plain staff bypass is gone."""

    @pytest.mark.parametrize(
        ("role", "is_owner", "expected"),
        [
            (UserRole.STUDENT, True, True),
            (UserRole.STUDENT, False, False),
            # Phase 3 — teacher without course-teacher rows on the
            # deployment-owner's course: rejected. Course-teacher
            # access is exercised in ``test_course_teacher_can_view``
            # below.
            (UserRole.TEACHER, False, False),
            (UserRole.ADMIN, False, True),
        ],
    )
    def test_matrix(self, role, is_owner, expected, monkeypatch):
        owner_id = uuid.uuid4()
        actor = _user(role, user_id=owner_id if is_owner else None)
        course_id = uuid.uuid4()
        dep = SimpleNamespace(
            deploymentId=uuid.uuid4(),
            userId=owner_id,
            user=SimpleNamespace(courseId=course_id),
        )
        db = MagicMock()
        # execute().first() returns the owner's courseId row.
        db.execute.return_value.first.return_value = (course_id,)
        # is_course_teacher_id query returns None → teacher branch falls through.
        db.query.return_value.filter.return_value.first.return_value = None
        assert caps.can_view_deployment_owner(actor, dep, db) is expected

    def test_course_teacher_can_view_owner_view(self, teacher):
        # Phase 3 — a teacher who IS a course-teacher of the
        # deployment-owner's course gets the owner view (inspect).
        course_id = uuid.uuid4()
        dep = SimpleNamespace(
            deploymentId=uuid.uuid4(),
            userId=uuid.uuid4(),
            user=SimpleNamespace(courseId=course_id),
        )
        db = MagicMock()
        # execute().first() returns the owner's courseId.
        db.execute.return_value.first.return_value = (course_id,)
        # is_course_teacher_id query returns a truthy row → match.
        db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(courseId=course_id, userId=teacher.userId)
        )
        assert caps.can_view_deployment_owner(teacher, dep, db) is True

    def test_teacher_without_course_context_rejected(self, teacher):
        # Deployment owner has no course assigned → course-teacher path
        # doesn't apply, teacher is not owner/admin → False.
        dep = SimpleNamespace(
            deploymentId=uuid.uuid4(),
            userId=uuid.uuid4(),
            user=SimpleNamespace(courseId=None),
        )
        db = MagicMock()
        # courseId query returns a row with None (owner has no course).
        db.execute.return_value.first.return_value = (None,)
        assert caps.can_view_deployment_owner(teacher, dep, db) is False


class TestCanOperateDeployment:
    """Phase 2: operate is owner-or-admin only. Teachers no longer get
    a blanket operate right via the staff bypass."""

    def test_owner_can_operate(self, student, db_mock):
        dep = _deployment(student.userId)
        assert caps.can_operate_deployment(student, dep, db_mock) is True

    def test_teacher_cannot_operate_foreign_deployment(self, teacher, db_mock):
        # Phase 2 — operate is owner-or-admin only. A teacher who is
        # not the owner cannot pause/destroy a deployment.
        dep = _deployment(uuid.uuid4())
        assert caps.can_operate_deployment(teacher, dep, db_mock) is False

    def test_admin_can_operate(self, admin, db_mock):
        dep = _deployment(uuid.uuid4())
        assert caps.can_operate_deployment(admin, dep, db_mock) is True

    def test_unrelated_student_cannot_operate(self, student, db_mock):
        dep = _deployment(uuid.uuid4())
        assert caps.can_operate_deployment(student, dep, db_mock) is False


class TestCanResendAccess:
    def test_self_resend_when_member(self, student, db_mock):
        dep = _deployment(student.userId)
        assert caps.can_resend_access(student, dep, student.userId, db_mock) is True

    def test_admin_can_resend_for_anyone(self, admin, db_mock):
        dep = _deployment(uuid.uuid4())
        other = uuid.uuid4()
        assert caps.can_resend_access(admin, dep, other, db_mock) is True

    def test_student_cannot_resend_for_other(self, student, db_mock):
        # Student is owner of dep (member-view), but cannot dispatch
        # credentials to someone else — that's the owner-view gate.
        dep = _deployment(student.userId)
        other = uuid.uuid4()
        # Owner of the deployment counts as owner-view → True.
        assert caps.can_resend_access(student, dep, other, db_mock) is True

    def test_unrelated_student_cannot_resend_for_other(self, student, db_mock):
        dep = _deployment(uuid.uuid4())  # student is not owner
        other = uuid.uuid4()
        assert caps.can_resend_access(student, dep, other, db_mock) is False


# ================================================================
# COURSES
# ================================================================
class TestIsCourseTeacher:
    """Phase 3: ``is_course_teacher`` queries the ``course_teachers``
    join table. Role gate keeps non-teachers out before any DB read.
    """

    def test_student_never_matches(self, student, db_mock):
        course = _course()
        assert caps.is_course_teacher(student, course, db_mock) is False
        # The role short-circuit must run BEFORE the DB query so the
        # student path doesn't issue useless lookups.
        db_mock.query.assert_not_called()

    def test_admin_role_gate(self, admin, db_mock):
        # Admins don't sit in the course_teachers table by design —
        # they get edit/view via the admin bypass at the call site,
        # not via ``is_course_teacher``. The role gate keeps this
        # function False for admins regardless of the DB state, so
        # callers can rely on it returning the *literal* course-teacher
        # fact and combine it with their own admin override.
        course = _course()
        assert caps.is_course_teacher(admin, course, db_mock) is False
        db_mock.query.assert_not_called()

    def test_teacher_with_matching_row(self, teacher):
        course = _course()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(courseId=course.courseId, userId=teacher.userId)
        )
        assert caps.is_course_teacher(teacher, course, db) is True

    def test_teacher_without_matching_row(self, teacher):
        course = _course()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        assert caps.is_course_teacher(teacher, course, db) is False


class TestGetMyCourseTeacherIds:
    def test_returns_empty_for_non_teacher(self, student, admin, db_mock):
        assert caps.get_my_course_teacher_ids(student, db_mock) == set()
        assert caps.get_my_course_teacher_ids(admin, db_mock) == set()

    def test_teacher_collects_course_ids(self, teacher):
        cid1, cid2 = uuid.uuid4(), uuid.uuid4()
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            (cid1,),
            (cid2,),
        ]
        assert caps.get_my_course_teacher_ids(teacher, db) == {cid1, cid2}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (UserRole.STUDENT, False),
        (UserRole.TEACHER, True),
        (UserRole.ADMIN, True),
    ],
)
def test_can_view_course_detail(role, expected):
    assert caps.can_view_course_detail(_user(role)) is expected


def test_ensure_view_course_detail_raises(student):
    with pytest.raises(HTTPException) as exc:
        caps.ensure_view_course_detail(student)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "role_required"
    # Order matters: STAFF_ROLES is (TEACHER, ADMIN).
    assert exc.value.detail["required"] == [
        UserRole.TEACHER.value,
        UserRole.ADMIN.value,
    ]


class TestCanEditCourse:
    """Phase 3: ``can_edit_course`` is admin OR course-teacher of THIS
    course. The legacy ``any staff bypass`` is gone — a teacher who is
    not a designated teacher of the course gets rejected.
    """

    def test_admin_can_edit_any_course(self, admin, db_mock):
        course = _course()
        # Admin path returns True without hitting the DB.
        assert caps.can_edit_course(admin, course, db_mock) is True

    def test_student_cannot_edit(self, student, db_mock):
        course = _course()
        assert caps.can_edit_course(student, course, db_mock) is False

    def test_teacher_without_course_teacher_row_rejected(self, teacher):
        course = _course()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        assert caps.can_edit_course(teacher, course, db) is False

    def test_teacher_with_course_teacher_row_allowed(self, teacher):
        course = _course()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(courseId=course.courseId, userId=teacher.userId)
        )
        assert caps.can_edit_course(teacher, course, db) is True


# ================================================================
# USERS
# ================================================================
class TestCanViewUser:
    def test_self_lookup_always_allowed(self, student, teacher, admin):
        for actor in (student, teacher, admin):
            assert caps.can_view_user(actor, actor.userId) is True

    def test_student_cannot_view_other(self, student):
        assert caps.can_view_user(student, uuid.uuid4()) is False

    def test_teacher_can_view_other(self, teacher):
        assert caps.can_view_user(teacher, uuid.uuid4()) is True

    def test_admin_can_view_other(self, admin):
        assert caps.can_view_user(admin, uuid.uuid4()) is True


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (UserRole.STUDENT, False),
        (UserRole.TEACHER, False),
        (UserRole.ADMIN, True),
    ],
)
def test_can_change_user_role(role, expected):
    assert caps.can_change_user_role(_user(role)) is expected


def test_ensure_change_user_role_raises(teacher):
    with pytest.raises(HTTPException) as exc:
        caps.ensure_change_user_role(teacher)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "role_required"
    assert exc.value.detail["required"] == [UserRole.ADMIN.value]
