"""Regressionstests für die Sicherheits- und Berechtigungsfehler aus Phase 0
des Refactor-Plans (``REFACTOR_PLAN.md``).

Jeder Test hier ist zuerst gegen den fehlerhaften Stand rot gelaufen. Die
Kommentare nennen die Plan-ID (A-01 … A-07), damit man den Befund nachlesen kann.
"""
import json
import logging
import uuid
from contextlib import contextmanager

import pytest

from app.main import app as fastapi_app
from app.models import (
    App,
    Course,
    CourseTeacher,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    User,
    UserRole,
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


