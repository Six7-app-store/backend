"""Time helpers.

:func:`utcnow` returns a naive UTC timestamp because every ``DateTime``
column in :mod:`app.models` is declared naive; mixing aware and naive
datetimes would raise ``TypeError`` on comparison.

A naive timestamp must not leave the API as it is: a browser reads an ISO
string without offset as *local* time, so the UI showed every timestamp two
hours early in CEST. :data:`UtcDatetime` and :func:`utc_isoformat` mark the
values as UTC on the way out.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import SerializerFunctionWrapHandler, WrapSerializer


def utcnow() -> datetime:
    """Return the current UTC time as a naive ``datetime`` (no tzinfo)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _serialize_as_utc(value: datetime, handler: SerializerFunctionWrapHandler) -> Any:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return handler(value)


# Response field type for timestamps from the database. Validation is
# unchanged (naive stays naive), only the JSON output gains the ``Z``.
UtcDatetime = Annotated[datetime, WrapSerializer(_serialize_as_utc, when_used="json")]


def utc_isoformat(value: str | None) -> str | None:
    """Mark an ISO timestamp without offset as UTC.

    For strings passed through from OpenStack, such as
    ``OS-SRV-USG:launched_at``. A value with an offset, or one that does not
    parse, comes back unchanged.
    """
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=UTC).isoformat()
