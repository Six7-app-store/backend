"""Unit-Tests fuer ``app.utils.time``.

Die Datenbank speichert UTC ohne Zeitzone. Ohne Offset im JSON liest der
Browser einen Zeitstempel als Ortszeit und zeigt ihn in der Sommerzeit zwei
Stunden zu frueh an.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.schemas import TaskSummary
from app.utils.time import UtcDatetime, utc_isoformat

pytestmark = pytest.mark.unit


class _Stamp(BaseModel):
    at: UtcDatetime
    maybe: UtcDatetime | None = None


def test_naive_timestamp_is_serialized_as_utc() -> None:
    stamp = _Stamp(at=datetime(2026, 10, 24, 12, 59, 45, 365538))
    assert stamp.model_dump(mode="json")["at"] == "2026-10-24T12:59:45.365538Z"


def test_aware_timestamp_keeps_its_offset() -> None:
    stamp = _Stamp(at=datetime(2026, 10, 24, 12, 59, 45, tzinfo=UTC))
    assert stamp.model_dump(mode="json")["at"] == "2026-10-24T12:59:45Z"


def test_optional_timestamp_stays_null() -> None:
    stamp = _Stamp(at=datetime(2026, 10, 24, 12, 0))
    assert stamp.model_dump(mode="json")["maybe"] is None


def test_python_dump_and_validation_stay_naive() -> None:
    """Nur das JSON bekommt den Offset; Vergleiche mit DB-Werten bleiben moeglich."""
    stamp = _Stamp(at="2026-10-24T12:59:45")
    assert stamp.at.tzinfo is None
    assert stamp.model_dump()["at"].tzinfo is None


def test_response_schema_uses_utc() -> None:
    summary = TaskSummary(
        taskId="00000000-0000-0000-0000-000000000001",
        type="deploy",
        status="pending",
        created_at=datetime(2026, 10, 24, 12, 59, 45),
    )
    assert summary.model_dump(mode="json")["created_at"] == "2026-10-24T12:59:45Z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-24T12:59:58.000000", "2026-10-24T12:59:58+00:00"),
        ("2026-10-24T12:59:58Z", "2026-10-24T12:59:58Z"),
        ("2026-10-24T14:59:58+02:00", "2026-10-24T14:59:58+02:00"),
        ("kein Datum", "kein Datum"),
        ("", ""),
        (None, None),
    ],
)
def test_utc_isoformat(value: str | None, expected: str | None) -> None:
    assert utc_isoformat(value) == expected
