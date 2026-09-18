from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import users as crud_users
from app.database import get_db
from app.models import App, Deployment, User, UserRole
from app.schemas import UserResponse, UserStatistics, UserUpdate, UserWithCourse
from app.utils.auth import get_current_user
from app.utils.capabilities import ensure_change_user_role, ensure_view_user
from app.utils.keycloak_auth import (
    get_keycloak_users_by_ids,
    search_keycloak_users,
)
from app.utils.permissions import (
    require_staff,
)

router = APIRouter()

# ----------------------------------------------------------------
# GET CURRENT USER
# ----------------------------------------------------------------
@router.get("/me", response_model=UserWithCourse)
def get_me(current_user: User = Depends(get_current_user)):
    """Get current authenticated user with course information"""
    return current_user

# ----------------------------------------------------------------
# GET ALL USERS (TEACHER/ADMIN ONLY)
# ----------------------------------------------------------------
@router.get("/", response_model=list[UserResponse])
def list_users(
    skip: int = 0,
    limit: int = 100,
    role: UserRole | None = None,
    course_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Get all users with optional filters
    - **Requires**: TEACHER or ADMIN role
    """
    users = crud_users.get_users(db, skip=skip, limit=limit, role=role, course_id=course_id)
    # Enrich users with Keycloak names when keycloak_id is present
    kc_ids = [u.keycloak_id for u in users if getattr(u, 'keycloak_id', None)]
    kc_map = {}
    if kc_ids:
        try:
            kc_map = get_keycloak_users_by_ids(kc_ids)
        except HTTPException:
            # If enrichment fails, continue returning base users
            kc_map = {}

    result = []
    for u in users:
        user_obj = {
            "userId": u.userId,
            "email": u.email,
            "username": u.username,
            "role": u.role,
            "courseId": u.courseId,
            "created_at": u.created_at,
            "keycloak_id": getattr(u, 'keycloak_id', None),
            # default empty strings if not available
            "firstName": None,
            "lastName": None,
        }
        if user_obj["keycloak_id"] and user_obj["keycloak_id"] in kc_map:
            kc = kc_map[user_obj["keycloak_id"]]
            user_obj["firstName"] = kc.get("firstName")
            user_obj["lastName"] = kc.get("lastName")
        result.append(user_obj)

    return result

# ----------------------------------------------------------------
# SEARCH USERS FROM KEYCLOAK
# ----------------------------------------------------------------
@router.get("/search")
def search_users_keycloak(
    query: str,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Search users directly from Keycloak by username, email, or name
    - **Requires**: TEACHER or ADMIN role
    - Returns users from Keycloak (not local DB)

    Response:
    - id: Keycloak user ID
    - username: Username
    - email: Email address
    - firstName: First name
    - lastName: Last name
    - enabled: Account enabled status
    """
    if not query or len(query) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Search query must be at least 2 characters"
        )

    # ``db`` is injected via Depends(get_db) so test dependency overrides
    # apply and the route never writes against the dev DB.
    from app.utils.keycloak_auth import sync_user_from_keycloak

    keycloak_users = search_keycloak_users(query, limit)
    results = []
    for kc_user in keycloak_users:
        # Create/update the user in the local DB
        db_user = sync_user_from_keycloak(db, kc_user)
        results.append({
            "userId": db_user.userId,
            "email": db_user.email,
            "username": db_user.username,
            "role": db_user.role,
            "courseId": db_user.courseId,
            "created_at": db_user.created_at,
            "keycloak_id": db_user.keycloak_id,
            "firstName": kc_user.get("firstName"),
            "lastName": kc_user.get("lastName"),
        })
    return results

# ----------------------------------------------------------------
# GET USER BY ID
# ----------------------------------------------------------------
@router.get("/{user_id}", response_model=UserWithCourse)
def get_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get user by ID
    - **Students**: Can only view their own profile
    - **Teachers/Admins**: Can view any profile
    """
    user = crud_users.get_user(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Check access permission
    if current_user.role == UserRole.STUDENT and user_id != current_user.userId:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own profile"
        )

    return user

# ----------------------------------------------------------------
# GET USER STATISTICS
# ----------------------------------------------------------------
@router.get("/{user_id}/statistics", response_model=UserStatistics)
def get_user_statistics(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get user statistics
    - **Owner or Teacher/Admin** can view
    """
    user = crud_users.get_user(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Check access permission
    ensure_view_user(current_user, user_id)

    # Get statistics — counts via SQL to avoid a hard limit and to avoid
    # loading full ORM objects. Deployment status is derived from the
    # latest task (Deployment has no stored status column), so we fetch
    # deployment IDs and pass them to bulk_get_task_summary.
    total_apps = (
        db.query(func.count(App.appId))
        .filter(App.userId == user_id, App.deleted_at.is_(None))
        .scalar()
        or 0
    )

    dep_ids = [
        row[0]
        for row in db.query(Deployment.deploymentId)
        .filter(Deployment.userId == user_id, Deployment.deleted_at.is_(None))
        .all()
    ]
    total_deployments = len(dep_ids)

    task_summary = crud_deployments.bulk_get_task_summary(db, dep_ids)
    statuses = [
        crud_deployments.derive_status(task_status, task_type)
        for task_status, task_type, _ in task_summary.values()
    ]

    return UserStatistics(
        total_apps=total_apps,
        total_deployments=total_deployments,
        successful_deployments=sum(1 for s in statuses if s == "success"),
        failed_deployments=sum(1 for s in statuses if s == "failed"),
        pending_deployments=sum(1 for s in statuses if s == "pending"),
    )

# ----------------------------------------------------------------
# UPDATE USER (ROLE ONLY — ADMIN)
# ----------------------------------------------------------------
@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: UUID,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a user record.

    Profile edits (firstName/lastName/email/username) happen exclusively
    in Keycloak; ``UserUpdate`` only carries ``role`` and ``courseId``.
    Both are admin-only: ``role`` changes go through
    :func:`ensure_change_user_role` (403 with ``{code: "role_required",
    required: ["admin"]}``), and ``courseId`` changes require admin too.
    """
    user = crud_users.get_user(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Any change to a non-profile column (role, courseId) is admin-only.
    payload = user_update.model_dump(exclude_unset=True)
    if "role" in payload:
        ensure_change_user_role(current_user)
    if "courseId" in payload and current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "role_required",
                "required": [UserRole.ADMIN.value],
            },
        )

    updated_user = crud_users.update_user(db, user_id, user_update)
    return updated_user


# ----------------------------------------------------------------
# DELETE USER
# ----------------------------------------------------------------
# User deletion is handled exclusively in Keycloak; the app exposes no
# delete endpoint to avoid dangling user_ids on apps/deployments/tasks.
