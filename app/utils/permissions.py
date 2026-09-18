"""
Permission and authorization utilities for role-based access control.

Provides a ``require_roles()`` FastAPI-dependency factory and the
``ADMIN_ROLES`` / ``STAFF_ROLES`` groupings. Fine-grained,
resource-level decisions live in :mod:`app.utils.capabilities`.
"""
from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    Deployment,
    Team,
    User,
    UserRole,
    UserToDeployment,
    UserToTeam,
)
from app.utils.auth import get_current_user

# ----------------------------------------------------------------
# ROLE GROUPINGS
# ----------------------------------------------------------------
# ``STAFF_ROLES`` covers everyone with elevated privileges (the users
# for whom the UI shows staff-only chrome). It does NOT mean "anyone
# with course-teacher rights" — that is a per-resource check handled
# in :mod:`app.utils.capabilities`.
STAFF_ROLES: tuple[UserRole, ...] = (UserRole.TEACHER, UserRole.ADMIN)
ADMIN_ROLES: tuple[UserRole, ...] = (UserRole.ADMIN,)


# ----------------------------------------------------------------
# ROLE DEPENDENCY FACTORY
# ----------------------------------------------------------------
def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """Get current active user"""
    return current_user


def require_roles(*roles: UserRole) -> Callable[..., User]:
    """FastAPI dependency factory enforcing a role allow-list.

    Returns a dependency that resolves the current user and raises 403
    with a structured ``detail`` payload when the user's role is not in
    ``roles``. The payload shape is::

        {"code": "role_required", "required": ["admin", ...]}

    so the frontend can render a precise "you need role X" message and
    distinguish role-based 403s from resource-based 403s.
    """
    if not roles:
        raise ValueError("require_roles() needs at least one role")

    allowed = tuple(roles)

    def _dep(user: User = Depends(get_current_active_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "role_required",
                    "required": [r.value for r in allowed],
                },
            )
        return user

    return _dep


# Canonical aliases for requiring a role on router dependencies.
require_admin = require_roles(UserRole.ADMIN)
require_staff = require_roles(UserRole.TEACHER, UserRole.ADMIN)


# ----------------------------------------------------------------
# DEPLOYMENT ACCESS (Owner / Team-Member / Teacher / Admin)
# ----------------------------------------------------------------
def has_deployment_access(deployment: Deployment, user: User, db: Session) -> bool:
    """
    Return True if `user` may read/manage `deployment`.

    Allowed when any of:
      - user is teacher or admin
      - user is the deployment owner
      - user is part of any team assigned to this deployment
        (via UserToTeam joined to Team.deploymentId)
      - user appears in UserToDeployment for this deployment
    """
    if user.role in STAFF_ROLES:
        return True
    if str(deployment.userId) == str(user.userId):
        return True

    team_match = (
        db.query(UserToTeam.userToTeamId)
        .join(Team, Team.teamId == UserToTeam.teamId)
        .filter(
            Team.deploymentId == deployment.deploymentId,
            UserToTeam.userId == user.userId,
        )
        .first()
    )
    if team_match:
        return True

    direct_match = (
        db.query(UserToDeployment.userToDeploymentId)
        .filter(
            UserToDeployment.deploymentId == deployment.deploymentId,
            UserToDeployment.userId == user.userId,
        )
        .first()
    )
    return direct_match is not None


def ensure_deployment_access(deployment: Deployment, user: User, db: Session) -> None:
    """
    Raise 403 unless `user` may access `deployment`.

    Use this in every endpoint that takes a deployment_id from the URL/body
    to prevent IDOR. Pass the loaded Deployment, not just the ID — callers
    should already have fetched it (and should return 404 if missing before
    calling this).
    """
    if not has_deployment_access(deployment, user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this deployment",
        )



def is_deployment_owner_view(deployment: Deployment, user: User) -> bool:
    """True if ``user`` should see the *owner view* of ``deployment``.

    The owner view shows everything (tasks, logs, terraform state, full
    team rosters, destroy/delete); the member view shows only deployment
    metadata, the user's own team, and resend-credentials for themself.
    Teachers, admins, and the deployment creator get the owner view.
    """
    if user.role in STAFF_ROLES:
        return True
    return str(deployment.userId) == str(user.userId)


def ensure_deployment_owner_view(deployment: Deployment, user: User) -> None:
    """Raise 403 unless ``user`` has the owner view of ``deployment``.

    Use on endpoints that expose deployment-internals (tasks, logs,
    state, destroy/delete) — members have read-access to the
    deployment itself but not to those.
    """
    if not is_deployment_owner_view(deployment, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the deployment owner or staff can perform this action",
        )
