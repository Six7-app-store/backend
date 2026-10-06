from uuid import UUID

from sqlalchemy.orm import Session

from app.models import App, AppVersionApproval, AppVersionApprovalStatus
from app.schemas import AppCreate, AppUpdate
from app.utils.time import utcnow


def get_app(
    db: Session,
    app_id: UUID,
    include_deleted: bool = False,
) -> App | None:
    """Get app by ID. Hides soft-deleted apps by default.

    ``include_deleted=True`` is reserved for the rare audit lookup —
    the HTTP API never sets it.
    """
    q = db.query(App).filter(App.appId == app_id)
    if not include_deleted:
        q = q.filter(App.deleted_at.is_(None))
    return q.first()


def get_apps(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    user_id: UUID | None = None,
    include_deleted: bool = False,
) -> list[App]:
    """Get apps with optional user filter. Hides soft-deleted by default."""
    query = db.query(App)

    if not include_deleted:
        query = query.filter(App.deleted_at.is_(None))
    if user_id:
        query = query.filter(App.userId == user_id)

    return query.order_by(App.created_at, App.appId).offset(skip).limit(limit).all()


def get_visible_apps(
    db: Session,
    requesting_user_id: UUID,
    skip: int = 0,
    limit: int = 100,
) -> list[App]:
    """Return apps visible to the requesting user.

    Visibility rules:
    - Always: apps owned by the requesting user (regardless of is_private)
    - Additionally: public apps (is_private=False) that have at least one
      APPROVED version
    """
    approved_app_ids = (
        db.query(AppVersionApproval.appId)
        .filter(AppVersionApproval.status == AppVersionApprovalStatus.APPROVED)
        .distinct()
        .scalar_subquery()
    )

    return (
        db.query(App)
        .filter(App.deleted_at.is_(None))
        .filter(
            (App.userId == requesting_user_id)
            | (
                App.is_private.is_(False)
                & App.appId.in_(approved_app_ids)
            )
        )
        .order_by(App.created_at, App.appId)
        .offset(skip)
        .limit(limit)
        .all()
    )


def create_app(db: Session, app: AppCreate, user_id: UUID) -> App:
    """Create a new app."""
    db_app = App(
        name=app.name,
        description=app.description,
        git_link=app.git_link,
        is_private=app.is_private,
        userId=user_id,
    )
    db.add(db_app)
    db.commit()
    db.refresh(db_app)
    return db_app


def update_app(db: Session, app_id: UUID, app_update: AppUpdate) -> App | None:
    """Update app information.

    The ``image`` field is intentionally NOT applied here — the router
    decodes the data-URL via ``parse_image_data_url`` and calls
    ``set_app_image`` directly. ``model_dump`` would otherwise pass
    the data-URL string straight into the ``LargeBinary`` column.

    ``git_link`` is intentionally excluded as well: once an app has
    deployments, changing the repo would make existing deployments
    point at a different repo than they originally deployed.
    ``AppUpdate`` already drops the field from the schema; this
    exclude is defense-in-depth in case the schema is replaced or
    extended later.
    """
    db_app = get_app(db, app_id)
    if not db_app:
        return None

    update_data = app_update.model_dump(
        exclude_unset=True, exclude={"image", "git_link"}
    )
    for field, value in update_data.items():
        setattr(db_app, field, value)

    db.commit()
    db.refresh(db_app)
    return db_app


def set_app_image(
    db: Session,
    app_id: UUID,
    image_bytes: bytes | None,
    image_mime: str | None,
) -> App | None:
    """Persist the image bytes + mime atomically.

    Both args ``None`` clears the image. Otherwise both must be set —
    the router enforces that via ``parse_image_data_url`` before
    calling here, so this function trusts its inputs.
    """
    db_app = get_app(db, app_id)
    if not db_app:
        return None
    db_app.image = image_bytes
    db_app.image_mime = image_mime
    db.commit()
    db.refresh(db_app)
    return db_app


def soft_delete_app(db: Session, app_id: UUID) -> bool:
    """Mark an app as deleted without removing the row.

    The row stays so existing ``deployments.appId`` foreign keys keep
    resolving, but list queries skip it. Restoring an app means clearing
    ``deleted_at`` directly via SQL.
    """
    db_app = get_app(db, app_id)
    if not db_app:
        return False
    db_app.deleted_at = utcnow()
    db.commit()
    return True
