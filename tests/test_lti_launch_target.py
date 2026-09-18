"""Where an LTI launch lands.

A student clicking a Moodle activity wants the environment that activity
is about, not a dashboard they then have to navigate out of. These tests
cover :func:`app.services.lti_service.resolve_launch_target`, which turns
the launch into a concrete frontend path.

Two rules carry the whole thing:

* the Moodle course mapping is a *narrowing hint*, never an access
  grant — the candidate set is always "environments this user is a
  member of", and the mapping only picks among them;
* when it cannot narrow to exactly one, it says so by returning the
  list, rather than guessing at an environment.
"""

import uuid

import pytest

from app.models import (
    App,
    Course,
    CourseTeacher,
    Deployment,
    LtiContext,
    Team,
    User,
    UserRole,
    UserToTeam,
)
from app.services.lti_service import (
    TARGET_ENVIRONMENTS,
    TARGET_MAP_COURSE,
    resolve_launch_target,
)

pytestmark = pytest.mark.integration

ISSUER = "https://moodle.test"


# ----------------------------------------------------------------
# Fixtures / builders
# ----------------------------------------------------------------
def _user(db, role, course_id=None):
    u = User(
        userId=uuid.uuid4(),
        keycloak_id=uuid.uuid4().hex,
        email=f"{uuid.uuid4().hex[:8]}@dhbw.de",
        username=uuid.uuid4().hex[:8],
        firstName="Test",
        lastName="User",
        role=role,
        courseId=course_id,
    )
    db.add(u)
    db.commit()
    return u


def _course(db, name="Cloud Computing"):
    c = Course(courseId=uuid.uuid4(), name=name)
    db.add(c)
    db.commit()
    return c


def _teaches(db, teacher, course):
    db.add(CourseTeacher(courseId=course.courseId, userId=teacher.userId))
    db.commit()


def _context(db, course=None, context_id=None):
    ctx = LtiContext(
        issuer=ISSUER,
        context_id=context_id or uuid.uuid4().hex,
        title="Cloud Computing",
        courseId=course.courseId if course else None,
    )
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ctx


def _environment(db, owner, member=None, *, deleted=False):
    """One deployment owned by ``owner``, with ``member`` picked into it."""
    a = App(
        appId=uuid.uuid4(),
        name=f"App {uuid.uuid4().hex[:6]}",
        userId=owner.userId,
        git_link="https://example.com/repo.git",
    )
    db.add(a)
    db.commit()

    d = Deployment(
        deploymentId=uuid.uuid4(),
        name="Labor",
        userId=owner.userId,
        appId=a.appId,
    )
    if deleted:
        from app.utils.time import utcnow

        d.deleted_at = utcnow()
    db.add(d)
    db.commit()

    if member is not None:
        team = Team(teamId=uuid.uuid4(), name="Team A", deploymentId=d.deploymentId)
        db.add(team)
        db.commit()
        db.add(UserToTeam(teamId=team.teamId, userId=member.userId))
        db.commit()

    db.refresh(d)
    return d


# ================================================================
# STAFF
# ================================================================
def test_staff_is_sent_to_map_an_unmapped_moodle_course(db):
    teacher = _user(db, UserRole.TEACHER)
    ctx = _context(db)

    target = resolve_launch_target(db, teacher, ctx)

    assert target == f"{TARGET_MAP_COURSE}?context={ctx.ltiContextId}"


def test_staff_with_a_mapped_course_goes_to_the_environments_list(db):
    teacher = _user(db, UserRole.TEACHER)
    course = _course(db)
    ctx = _context(db, course)

    assert resolve_launch_target(db, teacher, ctx) == TARGET_ENVIRONMENTS


def test_staff_without_a_context_goes_to_the_environments_list(db):
    teacher = _user(db, UserRole.TEACHER)

    assert resolve_launch_target(db, teacher, None) == TARGET_ENVIRONMENTS


# ================================================================
# STUDENT
# ================================================================
def test_student_without_an_environment_gets_the_list(db):
    student = _user(db, UserRole.STUDENT)

    assert resolve_launch_target(db, student, None) == TARGET_ENVIRONMENTS


def test_student_with_exactly_one_environment_lands_on_it(db):
    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)
    dep = _environment(db, teacher, student)

    target = resolve_launch_target(db, student, None)

    assert target == f"{TARGET_ENVIRONMENTS}/{dep.deploymentId}"


def test_student_with_several_environments_gets_the_list(db):
    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)
    _environment(db, teacher, student)
    _environment(db, teacher, student)

    assert resolve_launch_target(db, student, None) == TARGET_ENVIRONMENTS


def test_the_moodle_course_narrows_several_environments_to_one(db):
    """The mapping earns its keep here: two environments, one course."""
    cloud_teacher = _user(db, UserRole.TEACHER)
    other_teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)

    cloud = _course(db, "Cloud Computing")
    _teaches(db, cloud_teacher, cloud)

    wanted = _environment(db, cloud_teacher, student)
    _environment(db, other_teacher, student)  # a different lecture

    ctx = _context(db, cloud)

    target = resolve_launch_target(db, student, ctx)

    assert target == f"{TARGET_ENVIRONMENTS}/{wanted.deploymentId}"


def test_a_mapped_course_without_teachers_does_not_empty_the_set(db):
    """Mapping to a course nobody is registered to teach must not strand
    an enrolled student on an empty list."""
    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)
    dep = _environment(db, teacher, student)

    ctx = _context(db, _course(db))  # mapped, but no CourseTeacher rows

    assert resolve_launch_target(db, student, ctx) == (
        f"{TARGET_ENVIRONMENTS}/{dep.deploymentId}"
    )


def test_the_mapping_never_widens_access(db):
    """A student who is in no team sees the list, mapping or not — the
    Moodle course picks among their environments, it does not add any."""
    teacher = _user(db, UserRole.TEACHER)
    stranger = _user(db, UserRole.STUDENT)
    course = _course(db)
    _teaches(db, teacher, course)
    _environment(db, teacher, member=None)

    ctx = _context(db, course)

    assert resolve_launch_target(db, stranger, ctx) == TARGET_ENVIRONMENTS


def test_a_deleted_environment_is_not_a_candidate(db):
    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)
    _environment(db, teacher, student, deleted=True)

    assert resolve_launch_target(db, student, None) == TARGET_ENVIRONMENTS
