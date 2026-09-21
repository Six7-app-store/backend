"""Capability functions for role-based access control.

This module is the single source of truth for "may this user do X?"
questions. Every router endpoint that goes beyond a plain role check
should reach for a ``can_*`` (boolean) or ``ensure_*`` (raises) helper
here instead of poking at ``user.role`` directly.

Conventions:
    - ``can_<verb>_<resource>(user, ..., *, db=None) -> bool`` answers
      the permission question and never raises.
    - ``ensure_<verb>_<resource>(...)`` calls the ``can_*`` form and
      raises :class:`fastapi.HTTPException` with status 403 and a
      structured ``detail`` payload::

          {"code": "<machine_code>", "required": [...optional context...]}

      The ``code`` lets the frontend render specific error messages.
      For role-shaped rejections, ``code="role_required"`` matches the
      payload produced by :func:`app.utils.permissions.require_roles`.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.models import App, Course, CourseTeacher, Deployment, User, UserRole
from app.utils.permissions import (
    STAFF_ROLES,
    has_deployment_access,
)


# ----------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------
def _is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def _is_staff(user: User) -> bool:
    """User has a staff role (Teacher or Admin).

    This is the role-shaped check; for per-resource course-teacher
    rights use :func:`is_course_teacher`.
    """
    return user.role in STAFF_ROLES


def _is_owner(user: User, owner_id) -> bool:
    return str(owner_id) == str(user.userId)


def _forbidden(code: str, required: list[str] | None = None) -> HTTPException:
    detail: dict = {"code": code}
    if required is not None:
        detail["required"] = required
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


# ================================================================
# APPS
# ================================================================
def can_view_app(user: User, app: App, *, db: Session | None = None) -> bool:
    """Whether ``user`` may see ``app`` at all.

    Allowed when:
      - user owns the app, OR
      - user is admin, OR
      - app is public AND has at least one approved version.
    """
    if _is_owner(user, app.userId):
        return True
    if _is_admin(user):
        return True
    if app.is_private:
        return False
    if db is None:
        # Without a DB handle we can't verify the approved-version
        # requirement; the safe default is "no".
        return False
    return crud_approvals.has_any_approved_version(db, app.appId)


def ensure_view_app(user: User, app: App, *, db: Session | None = None) -> None:
    if not can_view_app(user, app, db=db):
        raise _forbidden("app_view_forbidden")


def can_list_all_apps(user: User) -> bool:
    """Whether ``user`` may list every app (including private + unapproved).

    Admin only. Other staff members see "my apps + public approved
    apps" — that is a query-shape concern, not a flat boolean.
    """
    return _is_admin(user)


def ensure_list_all_apps(user: User) -> None:
    if not can_list_all_apps(user):
        raise _forbidden("role_required", [UserRole.ADMIN.value])


def can_edit_app(user: User, app: App) -> bool:
    """Whether ``user`` may edit ``app``'s metadata. Owner or admin only."""
    return _is_admin(user) or _is_owner(user, app.userId)


def ensure_edit_app(user: User, app: App) -> None:
    if not can_edit_app(user, app):
        raise _forbidden("app_edit_forbidden")


def can_delete_app(user: User, app: App) -> bool:
    """Whether ``user`` may (soft-)delete ``app``. Owner or admin only."""
    return _is_admin(user) or _is_owner(user, app.userId)


def ensure_delete_app(user: User, app: App) -> None:
    if not can_delete_app(user, app):
        raise _forbidden("app_delete_forbidden")


def can_submit_app_version(user: User, app: App) -> bool:
    """Submit a version for approval review. Owner or admin only."""
    return _is_admin(user) or _is_owner(user, app.userId)


def ensure_submit_app_version(user: User, app: App) -> None:
    if not can_submit_app_version(user, app):
        raise _forbidden("app_submit_forbidden")


def can_approve_app_version(user: User) -> bool:
    """Approve / reject / revoke a submitted version. Admin only."""
    return _is_admin(user)


def ensure_approve_app_version(user: User) -> None:
    if not can_approve_app_version(user):
        raise _forbidden("role_required", [UserRole.ADMIN.value])


# ================================================================
# DEPLOYMENTS
# ================================================================
def can_create_deployment(user: User) -> bool:
    """Whether ``user`` may create a deployment at all.

    Staff only. Students receive access to an environment a teacher set
    up for them; they never create one themselves. This is the coarse
    role gate — the per-app visibility rule (private app, unapproved
    version) still applies on top via :func:`ensure_view_app`.
    """
    return _is_staff(user)


def ensure_create_deployment(user: User) -> None:
    if not can_create_deployment(user):
        raise _forbidden("role_required", [r.value for r in STAFF_ROLES])


def can_view_deployment_member(user: User, dep: Deployment, db: Session) -> bool:
    """Member-view access to a deployment.

    Mirrors :func:`app.utils.permissions.has_deployment_access` —
    owner, staff, team-member, or direct UserToDeployment mapping.
    """
    return has_deployment_access(dep, user, db)


def ensure_view_deployment_member(user: User, dep: Deployment, db: Session) -> None:
    if not can_view_deployment_member(user, dep, db):
        raise _forbidden("deployment_view_forbidden")


def can_view_deployment_owner(user: User, dep: Deployment, db: Session) -> bool:
    """Owner-view access — tasks, logs, terraform state, destroy.

    Read access is granted to the deployment owner, admins, and
    course-teachers of the deployment owner's course (inspect only).
    Operate rights are separate — see :func:`can_operate_deployment`.
    The course-teacher check is skipped when the owner has no course.
    """
    if user.role == UserRole.ADMIN:
        return True
    if str(dep.userId) == str(user.userId):
        return True

    # Course-teacher inspect right applies only to teachers; other
    # roles are rejected by the role gate in ``is_course_teacher_id``.
    if user.role != UserRole.TEACHER:
        return False
    # Query courseId directly to avoid relying on a lazily-loaded
    # relation — the deployment may have been loaded without the user
    # relation preloaded, which would cause a DetachedInstanceError
    # or a silent None outside the session context.
    from sqlalchemy import select

    row = db.execute(
        select(User.courseId).where(User.userId == dep.userId)
    ).first()
    owner_course_id = row[0] if row else None
    if owner_course_id is None:
        return False
    return is_course_teacher_id(user, owner_course_id, db)


def ensure_view_deployment_owner(user: User, dep: Deployment, db: Session) -> None:
    if not can_view_deployment_owner(user, dep, db):
        raise _forbidden("deployment_owner_view_forbidden")


def can_operate_deployment(user: User, dep: Deployment, db: Session) -> bool:
    """Pause / Resume / Destroy / Redeploy on a deployment. Owner or admin only."""
    del db
    return _is_admin(user) or _is_owner(user, dep.userId)


def ensure_operate_deployment(user: User, dep: Deployment, db: Session) -> None:
    if not can_operate_deployment(user, dep, db):
        raise _forbidden("deployment_operate_forbidden")


def can_resend_access(
    user: User,
    dep: Deployment,
    target_user_id: UUID | str,
    db: Session,
) -> bool:
    """Resend access credentials for ``target_user_id`` on ``dep``.

    A user may resend credentials to themself on any deployment they
    can member-view; staff may resend credentials to anyone on a
    deployment they can owner-view.
    """
    if str(target_user_id) == str(user.userId):
        return can_view_deployment_member(user, dep, db)
    return can_view_deployment_owner(user, dep, db)


def ensure_resend_access(
    user: User,
    dep: Deployment,
    target_user_id: UUID | str,
    db: Session,
) -> None:
    if not can_resend_access(user, dep, target_user_id, db):
        raise _forbidden("deployment_resend_forbidden")


# ================================================================
# COURSES
# ================================================================
def is_course_teacher(user: User, course: Course, db: Session) -> bool:
    """Whether ``user`` is a designated teacher of ``course``.

    A user is a course-teacher for ``course`` exactly when their role
    is ``TEACHER`` and a ``(course_id, user_id)`` row exists in
    ``course_teachers``. Students never qualify — the role gate stays
    primary. Admins are handled by the admin bypass at each call site.
    """
    return is_course_teacher_id(user, course.courseId, db)


def is_course_teacher_id(user: User, course_id: UUID, db: Session) -> bool:
    """Variant of :func:`is_course_teacher` when only the course id
    is known. Used by helpers that need to filter rows by course
    without materialising the ``Course`` object.
    """
    if user.role != UserRole.TEACHER:
        return False
    row = (
        db.query(CourseTeacher)
        .filter(
            CourseTeacher.courseId == course_id,
            CourseTeacher.userId == user.userId,
        )
        .first()
    )
    return row is not None


def get_my_course_teacher_ids(user: User, db: Session) -> set[UUID]:
    """Load the set of course IDs ``user`` is a designated teacher of.

    Returns the empty set for non-teacher roles. Intended to be called
    once per request and threaded into list-shaping helpers to avoid an
    ``is_course_teacher`` query per row (N+1); currently the data source
    for the ``?scope=course`` filter on the deployments list.
    """
    if user.role != UserRole.TEACHER:
        return set()
    rows = (
        db.query(CourseTeacher.courseId)
        .filter(CourseTeacher.userId == user.userId)
        .all()
    )
    return {row[0] for row in rows}


def can_view_course_detail(user: User) -> bool:
    """Whether ``user`` may read course details + member rosters.

    Today: staff only. Students see the courses they're enrolled in
    via a different endpoint shape, not this one.
    """
    return _is_staff(user)


def ensure_view_course_detail(user: User) -> None:
    if not can_view_course_detail(user):
        raise _forbidden("role_required", [r.value for r in STAFF_ROLES])


def can_edit_course(user: User, course: Course, db: Session) -> bool:
    """Edit / delete ``course``. Course-teacher of this course or admin."""
    if _is_admin(user):
        return True
    return is_course_teacher(user, course, db)


def ensure_edit_course(user: User, course: Course, db: Session) -> None:
    if not can_edit_course(user, course, db):
        raise _forbidden("course_edit_forbidden")


# ================================================================
# USERS
# ================================================================
def can_view_user(actor: User, target_id: UUID | str) -> bool:
    """Whether ``actor`` may read the profile at ``target_id``.

    Mirrors today: ``/me`` is always allowed (handled separately by
    the router), seeing someone else requires a staff role.
    """
    if str(actor.userId) == str(target_id):
        return True
    return _is_staff(actor)


def ensure_view_user(actor: User, target_id: UUID | str) -> None:
    if not can_view_user(actor, target_id):
        raise _forbidden("user_view_forbidden")


def can_change_user_role(actor: User) -> bool:
    """Whether ``actor`` may change someone else's role. Admin only."""
    return _is_admin(actor)


def ensure_change_user_role(actor: User) -> None:
    if not can_change_user_role(actor):
        raise _forbidden("role_required", [UserRole.ADMIN.value])


__all__ = [
    # Apps
    "can_view_app",
    "ensure_view_app",
    "can_list_all_apps",
    "ensure_list_all_apps",
    "can_edit_app",
    "ensure_edit_app",
    "can_delete_app",
    "ensure_delete_app",
    "can_submit_app_version",
    "ensure_submit_app_version",
    "can_approve_app_version",
    "ensure_approve_app_version",
    # Deployments
    "can_view_deployment_member",
    "ensure_view_deployment_member",
    "can_view_deployment_owner",
    "ensure_view_deployment_owner",
    "can_operate_deployment",
    "ensure_operate_deployment",
    "can_resend_access",
    "ensure_resend_access",
    # Courses
    "is_course_teacher",
    "is_course_teacher_id",
    "get_my_course_teacher_ids",
    "can_view_course_detail",
    "ensure_view_course_detail",
    "can_edit_course",
    "ensure_edit_course",
    # Users
    "can_view_user",
    "ensure_view_user",
    "can_change_user_role",
    "ensure_change_user_role",
]
