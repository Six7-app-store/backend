import contextlib
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.database import get_db
from app.models import User, UserRole
from app.schemas import (
    AppCreate,
    AppResponse,
    AppUpdate,
    AppVersionApprovalResponse,
    AppVersionApprovalSubmit,
    AppWithVersions,
)
from app.services.git_service import git_service
from app.services.hcl_variable_parser import load_variable_definitions
from app.utils.app_image import build_image_data_url, parse_image_data_url
from app.utils.auth import get_current_user
from app.utils.capabilities import (
    ensure_delete_app,
    ensure_edit_app,
    ensure_submit_app_version,
    ensure_view_app,
)

logger = logging.getLogger(__name__)


def _serialize_app(app):
    """Replace ``app.image`` (bytes) with the data-URL form in-place.

    The ORM model carries the raw bytes plus a separate mime column.
    The Pydantic ``AppResponse`` schema declares ``image: Optional[str]``
    and uses ``from_attributes=True``, so Pydantic reads ``app.image``
    directly. Overwriting that attribute with the rebuilt data-URL
    means the response serialiser sees a string and the wire format
    matches the schema. Returns ``app`` so callers can chain.
    """
    if app is None:
        return None
    raw_bytes = getattr(app, "image", None)
    if isinstance(raw_bytes, (bytes, memoryview, bytearray)):
        app.image = build_image_data_url(bytes(raw_bytes), getattr(app, "image_mime", None))
    return app


def _version_tag(version) -> str:
    """Extract the tag string from a git version entry.

    ``git_service.get_versions`` yields either a plain tag string or a
    dict carrying the tag under one of ``version`` / ``releaseTag`` /
    ``tag``. Returns ``""`` when nothing matches, so callers can treat
    the result uniformly (empty string is falsy and never a valid tag).
    """
    if isinstance(version, str):
        return version
    return version.get("version") or version.get("releaseTag") or version.get("tag", "")


router = APIRouter()


# ----------------------------------------------------------------
# Pydantic schema for the /apps/{id}/variables response
# ----------------------------------------------------------------
# Mirrors the exact keys ``_parse_one_variable`` returns (mixed
# snake/camelCase) so the frontend and generated OpenAPI stay in sync.
class _MarkerErrorPayload(BaseModel):
    variable: str
    message: str
    location: str
    code: str | None = None


class AppVariableResponse(BaseModel):
    """Shape of one entry in ``GET /apps/{id}/variables``.

    Keys match what ``_parse_one_variable`` writes to the dict exactly —
    the frontend reads ``osType``/``osMode``/``osMulti``/``osScope``/
    ``varScope``/``fileExtensions`` in camelCase and the rest in
    snake/lowercase. Keys are kept verbatim (no auto-aliasing).
    """

    # ``populate_by_name`` lets callers construct with either field name
    # or alias; the dict-style names are the canonical source.
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    type: str
    description: str | None = None
    # Default is typed (Number/Bool/List/Dict/None); ``Any`` is
    # deliberately broad because HCL covers a whole literal family.
    default: Any | None = None
    required: bool
    source: str
    osType: str | None = None
    osMode: str | None = None
    osMulti: bool | None = None
    osScope: str | None = None
    varScope: str | None = None
    fileExtensions: list[str] | None = None
    markerError: _MarkerErrorPayload | None = None
    # ``template_key`` is null for ``source = terraform`` variables and
    # carries the per-template key (``default`` for the legacy layout,
    # or the subdirectory name like ``webserver``/``database`` in
    # multi-image apps) for ``source = packer`` variables. Lets the
    # wizard group Packer variables per image and avoid name collisions
    # across templates.
    template_key: str | None = None


# ----------------------------------------------------------------
# GET ALL APPS
# ----------------------------------------------------------------
@router.get("/", response_model=list[AppResponse])
def list_apps(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List apps visible to the current user.

    Admins see every non-deleted app (full platform view). Everyone else,
    including teachers, sees the student-style filter: own apps + public
    apps with at least one approved version.
    """
    if current_user.role == UserRole.ADMIN:
        apps = crud_apps.get_apps(db, skip=skip, limit=limit)
    else:
        apps = crud_apps.get_visible_apps(db, current_user.userId, skip=skip, limit=limit)
    return [_serialize_app(a) for a in apps]


# ----------------------------------------------------------------
# GET APP BY ID
# ----------------------------------------------------------------
@router.get("/{app_id}", response_model=AppWithVersions)
def get_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get app by ID with available versions.

    Returns the app with all available versions from Git.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    ensure_view_app(current_user, app, db=db)

    if app.git_link:
        try:
            versions = git_service.get_versions(app.git_link)
            app.versions = [
                _version_tag(v)
                for v in versions
                if _version_tag(v)
            ]
        except Exception as e:
            app.versions = []
            logger.warning(f"Could not fetch versions: {str(e)}")
    else:
        app.versions = []

    return _serialize_app(app)


# ----------------------------------------------------------------
# GET APP VARIABLES
# ----------------------------------------------------------------
@router.get("/{app_id}/variables", response_model=list[AppVariableResponse])
def get_app_variables(
    app_id: UUID,
    version: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get dynamic app variables from app's Git repository
    Parses variables.tf file and returns all configurable variables

    Returns:
    - name: Variable name
    - type: Variable type (string, number, bool, list, map, etc.)
    - description: Variable description
    - default: Default value (if any)
    - required: Whether variable is required
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission. ``ensure_view_app`` enforces the matrix:
    # owner OR admin sees private/unapproved, others need the
    # public+approved combination.
    ensure_view_app(current_user, app, db=db)

    variables = load_variable_definitions(app, version)
    if not variables:
        logger.warning("No variables found for app %s version %s", app_id, version)

    # Marker errors travel per-variable in the ``markerError`` field; the
    # endpoint does not 400 on a single bad marker but leaves the frontend
    # to show it inline, keeping the other variables usable.
    bad = [v for v in variables if v.get("markerError")]
    if bad:
        logger.warning(
            "App %s version %s has %d variable(s) with bad @openstack markers: %s",
            app_id, version, len(bad), [v["name"] for v in bad],
        )

    return variables


# ----------------------------------------------------------------
# CREATE APP
# ----------------------------------------------------------------
@router.post("/", response_model=AppResponse, status_code=status.HTTP_201_CREATED)
def create_app(
    app: AppCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new app
    - **All authenticated users** can create apps
    - **Git repository access is verified** before creating the app
    """
    # Decode the optional image data-URL up front so a malformed
    # payload fails before we hit Keycloak / Git / DB.
    image_bytes, image_mime = parse_image_data_url(app.image)

    # Verify repository access if git_link is provided
    if app.git_link:
        access_result = git_service.verify_repository_access(app.git_link)
        if not access_result['success']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=access_result['message']
            )
        logger.info(f"Repository access verified for {app.git_link}")

    db_app = crud_apps.create_app(db, app, current_user.userId)
    if image_bytes is not None:
        db_app = crud_apps.set_app_image(db, db_app.appId, image_bytes, image_mime)

    # Auto-submit all tags for review if requested (public apps only)
    if app.submit_all_versions and not app.is_private and app.git_link:
        try:
            versions = git_service.get_versions(app.git_link)
            for v in versions:
                tag = _version_tag(v)
                if tag:
                    with contextlib.suppress(Exception):
                        crud_approvals.submit_version(db, app_id=db_app.appId, version_tag=tag)
        except Exception as e:
            logger.warning(f"Could not auto-submit versions for app {db_app.appId}: {e}")

    return _serialize_app(db_app)


# ----------------------------------------------------------------
# UPDATE APP
# ----------------------------------------------------------------
@router.put("/{app_id}", response_model=AppResponse)
def update_app(
    app_id: UUID,
    app_update: AppUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an app.

    ``git_link`` is immutable after creation — sending it in the body
    returns HTTP 400. Use ``is_private`` to toggle visibility.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission — owner-or-admin only.
    ensure_edit_app(current_user, app)

    image_was_provided = "image" in app_update.model_fields_set
    image_bytes, image_mime = (None, None)
    if image_was_provided:
        image_bytes, image_mime = parse_image_data_url(app_update.image)

    updated_app = crud_apps.update_app(db, app_id, app_update)
    if image_was_provided:
        updated_app = crud_apps.set_app_image(db, app_id, image_bytes, image_mime)
    return _serialize_app(updated_app)


# ----------------------------------------------------------------
# SUBMIT VERSION FOR REVIEW
# ----------------------------------------------------------------
@router.post(
    "/{app_id}/versions/{version_tag}/submit",
    response_model=AppVersionApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit a specific version tag for admin review.

    Owner OR admin only. A REJECTED version can be resubmitted; PENDING
    and APPROVED cannot.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_submit_app_version(current_user, app)

    if not app.git_link:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="App has no git repository configured",
        )

    # Marker validation — blocks submit on invalid @openstack markers.
    # Same logic as the approve endpoint; git errors (400/500) are
    # skipped so submit still works when the repo is unreachable.
    try:
        variables = load_variable_definitions(app, version_tag)
        marker_errors = [v.get("markerError") for v in variables if v.get("markerError")]
        if marker_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": (
                        "Version kann nicht eingereicht werden — fehlerhafte "
                        "@openstack-Marker in den Variablen-Dateien"
                    ),
                    "marker_errors": marker_errors,
                },
            )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            raise
        # 400 (no git_link, handled above) or 500 (git unreachable) —
        # allow submit anyway.

    return crud_approvals.submit_version(
        db, app_id=app_id, version_tag=version_tag, diff_url=body.diff_url, notes=body.notes
    )


# ----------------------------------------------------------------
# WITHDRAW VERSION SUBMISSION
# ----------------------------------------------------------------
@router.delete(
    "/{app_id}/versions/{version_tag}/submit",
    status_code=status.HTTP_204_NO_CONTENT,
)
def withdraw_version(
    app_id: UUID,
    version_tag: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Withdraw a PENDING version submission.

    Owner OR admin only. Deletes the approval entry so the version
    appears as unsubmitted again.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_submit_app_version(current_user, app)
    crud_approvals.withdraw(db, app_id=app_id, version_tag=version_tag)
    return None


# ----------------------------------------------------------------
# GET VERSION APPROVALS FOR APP
# ----------------------------------------------------------------
@router.get(
    "/{app_id}/versions",
    response_model=list[AppVersionApprovalResponse],
)
def list_version_approvals(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all version approval entries for an app.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_edit_app(current_user, app)

    return crud_approvals.get_approvals_for_app(db, app_id)


# ----------------------------------------------------------------
# DELETE APP
# ----------------------------------------------------------------
@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Soft-delete an app.

    Sets ``apps.deleted_at`` instead of removing the row, so any
    historical or still-running deployment that points at this app keeps
    resolving. The app stops appearing in listings and the deploy wizard;
    existing deployments live on until destroyed individually.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission — owner-or-admin only.
    ensure_delete_app(current_user, app)

    success = crud_apps.soft_delete_app(db, app_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )
    return None
