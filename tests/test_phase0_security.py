"""Regressionstests für Sicherheits- und Berechtigungsfehler im Backend
(Befunde A-01 … A-07 einer Durchsicht).

Jeder Test hier ist zuerst gegen den fehlerhaften Stand rot gelaufen. Die
Abschnittsüberschriften nennen das Kürzel des Befunds; was falsch war und warum,
steht im Docstring des jeweiligen Tests.
"""
import contextlib
import json
import logging
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.main import app as fastapi_app
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Course,
    CourseTeacher,
    Deployment,
    OpenStackAuthType,
    Task,
    TaskStatus,
    TaskType,
    Team,
    User,
    UserOpenStackCredential,
    UserRole,
    UserToTeam,
)
from tests.conftest import _make_client

SECRET = "GEHEIMES-PASSWORT-123"


# ----------------------------------------------------------------
# Helfer
# ----------------------------------------------------------------
@contextmanager
def as_user(user):
    """Ersetzt ``get_current_user`` durch ``user`` und räumt danach auf."""
    try:
        yield _make_client(user)
    finally:
        fastapi_app.dependency_overrides.clear()


def _user(db, role=UserRole.STUDENT, *, course=None, keycloak_id=None, email=None):
    suffix = uuid.uuid4().hex[:8]
    u = User(
        userId=uuid.uuid4(),
        email=email or f"u-{suffix}@example.com",
        username=f"u-{suffix}",
        role=role,
        courseId=course.courseId if course else None,
        keycloak_id=keycloak_id,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _course(db, name="Kurs"):
    c = Course(courseId=uuid.uuid4(), name=f"{name}-{uuid.uuid4().hex[:4]}")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _teach(db, course, teacher):
    db.add(CourseTeacher(courseId=course.courseId, userId=teacher.userId))
    db.commit()


def _app(db, owner, *, is_private=False):
    a = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:6]}",
        userId=owner.userId,
        git_link="https://github.com/example/repo.git",
        is_private=is_private,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _deployment(db, owner, app):
    d = Deployment(
        deploymentId=uuid.uuid4(), name="dep", userId=owner.userId, appId=app.appId
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _task_with_secret(db, deployment):
    t = Task(
        taskId=uuid.uuid4(),
        deploymentId=deployment.deploymentId,
        celeryTaskId="c-1",
        type=TaskType.DEPLOY,
        status=TaskStatus.SUCCESS,
        tf_state=json.dumps({"pw": SECRET}),
        outputs=json.dumps({"admin_password": SECRET}),
        logs="log",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ================================================================
# A-04 · Zustandsdaten dürfen nicht ins Log
# ================================================================
@pytest.mark.unit
def test_failed_task_does_not_log_state_or_outputs(caplog):
    """A-04: ``update_data`` enthält ``tf_state``/``outputs`` (Passwörter) und
    wurde bisher komplett auf INFO geloggt."""
    from app.services.celery_event_listener import _handle_task_failed

    payload = {
        "error": "apply failed",
        "logs": [],
        "tf_state": json.dumps({"pw": SECRET}),
        "tofu_outputs": {"admin_password": SECRET},
    }
    event = {"exception": f"Failure('{json.dumps(payload)}')", "traceback": ""}

    with caplog.at_level(logging.DEBUG):
        _handle_task_failed("celery-id-1", event)

    assert SECRET not in caplog.text


# ================================================================
# A-03 · /tasks folgt demselben Rechtemodell wie /deployments
# ================================================================
@pytest.mark.integration
def test_tasks_denied_to_teacher_without_course_link(db):
    """A-03: Eine Lehrkraft ohne Kursbezug zum Besitzer sah über /tasks
    ``tf_state`` und ``outputs`` jedes Deployments."""
    owner = _user(db, UserRole.STUDENT)
    teacher = _user(db, UserRole.TEACHER)
    dep = _deployment(db, owner, _app(db, owner))
    task = _task_with_secret(db, dep)

    with as_user(teacher) as c:
        by_deployment = c.get(f"/tasks/deployment/{dep.deploymentId}")
        by_id = c.get(f"/tasks/{task.taskId}")

    assert by_deployment.status_code == 403
    assert by_id.status_code == 403
    assert SECRET not in by_deployment.text + by_id.text


@pytest.mark.integration
def test_tasks_allowed_to_course_teacher_of_owner(db):
    course = _course(db)
    owner = _user(db, UserRole.STUDENT, course=course)
    teacher = _user(db, UserRole.TEACHER)
    _teach(db, course, teacher)
    dep = _deployment(db, owner, _app(db, owner))
    task = _task_with_secret(db, dep)

    with as_user(teacher) as c:
        assert c.get(f"/tasks/deployment/{dep.deploymentId}").status_code == 200
        assert c.get(f"/tasks/{task.taskId}").status_code == 200


@pytest.mark.integration
def test_tasks_allowed_to_admin(db):
    owner = _user(db, UserRole.STUDENT)
    admin = _user(db, UserRole.ADMIN)
    dep = _deployment(db, owner, _app(db, owner))
    task = _task_with_secret(db, dep)

    with as_user(admin) as c:
        assert c.get(f"/tasks/deployment/{dep.deploymentId}").status_code == 200
        assert c.get(f"/tasks/{task.taskId}").status_code == 200


# ================================================================
# A-02 · Kursmitglieder verwalten nur Kurs-Lehrkräfte und Admins
# ================================================================
@pytest.mark.integration
def test_add_course_members_denied_to_unassigned_teacher(db):
    """A-02: Jede Lehrkraft durfte beliebige Nutzer in beliebige Kurse schieben.
    Die Kurszugehörigkeit steuert, wer Deployments einsehen darf."""
    course = _course(db)
    other = _course(db)
    outsider = _user(db, UserRole.TEACHER)
    _teach(db, other, outsider)  # Lehrkraft, aber von einem anderen Kurs
    victim = _user(db, UserRole.STUDENT)

    with as_user(outsider) as c:
        r = c.post(f"/courses/{course.courseId}/users", json={"userIds": [str(victim.userId)]})

    assert r.status_code == 403
    db.expire_all()
    assert db.get(User, victim.userId).courseId is None


@pytest.mark.integration
def test_remove_course_member_denied_to_unassigned_teacher(db):
    course = _course(db)
    outsider = _user(db, UserRole.TEACHER)
    member = _user(db, UserRole.STUDENT, course=course)

    with as_user(outsider) as c:
        r = c.delete(f"/courses/{course.courseId}/users/{member.userId}")

    assert r.status_code == 403
    db.expire_all()
    assert db.get(User, member.userId).courseId == course.courseId


@pytest.mark.integration
def test_course_members_can_be_managed_by_admin(db):
    course = _course(db)
    admin = _user(db, UserRole.ADMIN)
    student = _user(db, UserRole.STUDENT)

    with as_user(admin) as c:
        add = c.post(f"/courses/{course.courseId}/users", json={"userIds": [str(student.userId)]})
        rem = c.delete(f"/courses/{course.courseId}/users/{student.userId}")

    assert add.status_code == 200
    assert rem.status_code == 204


# ================================================================
# A-06 · Die Nutzersuche darf Rollen nicht ändern und nicht abbrechen
# ================================================================
@pytest.mark.unit
def test_sync_without_roles_keeps_existing_role(db):
    """A-06: Ein Keycloak-Suchtreffer trägt keine Rollen. ``sync`` mappte das
    auf ``student`` und stufte Lehrkräfte und Admins herab."""
    from app.utils.keycloak_auth import sync_user_from_keycloak

    teacher = _user(db, UserRole.TEACHER, keycloak_id="kc-teacher")
    search_hit = {
        "id": "kc-teacher",
        "username": teacher.username,
        "email": teacher.email,
        "firstName": "T",
        "lastName": "X",
        "enabled": True,
    }

    sync_user_from_keycloak(db, search_hit)

    db.expire_all()
    assert db.get(User, teacher.userId).role == UserRole.TEACHER


@pytest.mark.unit
def test_sync_with_roles_still_updates_role(db):
    """Gegenprobe: Mit Rollen im Token gilt weiter Keycloak als Quelle."""
    from app.utils.keycloak_auth import sync_user_from_keycloak

    user = _user(db, UserRole.STUDENT, keycloak_id="kc-up")
    sync_user_from_keycloak(
        db,
        {"id": "kc-up", "username": user.username, "email": user.email, "roles": ["teacher"]},
    )

    db.expire_all()
    assert db.get(User, user.userId).role == UserRole.TEACHER


@pytest.mark.integration
def test_user_search_does_not_demote_and_survives_unverified_email(db):
    """A-06: Ein Treffer, dessen E-Mail lokal schon existiert (z. B. LTI-Konto
    ohne ``keycloak_id``), ließ die ganze Suche mit 403 scheitern."""
    searcher = _user(db, UserRole.TEACHER)
    known = _user(db, UserRole.TEACHER, keycloak_id="kc-known")
    lti_only = _user(db, UserRole.STUDENT, email="lti-only@example.com")  # ohne keycloak_id

    def hit(kid, user):
        return {"id": kid, "username": user.username, "email": user.email,
                "firstName": "A", "lastName": "B", "enabled": True}

    hits = [hit("kc-known", known), hit("kc-lti", lti_only)]
    with (
        patch("app.routers.users.search_keycloak_users", return_value=hits),
        as_user(searcher) as c,
    ):
        r = c.get("/users/search", params={"query": "ab"})

    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(User, known.userId).role == UserRole.TEACHER


# ================================================================
# A-07 · /teams prüft den Zugriff auf das zugehörige Deployment
# ================================================================
def _team(db, deployment, name="T1", members=()):
    t = Team(teamId=uuid.uuid4(), name=name, deploymentId=deployment.deploymentId)
    db.add(t)
    db.commit()
    for m in members:
        db.add(UserToTeam(userId=m.userId, teamId=t.teamId))
    db.commit()
    db.refresh(t)
    return t


@pytest.mark.integration
def test_team_detail_denied_to_unrelated_student(db):
    """A-07: ``get_team`` ignorierte ``current_user``: jeder angemeldete Nutzer
    sah jedes Team samt Deployment-ID."""
    owner = _user(db, UserRole.TEACHER)
    dep = _deployment(db, owner, _app(db, owner))
    team = _team(db, dep)
    stranger = _user(db, UserRole.STUDENT)

    with as_user(stranger) as c:
        assert c.get(f"/teams/{team.teamId}").status_code == 403


@pytest.mark.integration
def test_team_detail_allowed_to_team_member(db):
    owner = _user(db, UserRole.TEACHER)
    dep = _deployment(db, owner, _app(db, owner))
    member = _user(db, UserRole.STUDENT)
    team = _team(db, dep, members=[member])

    with as_user(member) as c:
        assert c.get(f"/teams/{team.teamId}").status_code == 200


@pytest.mark.integration
def test_team_list_is_scoped_for_students(db):
    owner = _user(db, UserRole.TEACHER)
    dep_a = _deployment(db, owner, _app(db, owner))
    dep_b = _deployment(db, owner, _app(db, owner))
    member = _user(db, UserRole.STUDENT)
    mine = _team(db, dep_a, "mine", members=[member])
    _team(db, dep_b, "foreign")

    with as_user(member) as c:
        unfiltered = c.get("/teams/")
        foreign = c.get("/teams/", params={"deployment_id": str(dep_b.deploymentId)})
        own = c.get("/teams/", params={"deployment_id": str(dep_a.deploymentId)})

    assert unfiltered.status_code == 200
    assert {t["teamId"] for t in unfiltered.json()} == {str(mine.teamId)}
    assert foreign.status_code == 403
    assert own.status_code == 200


# ================================================================
# A-05 · Beim Deployen muss die gewählte Version freigegeben sein
# ================================================================
def _credentials(db, user):
    from app.utils import crypto

    db.add(
        UserOpenStackCredential(
            credentialId=uuid.uuid4(),
            userId=user.userId,
            auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
            auth_url="https://keystone.example/v3",
            encrypted_identifier=crypto.encrypt("id"),
            encrypted_secret=crypto.encrypt("secret"),
        )
    )
    db.commit()


def _approval(db, app, tag, status):
    db.add(
        AppVersionApproval(
            approvalId=uuid.uuid4(), appId=app.appId, version_tag=tag, status=status
        )
    )
    db.commit()


@pytest.fixture
def celery_stub():
    class _R:
        id = "fake-celery-id"

    with patch("app.services.task_service.celery_app.send_task", return_value=_R()) as m:
        yield m


def _deploy(client, app, tag):
    body = {"name": "d", "appId": str(app.appId), "userInputVar": {}, "teams": []}
    if tag is not None:
        body["releaseTag"] = tag
    return client.post("/deployments/", json=body)


@pytest.mark.integration
@pytest.mark.parametrize("tag", ["v2-pending", "v3-rejected", "v9-unbekannt", None])
def test_non_owner_cannot_deploy_unapproved_version(db, celery_stub, tag):
    """A-05: ``has_approved_version`` existierte, wurde aber nie aufgerufen.
    Geprüft wurde nur, ob die App *irgendeine* freigegebene Version hat."""
    owner = _user(db, UserRole.TEACHER)
    teacher = _user(db, UserRole.TEACHER)
    _credentials(db, teacher)
    app = _app(db, owner)
    _approval(db, app, "v1", AppVersionApprovalStatus.APPROVED)
    _approval(db, app, "v2-pending", AppVersionApprovalStatus.PENDING)
    _approval(db, app, "v3-rejected", AppVersionApprovalStatus.REJECTED)

    with as_user(teacher) as c:
        r = _deploy(c, app, tag)

    assert r.status_code == 403, r.text
    celery_stub.assert_not_called()


@pytest.mark.integration
def test_non_owner_can_deploy_approved_version(db, celery_stub):
    owner = _user(db, UserRole.TEACHER)
    teacher = _user(db, UserRole.TEACHER)
    _credentials(db, teacher)
    app = _app(db, owner)
    _approval(db, app, "v1", AppVersionApprovalStatus.APPROVED)

    with as_user(teacher) as c:
        r = _deploy(c, app, "v1")

    assert r.status_code == 201, r.text


@pytest.mark.integration
def test_owner_may_still_deploy_any_version(db, celery_stub):
    """Offen im Plan (Frage 3): Besitzer bleiben vorerst unverändert."""
    owner = _user(db, UserRole.TEACHER)
    _credentials(db, owner)
    app = _app(db, owner)

    with as_user(owner) as c:
        r = _deploy(c, app, "dev-branch")

    assert r.status_code == 201, r.text


# ================================================================
# A-01 · Das Plattform-Token geht nur an erlaubte Git-Hosts
# ================================================================
class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append((url, dict(headers or {})))

        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return []

        return _R()


def _service():
    from app.services.git_service import GitService

    svc = GitService.__new__(GitService)
    svc.token = "PLATFORM-TOKEN"
    svc._session = _FakeSession()
    return svc


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.attacker-example.invalid/o/r",
        "https://evilgithub.com/o/r",
        "https://github.com.evil.example/o/r",
        "git@gitlab.evil.example:o/r.git",
    ],
)
def test_token_is_never_sent_to_unlisted_host(url):
    """A-01: Ein Host galt als GitLab, sobald „gitlab" im Namen stand, und
    bekam das Plattform-Token im Header ``PRIVATE-TOKEN``."""
    svc = _service()

    result = svc.verify_repository_access(url)
    with contextlib.suppress(Exception):
        svc.get_versions(url)

    assert result["success"] is False
    assert svc._session.calls == []


@pytest.mark.unit
def test_token_is_sent_to_allowed_gitlab_host():
    svc = _service()

    svc.verify_repository_access("https://gitlab.com/owner/repo.git")

    (url, headers), = svc._session.calls
    assert url.startswith("https://gitlab.com/api/v4/")
    assert headers == {"PRIVATE-TOKEN": "PLATFORM-TOKEN"}


@pytest.mark.unit
def test_configured_self_hosted_gitlab_is_allowed():
    svc = _service()

    with patch("app.services.git_service.settings.GIT_ALLOWED_HOSTS", ["gitlab.dhbw.example"]):
        svc.verify_repository_access("https://gitlab.dhbw.example/owner/repo.git")

    (url, headers), = svc._session.calls
    assert url.startswith("https://gitlab.dhbw.example/api/v4/")


@pytest.mark.unit
def test_clone_url_carries_no_token_for_unlisted_host():
    svc = _service()

    with pytest.raises(ValueError):
        svc._get_authenticated_url("https://gitlab.attacker-example.invalid/o/r.git")
