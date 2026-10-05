from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.database import get_db
from app.models import User
from app.routers.apps import _serialize_app, load_variable_definitions
from app.schemas import (
    AppResponse,
    AppVersionApprovalDecision,
    AppVersionApprovalResponse,
    AppVersionApprovalWithApp,
)
from app.utils.permissions import require_admin

router = APIRouter()


# ----------------------------------------------------------------
# PENDING REVIEW QUEUE
# ----------------------------------------------------------------
@router.get(
    "/apps/versions/pending",
    response_model=list[AppVersionApprovalWithApp],
)
def list_pending_versions(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Return all version submissions awaiting admin review, oldest first."""
    return crud_approvals.get_pending_approvals(db)


# ----------------------------------------------------------------
# APPROVE VERSION
# ----------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions/{version_tag}/approve",
    response_model=AppVersionApprovalResponse,
)
def approve_version(
    app_id: UUID,
    version_tag: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Approve a PENDING version — makes it deployable by all users.

    Validates the ``@openstack`` markers in this version's OpenTofu
    variable files before flipping the status, using the same helper as
    ``GET /apps/{id}/variables``.
    """
    app = _require_app(db, app_id)

    # Block approval if any variable carries a marker error. If the git repo
    # is unreachable (400/500), skip validation rather than hard-blocking.
    try:
        variables = load_variable_definitions(app, version_tag)
        marker_errors = [
            v.get("markerError") for v in variables if v.get("markerError")
        ]
        if marker_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": (
                        "Version kann nicht approved werden — fehlerhafte "
                        "@openstack-Marker in den Variablen-Dateien"
                    ),
                    "marker_errors": marker_errors,
                },
            )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            raise
        # 400 (no git_link) or 500 (git unreachable) — skip validation

    return crud_approvals.approve(db, app_id, version_tag, current_user.userId)


# ----------------------------------------------------------------
# REJECT VERSION
# ----------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions/{version_tag}/reject",
    response_model=AppVersionApprovalResponse,
)
def reject_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Reject a PENDING version with a mandatory reason."""
    _require_app(db, app_id)
    return crud_approvals.reject(
        db, app_id, version_tag, current_user.userId, body.rejection_reason
    )


# ----------------------------------------------------------------
# REVOKE APPROVED VERSION
# ----------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions/{version_tag}/revoke",
    response_model=AppVersionApprovalResponse,
)
def revoke_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Revoke a previously APPROVED version with a mandatory reason (sets status to REJECTED)."""
    _require_app(db, app_id)
    return crud_approvals.revoke(db, app_id, version_tag, current_user.userId, body.rejection_reason)


# ----------------------------------------------------------------
# EMERGENCY DEACTIVATION
# ----------------------------------------------------------------
@router.put(
    "/apps/{app_id}",
    response_model=AppResponse,
)
def deactivate_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Emergency deactivation: set app to private so it disappears from
    the store immediately. Does not delete the app or its deployments."""
    from app.schemas import AppUpdate

    _require_app(db, app_id)
    updated = crud_apps.update_app(db, app_id, AppUpdate(is_private=True))
    return _serialize_app(updated)


# ----------------------------------------------------------------
# HELPER
# ----------------------------------------------------------------
def _require_app(db: Session, app_id: UUID):
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found",
        )
    return app
