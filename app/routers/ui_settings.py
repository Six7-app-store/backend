"""The instance's look: accent colour and logos.

Reading is unauthenticated on purpose — the login page shows the logo
and the accent before anyone is signed in. Changing is admin-only.
"""
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.crud import ui_settings as crud_ui_settings
from app.database import get_db
from app.models import User
from app.schemas import LogoVariant, UiLogoUpload, UiSettingsResponse, UiSettingsUpdate
from app.utils.app_image import parse_image_data_url
from app.utils.permissions import require_admin

router = APIRouter()

# The logo URL carries ``?v=<updatedAt>``, so a new upload is a new URL and
# a day of caching never shows a stale logo.
_LOGO_CACHE_CONTROL = "public, max-age=86400"
# An uploaded SVG is served from the API's origin. It is only ever meant
# for an ``<img>``; opened directly, this keeps any script in it inert.
_LOGO_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


@router.get("", response_model=UiSettingsResponse)
def get_ui_settings(db: Session = Depends(get_db)):
    """Current accent colour and which logos were uploaded. No authentication.

    ``null`` and ``false`` mean the frontend's built-in default applies.
    """
    return crud_ui_settings.to_response(crud_ui_settings.get(db))


@router.patch("", response_model=UiSettingsResponse)
def update_ui_settings(
    payload: UiSettingsUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Change the accent colour (``#RRGGBB``); ``null`` resets it to the default."""
    if "accentColor" in payload.model_fields_set:
        row = crud_ui_settings.set_accent_color(db, payload.accentColor)
    else:
        row = crud_ui_settings.get(db)
    return crud_ui_settings.to_response(row)


@router.get(
    "/logos/{variant}",
    response_class=Response,
    responses={
        200: {"content": {"image/*": {}}, "description": "The uploaded logo."},
        404: {"description": "No logo uploaded for this variant; the default applies."},
    },
)
def get_logo(variant: LogoVariant, db: Session = Depends(get_db)):
    """The uploaded logo as an image. No authentication."""
    logo = crud_ui_settings.get_logo(db, variant)
    if logo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "logo_not_set", "variant": variant},
        )
    data, mime = logo
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": _LOGO_CACHE_CONTROL,
            "Content-Security-Policy": _LOGO_CSP,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put("/logos/{variant}", response_model=UiSettingsResponse)
def upload_logo(
    variant: LogoVariant,
    payload: UiLogoUpload,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Replace a logo with an uploaded image (data-URL, at most 2 MiB)."""
    data, mime = parse_image_data_url(payload.image)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "image_required", "message": "image must not be empty"},
        )
    row = crud_ui_settings.set_logo(db, variant, data, mime)
    return crud_ui_settings.to_response(row)


@router.delete("/logos/{variant}", response_model=UiSettingsResponse)
def reset_logo(
    variant: LogoVariant,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Remove an uploaded logo so the built-in default applies again."""
    row = crud_ui_settings.set_logo(db, variant, None, None)
    return crud_ui_settings.to_response(row)
