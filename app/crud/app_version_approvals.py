from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import App, AppVersionApproval, AppVersionApprovalStatus
from app.utils.time import utcnow


def submit_version(
    db: Session,
    app_id: UUID,
    version_tag: str,
    diff_url: str | None = None,
    notes: str | None = None,
) -> AppVersionApproval:
    """Submit a version for admin review.

    Raises 409 if the version already has a PENDING or APPROVED entry —
    submitting again would be a no-op at best and confusing at worst.
    REJECTED versions can be resubmitted (creates a fresh PENDING row
    after the old one is removed).
    """
    existing = (
        db.query(AppVersionApproval)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.version_tag == version_tag,
        )
        .first()
    )

    if existing:
        if existing.status == AppVersionApprovalStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Version is already pending review",
            )
        if existing.status == AppVersionApprovalStatus.APPROVED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Version is already approved",
            )
        # REJECTED → allow resubmission: delete old entry, create fresh one
        db.delete(existing)
        db.flush()

    approval = AppVersionApproval(
        appId=app_id,
        version_tag=version_tag,
        diff_url=diff_url,
        notes=notes,
        status=AppVersionApprovalStatus.PENDING,
        created_at=utcnow(),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def get_pending_approvals(db: Session) -> list[AppVersionApproval]:
    """Return all PENDING version approvals for live public apps, oldest first."""
    return (
        db.query(AppVersionApproval)
        .join(App, App.appId == AppVersionApproval.appId)
        .filter(
            AppVersionApproval.status == AppVersionApprovalStatus.PENDING,
            App.is_private.is_(False),
            App.deleted_at.is_(None),
        )
        .order_by(AppVersionApproval.created_at.asc())
        .all()
    )


def get_approvals_for_app(db: Session, app_id: UUID) -> list[AppVersionApproval]:
    """Return all version approval entries for a given app."""
    return (
        db.query(AppVersionApproval)
        .filter(AppVersionApproval.appId == app_id)
        .order_by(AppVersionApproval.created_at.desc())
        .all()
    )


def has_approved_version(db: Session, app_id: UUID, version_tag: str) -> bool:
    """Return True if the given version is approved for this app."""
    return (
        db.query(AppVersionApproval.approvalId)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.version_tag == version_tag,
            AppVersionApproval.status == AppVersionApprovalStatus.APPROVED,
        )
        .first()
    ) is not None


def has_any_approved_version(db: Session, app_id: UUID) -> bool:
    """Return True if the app has at least one approved version."""
    return (
        db.query(AppVersionApproval.approvalId)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.status == AppVersionApprovalStatus.APPROVED,
        )
        .first()
    ) is not None


def withdraw(db: Session, app_id: UUID, version_tag: str) -> None:
    """Delete a PENDING approval entry (owner withdraws submission)."""
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status != AppVersionApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only pending submissions can be withdrawn (current status: '{approval.status.value}')",
        )

    db.delete(approval)
    db.commit()


def _get_approval_or_404(
    db: Session, app_id: UUID, version_tag: str
) -> AppVersionApproval:
    approval = (
        db.query(AppVersionApproval)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.version_tag == version_tag,
        )
        .first()
    )
    if not approval:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Version approval entry not found",
        )
    return approval


def approve(
    db: Session,
    app_id: UUID,
    version_tag: str,
    admin_id: UUID,
) -> AppVersionApproval:
    """Approve a PENDING or REJECTED version."""
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status == AppVersionApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Version is already approved",
        )

    approval.status = AppVersionApprovalStatus.APPROVED
    approval.reviewed_by = admin_id
    approval.reviewed_at = utcnow()
    approval.rejection_reason = None
    db.commit()
    db.refresh(approval)
    return approval


def reject(
    db: Session,
    app_id: UUID,
    version_tag: str,
    admin_id: UUID,
    rejection_reason: str,
) -> AppVersionApproval:
    """Reject a PENDING version with a mandatory reason."""
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status != AppVersionApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject a version with status '{approval.status.value}'",
        )

    approval.status = AppVersionApprovalStatus.REJECTED
    approval.reviewed_by = admin_id
    approval.reviewed_at = utcnow()
    approval.rejection_reason = rejection_reason
    db.commit()
    db.refresh(approval)
    return approval


def revoke(
    db: Session,
    app_id: UUID,
    version_tag: str,
    admin_id: UUID,
    rejection_reason: str,
) -> AppVersionApproval:
    """Revoke a previously APPROVED version (sets status back to REJECTED)."""
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status != AppVersionApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot revoke a version with status '{approval.status.value}'",
        )

    approval.status = AppVersionApprovalStatus.REJECTED
    approval.reviewed_by = admin_id
    approval.reviewed_at = utcnow()
    approval.rejection_reason = rejection_reason
    db.commit()
    db.refresh(approval)
    return approval
