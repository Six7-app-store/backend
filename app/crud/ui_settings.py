"""CRUD for the single ``ui_settings`` row.

The row is created on the first write, not by the migration: a missing
row and a row full of ``NULL`` both mean "built-in defaults", so readers
never need it to exist.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import UiSettings
from app.schemas import LogoVariant, UiLogos, UiSettingsResponse

SETTINGS_ID = 1


def get(db: Session) -> UiSettings | None:
    return db.get(UiSettings, SETTINGS_ID)


def _get_or_create(db: Session) -> UiSettings:
    row = get(db)
    if row is None:
        row = UiSettings(id=SETTINGS_ID)
        db.add(row)
    return row


def to_response(row: UiSettings | None) -> UiSettingsResponse:
    if row is None:
        return UiSettingsResponse()
    return UiSettingsResponse(
        accentColor=row.accent_color,
        logos=UiLogos(
            light=row.logo_light is not None,
            dark=row.logo_dark is not None,
            icon=row.logo_icon is not None,
        ),
        updatedAt=row.updated_at,
    )


def set_accent_color(db: Session, color: str | None) -> UiSettings:
    row = _get_or_create(db)
    row.accent_color = color.upper() if color else None
    db.commit()
    db.refresh(row)
    return row


def get_logo(db: Session, variant: LogoVariant) -> tuple[bytes, str] | None:
    """The uploaded logo's ``(bytes, mime)``, or ``None`` when the default applies."""
    row = get(db)
    if row is None:
        return None
    data = getattr(row, f"logo_{variant}")
    mime = getattr(row, f"logo_{variant}_mime")
    if data is None or mime is None:
        return None
    return data, mime


def set_logo(
    db: Session, variant: LogoVariant, data: bytes | None, mime: str | None
) -> UiSettings:
    """Store a logo, or clear it with ``data=None`` so the default applies again."""
    row = _get_or_create(db)
    setattr(row, f"logo_{variant}", data)
    setattr(row, f"logo_{variant}_mime", mime if data is not None else None)
    db.commit()
    db.refresh(row)
    return row
