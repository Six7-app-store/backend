"""Regressionstests für die Korrektheitsfehler aus Phase 1 des Refactor-Plans
(``REFACTOR_PLAN.md``, Befunde B-xx).

Jeder Test ist zuerst gegen den fehlerhaften Stand rot gelaufen.
"""
import pytest


# ================================================================
# B-01 · Ein Nicht-Semver-Tag darf die Versionsliste nicht zerstören
# ================================================================
def _git_service_with_tags(tags):
    from app.services.git_service import GitService

    svc = GitService.__new__(GitService)
    svc.token = "T"
    svc._parse_git_url = lambda _url: {
        "platform": "github", "host": "github.com", "owner": "o", "repo": "r",
    }
    svc._fetch_github_tags = lambda _parsed: [{"version": t} for t in tags]
    svc._fetch_github_releases = lambda _parsed: {}
    return svc


@pytest.mark.unit
def test_get_versions_keeps_every_tag_when_some_are_not_semver():
    """B-01: ``sort_key`` mischte ``(1, 2, 0)`` mit ``('latest',)``; der
    Vergleich ``int < str`` warf ``TypeError``, und ``GET /apps/{id}`` zeigte
    für die App danach gar keine Version mehr."""
    svc = _git_service_with_tags(["v1.2.0", "latest", "v1.3.0-rc1", "v1.10.0"])

    versions = [v["version"] for v in svc.get_versions("https://github.com/o/r")]

    assert sorted(versions) == sorted(["v1.2.0", "latest", "v1.3.0-rc1", "v1.10.0"])
    # Echte Versionen zuerst, absteigend und numerisch (1.10 > 1.2), der Rest danach.
    assert versions[:2] == ["v1.10.0", "v1.2.0"]


@pytest.mark.unit
def test_get_versions_sorts_plain_semver_descending():
    svc = _git_service_with_tags(["v1.0.0", "v2.0.0", "v1.5.3"])

    versions = [v["version"] for v in svc.get_versions("https://github.com/o/r")]

    assert versions == ["v2.0.0", "v1.5.3", "v1.0.0"]


# ================================================================
# B-02 · Das Ausgabe-Schema einer App darf kein DNS auflösen
# ================================================================
@pytest.mark.unit
def test_app_response_does_not_resolve_dns():
    """B-02: ``AppResponse`` erbte den ``git_link``-Validator von ``AppBase``.
    FastAPI validiert Antworten, also lief pro App ein blockierender
    ``gethostbyname`` im Request; löste ein Host später privat auf, scheiterte
    die ganze Liste mit 500."""
    import datetime as dt
    import uuid
    from unittest.mock import patch

    from app.schemas import AppResponse

    with patch("socket.gethostbyname", side_effect=AssertionError("DNS lookup")) as dns:
        for _ in range(3):
            AppResponse.model_validate({
                "appId": uuid.uuid4(),
                "userId": uuid.uuid4(),
                "name": "a",
                "git_link": "https://github.com/o/r",
                "created_at": dt.datetime.now(),
                "is_private": False,
            })

    dns.assert_not_called()


@pytest.mark.unit
def test_app_response_accepts_a_link_that_now_resolves_privately():
    import datetime as dt
    import uuid
    from unittest.mock import patch

    from app.schemas import AppResponse

    with patch("socket.gethostbyname", return_value="10.0.0.5"):
        out = AppResponse.model_validate({
            "appId": uuid.uuid4(),
            "userId": uuid.uuid4(),
            "name": "a",
            "git_link": "https://git.example.org/o/r",
            "created_at": dt.datetime.now(),
            "is_private": False,
        })

    assert out.git_link == "https://git.example.org/o/r"


@pytest.mark.unit
def test_app_create_still_rejects_private_hosts():
    """Gegenprobe: Die Prüfung gehört auf die Eingabe und bleibt dort."""
    from unittest.mock import patch

    from pydantic import ValidationError

    from app.schemas import AppCreate

    with patch("socket.gethostbyname", return_value="10.0.0.5"), pytest.raises(ValidationError):
        AppCreate(name="a", git_link="https://git.example.org/o/r")


# ================================================================
# B-03 · Ein Task ohne Celery-ID ist ein gültiger Zustand
# ================================================================
@pytest.mark.integration
def test_task_list_survives_a_task_without_celery_id(db):
    """B-03: ``Task.celeryTaskId`` ist nullable und bleibt bis zum Versand leer
    (``prepare_task_in_tx``), ``TaskResponse`` verlangte aber einen String. Die
    Task-Liste antwortete in diesem Zeitfenster mit 500."""
    import uuid

    from app.models import Task, TaskStatus, TaskType, UserRole
    from tests.test_phase0_security import _app, _deployment, _user, as_user

    owner = _user(db, UserRole.TEACHER)
    dep = _deployment(db, owner, _app(db, owner))
    db.add(Task(
        taskId=uuid.uuid4(),
        deploymentId=dep.deploymentId,
        celeryTaskId=None,
        type=TaskType.DEPLOY,
        status=TaskStatus.PENDING,
    ))
    db.commit()

    with as_user(owner) as c:
        r = c.get(f"/tasks/deployment/{dep.deploymentId}")

    assert r.status_code == 200, r.text
    assert r.json()[0]["celeryTaskId"] is None


# ================================================================
# B-04 · Der Statusfilter muss denselben Status meinen wie die Anzeige
# ================================================================
def _deployment_with_last_task(db, owner, task_type, task_status, *, created_at=None):
    import datetime as dt
    import uuid

    from app.models import Task
    from tests.test_phase0_security import _app, _deployment

    dep = _deployment(db, owner, _app(db, owner))
    db.add(Task(
        taskId=uuid.uuid4(),
        deploymentId=dep.deploymentId,
        celeryTaskId="c",
        type=task_type,
        status=task_status,
        created_at=created_at or dt.datetime(2026, 1, 1, 12, 0, 0),
    ))
    db.commit()
    return dep


@pytest.mark.integration
def test_status_filter_matches_the_displayed_status_for_every_task_state(db):
    """B-04: ``derive_status`` kennt ``paused``, ``pausing``, ``resuming``,
    ``pause_failed`` und ``resume_failed``, der Filter nicht. ``?status=paused``
    fand nichts, ``?status=success`` dafür auch pausierte Deployments.

    Geprüft wird jede Kombination aus Task-Typ und -Status gegen die Anzeige."""
    from app.crud import deployments as crud
    from app.models import TaskStatus, TaskType, UserRole
    from tests.test_phase0_security import _user

    owner = _user(db, UserRole.TEACHER)
    shown = {}
    for task_type in TaskType:
        for task_status in TaskStatus:
            dep = _deployment_with_last_task(db, owner, task_type, task_status)
            shown[dep.deploymentId] = crud.derive_status(task_status, task_type)

    for wanted in crud.DEPLOYMENT_STATUSES:
        found = {d.deploymentId for d in crud.get_deployments(db, status=wanted, limit=500)}
        expected = {i for i, s in shown.items() if s == wanted}
        assert found == expected, f"status={wanted}"

    # Jeder angezeigte Status ist auch filterbar.
    assert set(shown.values()) <= set(crud.DEPLOYMENT_STATUSES)


@pytest.mark.integration
def test_status_filter_rejects_an_unknown_status(db):
    from app.models import UserRole
    from tests.test_phase0_security import _user, as_user

    owner = _user(db, UserRole.TEACHER)

    with as_user(owner) as c:
        bad = c.get("/deployments/", params={"status_filter": "no-such-status"})
        good = c.get("/deployments/", params={"status_filter": "paused"})

    assert bad.status_code == 422
    assert "paused" in bad.text  # nennt die erlaubten Werte
    assert good.status_code == 200


# ================================================================
# B-05 · Listen brauchen eine stabile, sinnvolle Reihenfolge
# ================================================================
@pytest.mark.integration
def test_deployments_are_listed_newest_first(db):
    """B-05: Sortiert wurde nach ``deploymentId``, einer zufälligen UUID v4,
    also nach nichts Sinnvollem. Neueste zuerst, gemessen am ersten Task."""
    import datetime as dt

    from app.crud import deployments as crud
    from app.models import TaskStatus, TaskType, UserRole
    from tests.test_phase0_security import _user

    owner = _user(db, UserRole.TEACHER)
    created = {}
    for day in (3, 6, 1, 5, 2, 4):  # absichtlich nicht in zeitlicher Reihenfolge
        dep = _deployment_with_last_task(
            db, owner, TaskType.DEPLOY, TaskStatus.SUCCESS,
            created_at=dt.datetime(2026, 3, day, 12, 0, 0),
        )
        created[dep.deploymentId] = day

    listed = [created[d.deploymentId] for d in crud.get_deployments(db, limit=50)]

    assert listed == [6, 5, 4, 3, 2, 1]


@pytest.mark.integration
def test_deployment_pages_do_not_overlap_or_skip(db):
    import datetime as dt

    from app.crud import deployments as crud
    from app.models import TaskStatus, TaskType, UserRole
    from tests.test_phase0_security import _user

    owner = _user(db, UserRole.TEACHER)
    ids = [
        _deployment_with_last_task(
            db, owner, TaskType.DEPLOY, TaskStatus.SUCCESS,
            created_at=dt.datetime(2026, 3, day, 12, 0, 0),
        ).deploymentId
        for day in range(1, 8)
    ]

    pages = [
        [d.deploymentId for d in crud.get_deployments(db, skip=skip, limit=3)]
        for skip in (0, 3, 6)
    ]

    assert [i for page in pages for i in page] == list(reversed(ids))


@pytest.mark.integration
def test_other_lists_have_a_defined_order(db):
    """B-05: ``offset``/``limit`` ohne ``ORDER BY`` liefert in Postgres keine
    garantierte Reihenfolge; Seiten können sich überlappen oder Zeilen auslassen.
    Eingefügt wird hier in der Gegenrichtung der erwarteten Reihenfolge."""
    import datetime as dt
    import uuid

    from app.crud import apps as crud_apps
    from app.crud import courses as crud_courses
    from app.crud import tasks as crud_tasks
    from app.crud import teams as crud_teams
    from app.crud import users as crud_users
    from app.models import App, Course, Task, TaskStatus, TaskType, Team, User, UserRole
    from tests.test_phase0_security import _app, _deployment, _user

    owner = _user(db, UserRole.TEACHER)
    days = (5, 4, 3, 2, 1)  # eingefügt von neu nach alt

    # Apps und Nutzer: älteste zuerst
    for d in days:
        db.add(App(appId=uuid.uuid4(), name=f"app{d}", userId=owner.userId,
                   created_at=dt.datetime(2026, 1, d)))
        db.add(User(userId=uuid.uuid4(), email=f"o{d}@x.de", username=f"o{d}",
                    role=UserRole.STUDENT, created_at=dt.datetime(2026, 1, d)))
    db.commit()
    assert [a.name for a in crud_apps.get_apps(db)] == [f"app{d}" for d in sorted(days)]
    assert [a.name for a in crud_apps.get_visible_apps(db, owner.userId)] == [
        f"app{d}" for d in sorted(days)
    ]
    students = [u.username for u in crud_users.get_users(db, role=UserRole.STUDENT)]
    assert students == [f"o{d}" for d in sorted(days)]

    # Kurse und Teams: nach Name
    dep = _deployment(db, owner, _app(db, owner))
    for name in ("c", "b", "a"):
        db.add(Course(courseId=uuid.uuid4(), name=name))
        db.add(Team(teamId=uuid.uuid4(), name=name, deploymentId=dep.deploymentId))
    db.commit()
    assert [c.name for c in crud_courses.get_courses(db)] == ["a", "b", "c"]
    assert [t.name for t in crud_teams.get_teams(db, deployment_id=dep.deploymentId)] == [
        "a", "b", "c",
    ]

    # Tasks: nach Erstellung
    for d in days:
        db.add(Task(taskId=uuid.uuid4(), deploymentId=dep.deploymentId, celeryTaskId=f"t{d}",
                    type=TaskType.DEPLOY, status=TaskStatus.SUCCESS,
                    created_at=dt.datetime(2026, 2, d)))
    db.commit()
    assert [t.celeryTaskId for t in crud_tasks.get_tasks(db, deployment_id=dep.deploymentId)] == [
        f"t{d}" for d in sorted(days)
    ]
