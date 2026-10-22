"""Building a Studiengruppe out of a Moodle course's member list.

The mapping endpoint next door attaches a Moodle course to a
Studiengruppe that already exists. This one exists for the other case —
there is none yet — and fills it from what the platform reports through
NRPS.

The platform is not spoken to here. ``fetch_context_members`` is
replaced per test, because what is worth pinning down is not the HTTP
call but what the import does with a roster: which members become
accounts, which are deliberately left alone, and what a member list can
and cannot talk this application into.

The last part is the point of most of these tests. A membership carries
an e-mail address that the member can edit in their own Moodle profile,
so the import matches on ``user_id`` — the platform's own subject — and
treats a familiar-looking address as a reason to stop, never as proof.
"""

import uuid

import pytest

from app.config import settings
from app.models import (
    Course,
    CourseTeacher,
    IdentityProvider,
    LtiContext,
    User,
    UserIdentity,
    UserRole,
)
from app.services import lti_service

pytestmark = pytest.mark.integration

ISSUER = "https://moodle.test"
MEMBERSHIPS_URL = f"{ISSUER}/mod/lti/services.php/CourseSection/3/bindings/1/memberships"


@pytest.fixture(autouse=True)
def lti_on(monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", True)
    # Off unless a test says otherwise — the shipped default, and the
    # one that makes "Moodle says Instructor" not a role grant.
    monkeypatch.setattr(settings, "LTI_TRUST_INSTRUCTOR_ROLE", False)


# ----------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------
def _context(db, *, course=None, memberships_url=MEMBERSHIPS_URL):
    ctx = LtiContext(
        issuer=ISSUER,
        context_id=uuid.uuid4().hex,
        title="Wirtschaftsinformatik SE B 25",
        label="WWI25SEB",
        memberships_url=memberships_url,
        courseId=course.courseId if course else None,
    )
    db.add(ctx)
    db.commit()
    db.refresh(ctx)
    return ctx


def _member(sub, email, *, roles=("Learner",), status="Active", given="Test", family="Person"):
    return {
        "user_id": str(sub),
        "roles": list(roles),
        "status": status,
        "name": f"{given} {family}",
        "given_name": given,
        "family_name": family,
        "email": email,
    }


def _roster(monkeypatch, members):
    monkeypatch.setattr(lti_service, "fetch_context_members", lambda _ctx: list(members))


def _import(client, ctx, **body):
    return client.post(f"/lti/contexts/{ctx.ltiContextId}/import", json=body)


# ================================================================
# THE HAPPY PATH
# ================================================================
def test_a_moodle_course_becomes_a_studiengruppe_with_its_students(
    client, db, mock_user, monkeypatch
):
    ctx = _context(db)
    _roster(
        monkeypatch,
        [
            _member(101, "erst@dhbw.de", given="Erst"),
            _member(102, "zweit@dhbw.de", given="Zweit"),
        ],
    )

    resp = _import(client, ctx)

    assert resp.status_code == 201
    body = resp.json()
    assert body["courseName"] == "Wirtschaftsinformatik SE B 25"
    assert body["created"] == 2
    assert body["students"] == 2
    assert body["skipped"] == []

    course_id = uuid.UUID(body["courseId"])
    members = db.query(User).filter(User.courseId == course_id).all()
    assert {u.email for u in members} == {"erst@dhbw.de", "zweit@dhbw.de"}
    assert all(u.role == UserRole.STUDENT for u in members)


def test_the_new_students_get_an_lti_identity_not_just_an_account(
    client, db, monkeypatch
):
    """Without it the next launch would not recognise them and would
    fall into the link challenge against the account just created."""
    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "erst@dhbw.de")])

    _import(client, ctx)

    identity = (
        db.query(UserIdentity)
        .filter(
            UserIdentity.provider == IdentityProvider.LTI,
            UserIdentity.issuer == ISSUER,
            UserIdentity.subject == "101",
        )
        .first()
    )
    assert identity is not None
    assert identity.user.email == "erst@dhbw.de"


def test_the_context_is_mapped_onto_the_course_it_created(client, db, monkeypatch):
    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "erst@dhbw.de")])

    body = _import(client, ctx).json()

    db.refresh(ctx)
    assert ctx.courseId == uuid.UUID(body["courseId"])


def test_the_importing_teacher_teaches_the_new_course(client, db, mock_user, monkeypatch):
    """Otherwise they could not edit what they just created — the same
    reason ``POST /courses/`` adds the row."""
    ctx = _context(db)
    _roster(monkeypatch, [])

    body = _import(client, ctx).json()

    row = (
        db.query(CourseTeacher)
        .filter(
            CourseTeacher.courseId == uuid.UUID(body["courseId"]),
            CourseTeacher.userId == mock_user.userId,
        )
        .first()
    )
    assert row is not None


def test_the_importing_teacher_is_usually_in_the_roster_too(
    client, db, mock_user, monkeypatch
):
    """The normal case, not an edge one: whoever presses the button is
    an instructor of that Moodle course, so they arrive twice — once as
    the actor, once out of the member list. Two ``course_teachers`` rows
    would violate the primary key and lose the whole import."""
    monkeypatch.setattr(settings, "LTI_TRUST_INSTRUCTOR_ROLE", True)
    db.add(
        UserIdentity(
            userId=mock_user.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="500",
        )
    )
    db.commit()

    ctx = _context(db)
    _roster(monkeypatch, [_member(500, mock_user.email, roles=("Instructor",))])

    resp = _import(client, ctx)

    assert resp.status_code == 201
    body = resp.json()
    assert body["teachers"] == 1
    rows = (
        db.query(CourseTeacher)
        .filter(
            CourseTeacher.courseId == uuid.UUID(body["courseId"]),
            CourseTeacher.userId == mock_user.userId,
        )
        .all()
    )
    assert len(rows) == 1


def test_an_importing_admin_gets_no_course_teacher_row(
    admin_client, db, mock_admin, monkeypatch
):
    """Admin rights are role-shaped and already cover the course."""
    ctx = _context(db)
    _roster(monkeypatch, [])

    body = _import(admin_client, ctx).json()

    row = (
        db.query(CourseTeacher)
        .filter(CourseTeacher.courseId == uuid.UUID(body["courseId"]))
        .first()
    )
    assert row is None


def test_the_name_can_be_overridden(client, db, monkeypatch):
    ctx = _context(db)
    _roster(monkeypatch, [])

    body = _import(client, ctx, name="WI SE B 25").json()

    assert body["courseName"] == "WI SE B 25"


def test_a_context_without_a_title_falls_back_to_something_nameable(
    client, db, monkeypatch
):
    ctx = _context(db)
    ctx.title = None
    ctx.label = None
    db.commit()
    _roster(monkeypatch, [])

    body = _import(client, ctx).json()

    assert ctx.context_id in body["courseName"]


# ================================================================
# WHAT A MEMBER LIST MUST NOT TALK US INTO
# ================================================================
def test_a_taken_address_never_matches_an_existing_account(client, db, monkeypatch):
    """The address in a membership is an editable Moodle profile field.
    Matching on it would hand over that account's deployments and
    OpenStack credentials to whoever typed the address into Moodle."""
    victim = User(
        email="dozentin@dhbw.de",
        username="dozentin",
        role=UserRole.TEACHER,
    )
    db.add(victim)
    db.commit()
    db.refresh(victim)
    victim_id = victim.userId

    ctx = _context(db)
    _roster(monkeypatch, [_member(999, "dozentin@dhbw.de", roles=("Learner",))])

    body = _import(client, ctx).json()

    assert body["created"] == 0
    assert body["matched"] == 0
    assert [s["reason"] for s in body["skipped"]] == ["link_required"]

    # Untouched: same account, same role, still in no Studiengruppe.
    db.refresh(victim)
    assert victim.userId == victim_id
    assert victim.role == UserRole.TEACHER
    assert victim.courseId is None
    assert db.query(UserIdentity).count() == 0


def test_a_member_already_linked_by_subject_is_matched_not_recreated(
    client, db, monkeypatch
):
    known = User(email="bekannt@dhbw.de", username="bekannt", role=UserRole.STUDENT)
    db.add(known)
    db.flush()
    db.add(
        UserIdentity(
            userId=known.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="101",
        )
    )
    db.commit()

    ctx = _context(db)
    # A different address than the account holds — the subject decides.
    _roster(monkeypatch, [_member(101, "anders@dhbw.de")])

    body = _import(client, ctx).json()

    assert body["created"] == 0
    assert body["matched"] == 1
    db.refresh(known)
    assert known.email == "bekannt@dhbw.de"
    assert known.courseId == uuid.UUID(body["courseId"])


def test_an_existing_admin_is_not_downgraded_by_a_learner_role(
    client, db, monkeypatch
):
    boss = User(email="chef@dhbw.de", username="chef", role=UserRole.ADMIN)
    db.add(boss)
    db.flush()
    db.add(
        UserIdentity(
            userId=boss.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="101",
        )
    )
    db.commit()

    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "chef@dhbw.de", roles=("Learner",))])

    _import(client, ctx)

    db.refresh(boss)
    assert boss.role == UserRole.ADMIN


def test_a_student_already_in_another_group_stays_there(client, db, monkeypatch):
    other = Course(name="WI SE A 23")
    db.add(other)
    db.flush()
    settled = User(
        email="fest@dhbw.de",
        username="fest",
        role=UserRole.STUDENT,
        courseId=other.courseId,
    )
    db.add(settled)
    db.flush()
    db.add(
        UserIdentity(
            userId=settled.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="101",
        )
    )
    db.commit()
    other_id = other.courseId

    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "fest@dhbw.de")])

    body = _import(client, ctx).json()

    assert [s["reason"] for s in body["skipped"]] == ["already_in_another_group"]
    assert body["students"] == 0
    db.refresh(settled)
    assert settled.courseId == other_id


def test_a_member_without_an_address_is_skipped(client, db, monkeypatch):
    ctx = _context(db)
    _roster(monkeypatch, [_member(101, None)])

    body = _import(client, ctx).json()

    assert body["created"] == 0
    assert [s["reason"] for s in body["skipped"]] == ["no_email"]


def test_a_member_without_a_subject_is_skipped(client, db, monkeypatch):
    ctx = _context(db)
    _roster(monkeypatch, [_member("", "ohne@dhbw.de")])

    body = _import(client, ctx).json()

    assert [s["reason"] for s in body["skipped"]] == ["no_subject"]
    assert db.query(User).filter(User.email == "ohne@dhbw.de").first() is None


def test_inactive_members_are_ignored_entirely(client, db, monkeypatch):
    ctx = _context(db)
    _roster(
        monkeypatch,
        [
            _member(101, "weg@dhbw.de", status="Inactive"),
            _member(102, "da@dhbw.de"),
        ],
    )

    body = _import(client, ctx).json()

    assert body["created"] == 1
    assert body["skipped"] == []
    assert db.query(User).filter(User.email == "weg@dhbw.de").first() is None


def test_a_membership_without_a_status_counts_as_active(client, db, monkeypatch):
    """The field is optional in the specification. Treating its absence
    as inactive would import nobody from such a platform."""
    ctx = _context(db)
    member = _member(101, "da@dhbw.de")
    del member["status"]
    _roster(monkeypatch, [member])

    assert _import(client, ctx).json()["created"] == 1


# ================================================================
# INSTRUCTORS
# ================================================================
def test_an_instructor_is_not_made_a_teacher_when_the_role_is_not_trusted(
    client, db, monkeypatch
):
    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "trainer@dhbw.de", roles=("Instructor",))])

    body = _import(client, ctx).json()

    assert body["teachers"] == 0
    assert [s["reason"] for s in body["skipped"]] == ["instructor_not_trusted"]
    created = db.query(User).filter(User.email == "trainer@dhbw.de").first()
    assert created.role == UserRole.STUDENT
    assert (
        db.query(CourseTeacher).filter(CourseTeacher.userId == created.userId).first()
        is None
    )


def test_a_trusted_instructor_becomes_a_teacher_of_the_new_course(
    client, db, monkeypatch
):
    monkeypatch.setattr(settings, "LTI_TRUST_INSTRUCTOR_ROLE", True)
    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "trainer@dhbw.de", roles=("Instructor",))])

    body = _import(client, ctx).json()

    assert body["teachers"] == 1
    created = db.query(User).filter(User.email == "trainer@dhbw.de").first()
    assert created.role == UserRole.TEACHER
    assert created.courseId is None  # staff are not enrolled as members
    row = (
        db.query(CourseTeacher)
        .filter(
            CourseTeacher.courseId == uuid.UUID(body["courseId"]),
            CourseTeacher.userId == created.userId,
        )
        .first()
    )
    assert row is not None


def test_full_uri_roles_are_understood(client, db, monkeypatch):
    """The specification sends URIs, Moodle sends the bare suffix."""
    monkeypatch.setattr(settings, "LTI_TRUST_INSTRUCTOR_ROLE", True)
    ctx = _context(db)
    _roster(
        monkeypatch,
        [
            _member(
                101,
                "trainer@dhbw.de",
                roles=("http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",),
            )
        ],
    )

    assert _import(client, ctx).json()["teachers"] == 1


# ================================================================
# WHO MAY DO THIS
# ================================================================
def test_students_cannot_import_a_roster(student_client, db, monkeypatch):
    ctx = _context(db)
    _roster(monkeypatch, [_member(101, "erst@dhbw.de")])

    resp = _import(student_client, ctx)

    assert resp.status_code == 403
    assert db.query(Course).count() == 0


def test_an_unknown_context_is_a_404(client, monkeypatch):
    _roster(monkeypatch, [])

    resp = client.post(f"/lti/contexts/{uuid.uuid4()}/import", json={})

    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "lti_context_not_found"


def test_an_already_mapped_context_is_refused(client, db, monkeypatch):
    """A second Studiengruppe for the same Moodle course would split its
    members across two groups. Remapping is the PUT's job."""
    existing = Course(name="WI SE B 23")
    db.add(existing)
    db.commit()
    ctx = _context(db, course=existing)
    _roster(monkeypatch, [_member(101, "erst@dhbw.de")])

    resp = _import(client, ctx)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "lti_context_already_mapped"
    assert db.query(Course).count() == 1


def test_the_endpoint_is_off_when_lti_is(client, db, monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", False)
    ctx = _context(db)

    assert _import(client, ctx).status_code == 503


# ================================================================
# WHEN MOODLE CANNOT BE READ
# ================================================================
def test_a_context_without_a_memberships_url_says_so(client, db):
    """Recorded before the claim was stored, or the service is off in
    Moodle. Either way another launch is what fixes it."""
    ctx = _context(db, memberships_url=None)

    resp = _import(client, ctx)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "lti_nrps_unavailable"
    assert db.query(Course).count() == 0


def test_a_refused_member_list_leaves_nothing_behind(client, db, monkeypatch):
    def _boom(context):
        raise lti_service.LtiRosterError("lti_nrps_failed", "Moodle said no")

    monkeypatch.setattr(lti_service, "fetch_context_members", _boom)
    ctx = _context(db)

    resp = _import(client, ctx)

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "lti_nrps_failed"
    assert db.query(Course).count() == 0
    db.refresh(ctx)
    assert ctx.courseId is None


# ================================================================
# HOW THE OUTBOUND CALL FAILS
# ================================================================
# These drive ``fetch_context_members`` itself rather than the endpoint.
# No key material and no network: the tool configuration and the service
# class are both replaced, because what is under test is how a refusal
# from the platform is turned into something a lecturer can read.
class _StubConf:
    def __init__(self, registration=object()):
        self._registration = registration

    def find_registration_by_params(self, iss, client_id):
        return self._registration


def _service_raising(exc):
    class _Stub:
        def __init__(self, connector, data):
            pass

        def get_members(self):
            raise exc

    return _Stub


def test_a_platform_refusal_becomes_a_502_with_a_reason(db, monkeypatch):
    from pylti1p3.exception import LtiException

    monkeypatch.setattr(lti_service, "get_tool_conf", _StubConf)
    monkeypatch.setattr(lti_service, "ServiceConnector", lambda _reg: object())
    monkeypatch.setattr(
        lti_service,
        "NamesRolesProvisioningService",
        _service_raising(LtiException("403 Forbidden")),
    )
    ctx = _context(db)

    with pytest.raises(lti_service.LtiRosterError) as caught:
        lti_service.fetch_context_members(ctx)

    assert caught.value.code == "lti_nrps_failed"
    assert caught.value.status_code == 502


def test_an_unreachable_platform_is_told_apart_from_a_refusal(db, monkeypatch):
    """Different cause, different thing to go and check."""
    monkeypatch.setattr(lti_service, "get_tool_conf", _StubConf)
    monkeypatch.setattr(lti_service, "ServiceConnector", lambda _reg: object())
    monkeypatch.setattr(
        lti_service,
        "NamesRolesProvisioningService",
        _service_raising(ConnectionError("connection refused")),
    )
    ctx = _context(db)

    with pytest.raises(lti_service.LtiRosterError) as caught:
        lti_service.fetch_context_members(ctx)

    assert caught.value.code == "lti_nrps_unreachable"


def test_a_context_from_a_platform_we_no_longer_know_is_a_409(db, monkeypatch):
    from pylti1p3.exception import LtiException

    class _NoRegistration:
        def find_registration_by_params(self, iss, client_id):
            raise LtiException(f"iss {iss} not found")

    monkeypatch.setattr(lti_service, "get_tool_conf", _NoRegistration)
    ctx = _context(db)

    with pytest.raises(lti_service.LtiRosterError) as caught:
        lti_service.fetch_context_members(ctx)

    assert caught.value.code == "lti_unknown_platform"
    assert caught.value.status_code == 409
