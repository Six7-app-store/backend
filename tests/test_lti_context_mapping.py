"""Mapping a Moodle course onto a local course.

A launch records which Moodle course it came from but leaves the mapping
empty on purpose — a Moodle course and a Studiengruppe are different
things. Somebody who teaches the course has to say they belong together,
and these tests pin down who that is.

The mapping decides where a student launch lands, so the gate is the
same one that guards editing the course itself: admin, or a registered
teacher *of that course*. A teacher of some other course is refused,
including when undoing a colleague's mapping.
"""

import uuid

import pytest

from app.config import settings
from app.models import Course, CourseTeacher, LtiContext

pytestmark = pytest.mark.integration

ISSUER = "https://moodle.test"


@pytest.fixture(autouse=True)
def lti_on(monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", True)


def _course(db, name="Cloud Computing"):
    c = Course(courseId=uuid.uuid4(), name=name)
    db.add(c)
    db.commit()
    return c


def _context(db, course=None):
    ctx = LtiContext(
        issuer=ISSUER,
        context_id=uuid.uuid4().hex,
        title="Cloud Computing",
        courseId=course.courseId if course else None,
    )
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ctx


def _teaches(db, user, course):
    db.add(CourseTeacher(courseId=course.courseId, userId=user.userId))
    db.commit()


# ================================================================
# READ
# ================================================================
def test_teacher_can_read_a_recorded_moodle_course(client, db):
    ctx = _context(db)

    resp = client.get(f"/lti/contexts/{ctx.ltiContextId}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["context_id"] == ctx.context_id
    assert body["title"] == "Cloud Computing"
    assert body["courseId"] is None


def test_students_cannot_read_moodle_courses(student_client, db):
    ctx = _context(db)

    assert student_client.get(f"/lti/contexts/{ctx.ltiContextId}").status_code == 403


def test_unknown_context_is_a_404(client):
    resp = client.get(f"/lti/contexts/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "lti_context_not_found"


# ================================================================
# MAP
# ================================================================
def test_the_course_teacher_can_map(client, db, mock_user):
    course = _course(db)
    _teaches(db, mock_user, course)
    ctx = _context(db)

    resp = client.put(
        f"/lti/contexts/{ctx.ltiContextId}",
        json={"courseId": str(course.courseId)},
    )

    assert resp.status_code == 200
    assert resp.json()["courseId"] == str(course.courseId)
    db.refresh(ctx)
    assert ctx.courseId == course.courseId


def test_a_teacher_of_another_course_is_refused(client, db):
    """No CourseTeacher row for this course — mapping it would attach
    another lecturer's students to a Moodle course of your choosing."""
    course = _course(db)
    ctx = _context(db)

    resp = client.put(
        f"/lti/contexts/{ctx.ltiContextId}",
        json={"courseId": str(course.courseId)},
    )

    assert resp.status_code == 403
    db.refresh(ctx)
    assert ctx.courseId is None


def test_students_cannot_map(student_client, db):
    course = _course(db)
    ctx = _context(db)

    resp = student_client.put(
        f"/lti/contexts/{ctx.ltiContextId}",
        json={"courseId": str(course.courseId)},
    )

    assert resp.status_code == 403


def test_admins_can_map_any_course(admin_client, db):
    course = _course(db)
    ctx = _context(db)

    resp = admin_client.put(
        f"/lti/contexts/{ctx.ltiContextId}",
        json={"courseId": str(course.courseId)},
    )

    assert resp.status_code == 200
    assert resp.json()["courseId"] == str(course.courseId)


def test_mapping_to_an_unknown_course_is_a_404(client, db):
    ctx = _context(db)

    resp = client.put(
        f"/lti/contexts/{ctx.ltiContextId}",
        json={"courseId": str(uuid.uuid4())},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "course_not_found"


# ================================================================
# UNMAP
# ================================================================
def test_the_course_teacher_can_unmap(client, db, mock_user):
    course = _course(db)
    _teaches(db, mock_user, course)
    ctx = _context(db, course)

    resp = client.put(f"/lti/contexts/{ctx.ltiContextId}", json={"courseId": None})

    assert resp.status_code == 200
    assert resp.json()["courseId"] is None
    db.refresh(ctx)
    assert ctx.courseId is None


def test_a_foreign_teacher_cannot_undo_someone_elses_mapping(client, db):
    course = _course(db)
    ctx = _context(db, course)

    resp = client.put(f"/lti/contexts/{ctx.ltiContextId}", json={"courseId": None})

    assert resp.status_code == 403
    db.refresh(ctx)
    assert ctx.courseId == course.courseId



# ================================================================
# KILL SWITCH
# ================================================================
def test_the_endpoints_are_off_when_lti_is(client, db, monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", False)
    ctx = _context(db)

    assert client.get(f"/lti/contexts/{ctx.ltiContextId}").status_code == 503
    assert client.put(
        f"/lti/contexts/{ctx.ltiContextId}", json={"courseId": None}
    ).status_code == 503


# ================================================================
# H-7 REGRESSION — auth bypass when mapped course is deleted
# ================================================================
def test_student_cannot_detach_context_when_mapped_course_is_deleted(
    student_client, db
):
    """Before the fix: if a context's mapped course was deleted,
    ``ensure_edit_course`` was inside an ``if current is not None:`` guard
    and was silently skipped — any authenticated user could then detach
    the mapping.  After the fix a student must still get 403.
    """
    course = _course(db)
    ctx = _context(db, course)

    # Simulate the course being deleted after the mapping was made.
    db.delete(course)
    db.commit()
    # The context still holds the old courseId FK; Postgres ON DELETE SET NULL
    # may clear it depending on schema, but we test the guard path directly
    # by checking the ctx still has the courseId set (or that 403 is returned
    # regardless of db state at this point).
    db.refresh(ctx)

    resp = student_client.put(
        f"/lti/contexts/{ctx.ltiContextId}", json={"courseId": None}
    )

    assert resp.status_code == 403


def test_teacher_can_still_detach_context_when_mapped_course_is_deleted(
    client, db
):
    """A teacher/admin should be allowed to clean up an orphaned mapping
    (their role satisfies the ``ensure_view_course_detail`` fallback).
    """
    course = _course(db)
    ctx = _context(db, course)

    db.delete(course)
    db.commit()
    db.refresh(ctx)

    # client fixture is authenticated as a teacher/admin
    resp = client.put(
        f"/lti/contexts/{ctx.ltiContextId}", json={"courseId": None}
    )

    assert resp.status_code == 200
