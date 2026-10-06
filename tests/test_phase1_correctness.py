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


# ================================================================
# B-14 · ?scope=course respektiert die anderen Filter
# ================================================================
@pytest.mark.integration
def test_course_scope_listing_applies_the_status_filter(db):
    """B-14: ``status_filter`` wurde angenommen und im Kurs-Scope still
    ignoriert (ein Kommentar im Code nannte das Absicht)."""
    from app.models import TaskStatus, TaskType, UserRole
    from tests.test_phase0_security import _course, _teach, _user, as_user

    course, other_course = _course(db), _course(db)
    teacher = _user(db, UserRole.TEACHER)
    _teach(db, course, teacher)
    ok_student = _user(db, UserRole.STUDENT, course=course)
    bad_student = _user(db, UserRole.STUDENT, course=course)
    outsider = _user(db, UserRole.STUDENT, course=other_course)

    ok = _deployment_with_last_task(db, ok_student, TaskType.DEPLOY, TaskStatus.SUCCESS)
    bad = _deployment_with_last_task(db, bad_student, TaskType.DEPLOY, TaskStatus.FAILED)
    _deployment_with_last_task(db, outsider, TaskType.DEPLOY, TaskStatus.FAILED)

    with as_user(teacher) as c:
        failed = c.get("/deployments/", params={"scope": "course", "status_filter": "failed"})
        everything = c.get("/deployments/", params={"scope": "course"})
        one_student = c.get(
            "/deployments/", params={"scope": "course", "student": str(ok_student.userId)}
        )

    assert {d["deploymentId"] for d in failed.json()} == {str(bad.deploymentId)}
    assert {d["deploymentId"] for d in everything.json()} == {
        str(ok.deploymentId), str(bad.deploymentId),
    }
    assert {d["deploymentId"] for d in one_student.json()} == {str(ok.deploymentId)}


@pytest.mark.integration
def test_course_scope_listing_is_empty_for_a_teacher_without_courses(db):
    from app.models import TaskStatus, TaskType, UserRole
    from tests.test_phase0_security import _course, _user, as_user

    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT, course=_course(db))
    _deployment_with_last_task(db, student, TaskType.DEPLOY, TaskStatus.SUCCESS)

    with as_user(teacher) as c:
        r = c.get("/deployments/", params={"scope": "course"})

    assert r.status_code == 200
    assert r.json() == []


# ================================================================
# B-06 · Der Download-Header verträgt jeden Dateinamen
# ================================================================
def _deployment_with_upload(db, owner, filename):
    import base64
    import json

    from tests.test_phase0_security import _app, _deployment

    dep = _deployment(db, owner, _app(db, owner))
    payload = b"inhalt"
    dep.userInputVar = json.dumps({
        "terraform": {
            "task_file": {
                "all": {
                    "name": filename,
                    "content_b64": base64.b64encode(payload).decode(),
                    "size": len(payload),
                    "content_type": "text/plain",
                }
            }
        }
    })
    db.commit()
    return dep


@pytest.mark.integration
def test_download_with_umlaut_and_euro_sign_in_the_filename(db):
    """B-06: Der Dateiname ging roh in den Header. Starlette kodiert Header als
    Latin-1, ``€`` warf ``UnicodeEncodeError`` und der Download endete in 500."""
    from app.models import UserRole
    from tests.test_phase0_security import _user, as_user

    owner = _user(db, UserRole.TEACHER)
    dep = _deployment_with_upload(db, owner, "Übung €.txt")

    with as_user(owner) as c:
        r = c.get(f"/deployments/{dep.deploymentId}/files/task_file/all")

    assert r.status_code == 200, r.text
    assert r.content == b"inhalt"
    header = r.headers["content-disposition"]
    assert header.isascii()
    assert "filename*=UTF-8''%C3%9Cbung%20%E2%82%AC.txt" in header


@pytest.mark.integration
def test_download_filename_cannot_break_out_of_the_header(db):
    from app.models import UserRole
    from tests.test_phase0_security import _user, as_user

    owner = _user(db, UserRole.TEACHER)
    dep = _deployment_with_upload(db, owner, 'a"b\r\nX-Evil: 1.txt')

    with as_user(owner) as c:
        r = c.get(f"/deployments/{dep.deploymentId}/files/task_file/all")

    assert r.status_code == 200, r.text
    assert "x-evil" not in r.headers
    header = r.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header
    assert header.count('"') == 2  # nur die Anführungszeichen um den ASCII-Namen


@pytest.mark.integration
def test_download_with_a_plain_filename_keeps_working(db):
    from app.models import UserRole
    from tests.test_phase0_security import _user, as_user

    owner = _user(db, UserRole.TEACHER)
    dep = _deployment_with_upload(db, owner, "aufgabe.pdf")

    with as_user(owner) as c:
        r = c.get(f"/deployments/{dep.deploymentId}/files/task_file/all")

    assert r.status_code == 200
    assert 'filename="aufgabe.pdf"' in r.headers["content-disposition"]


# ================================================================
# B-07 · Die strukturierte Worker-Fehlermeldung wird unverändert gelesen
# ================================================================
def _worker_repr(payload):
    """So rendert der Worker die Ausnahme: ``Failure.__repr__`` ist
    ``Failure({args[0]!r})`` mit dem JSON-String als einzigem Argument."""
    import json

    return f"Failure({json.dumps(payload)!r})"


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        "Schlüssel fehlt",                     # B-07: ``unicode_escape`` machte "SchlÃ¼ssel"
        "Größe 5 € überschritten",
        "tofu said: can't read 'x') here",    # ``')`` brach die nicht-gierige Regex ab
        'quote " and backslash \\ and newline \n inside',
    ],
)
def test_structured_failure_survives_the_repr_round_trip(error):
    from app.services.celery_event_listener import _parse_structured_failure

    payload = {"error": error, "logs": [], "tf_state": None}

    parsed = _parse_structured_failure(_worker_repr(payload), "")

    assert parsed == payload


@pytest.mark.unit
def test_structured_failure_is_found_in_the_traceback_too():
    from app.services.celery_event_listener import _parse_structured_failure

    payload = {"error": "Größe", "logs": []}
    traceback = f"Traceback (most recent call last):\n  ...\n{_worker_repr(payload)}\n"

    assert _parse_structured_failure("", traceback) == payload


@pytest.mark.unit
def test_structured_failure_accepts_the_bare_json_form():
    from app.services.celery_event_listener import _parse_structured_failure

    traceback = 'celery.exceptions.Foo: Failure: {"error": "x", "logs": []}'

    assert _parse_structured_failure("", traceback) == {"error": "x", "logs": []}


@pytest.mark.unit
def test_structured_failure_returns_none_for_other_exceptions():
    from app.services.celery_event_listener import _parse_structured_failure

    assert _parse_structured_failure("WorkerLostError('x')", "boom") is None


# ================================================================
# B-10 · Der Event-Listener baut die Verbindung wieder auf
# ================================================================
class _StopListener(BaseException):
    """Beendet die Endlosschleife im Test (kein ``Exception``, wird also nicht gefangen)."""


@pytest.mark.unit
def test_event_listener_reconnects_with_growing_delay_after_a_crash(caplog):
    """B-10: ``capture`` lief in einem Daemon-Thread ohne Schleife und ohne
    Fehlerbehandlung. Brach die Verbindung zu RabbitMQ ab, starb der Thread
    still, und das Backend bekam bis zum Neustart keine Events mehr."""
    from unittest.mock import patch

    from app.services import celery_event_listener as listener

    calls = []

    def flaky_listen_once():
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError("broker connection lost")
        raise _StopListener

    sleeps = []
    with (
        patch.object(listener, "_listen_once", flaky_listen_once),
        patch.object(listener.time, "sleep", sleeps.append),
        pytest.raises(_StopListener),
    ):
        listener.start_event_listener()

    assert len(calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]
    crashes = [r for r in caplog.records if r.exc_info and "broker connection lost" in r.getMessage() + str(r.exc_info[1])]
    assert crashes, "der Absturz muss mit Traceback geloggt werden"


@pytest.mark.unit
def test_event_listener_also_reconnects_when_capture_returns():
    from unittest.mock import patch

    from app.services import celery_event_listener as listener

    outcomes = [None, None]

    def listen_once():
        if outcomes:
            return outcomes.pop()
        raise _StopListener

    sleeps = []
    with (
        patch.object(listener, "_listen_once", listen_once),
        patch.object(listener.time, "sleep", sleeps.append),
        pytest.raises(_StopListener),
    ):
        listener.start_event_listener()

    assert len(sleeps) == 2


@pytest.mark.unit
def test_event_listener_delay_is_capped():
    from unittest.mock import patch

    from app.services import celery_event_listener as listener

    calls = []

    def listen_once():
        calls.append(1)
        if len(calls) > 9:
            raise _StopListener
        raise RuntimeError("down")

    sleeps = []
    with (
        patch.object(listener, "_listen_once", listen_once),
        patch.object(listener.time, "sleep", sleeps.append),
        pytest.raises(_StopListener),
    ):
        listener.start_event_listener()

    assert max(sleeps) == 60.0
    assert sleeps[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


@pytest.mark.unit
def test_event_listener_delay_resets_after_a_healthy_connection():
    """Hielt eine Verbindung lange, ist der nächste Abbruch ein neuer Vorfall,
    kein weiterer Versuch derselben Störung."""
    from unittest.mock import patch

    from app.services import celery_event_listener as listener

    # Je Durchlauf zwei Zeitstempel: Start und Ende. Der dritte Lauf hält 180 s.
    clock = iter([0, 0.1, 10, 10.1, 20, 200, 210, 210.1, 300, 300.1])
    runs = []

    def listen_once():
        runs.append(1)
        if len(runs) > 4:
            raise _StopListener
        raise RuntimeError("down")

    sleeps = []
    with (
        patch.object(listener, "_listen_once", listen_once),
        patch.object(listener.time, "sleep", sleeps.append),
        patch.object(listener.time, "monotonic", lambda: next(clock)),
        pytest.raises(_StopListener),
    ):
        listener.start_event_listener()

    assert sleeps == [1.0, 2.0, 1.0, 2.0]


# ================================================================
# B-23 · Modell und Migrationen beschreiben dieselbe Datenbank
# ================================================================
@pytest.mark.unit
def test_models_declare_the_partial_indexes():
    """B-23: ``uq_tasks_active_per_deployment``, ``ix_deployments_live`` und
    ``ix_apps_live`` standen nur in Migrationen. Die Migration ``73fd123a60aa``
    hat sie per Autogenerate gelöscht, weil das Modell sie nicht kannte."""
    from app.models import App, Deployment, Task

    def indexes(model):
        return {i.name: i for i in model.__table__.indexes}

    active = indexes(Task)["uq_tasks_active_per_deployment"]
    assert active.unique
    assert [c.name for c in active.columns] == ["deploymentId"]
    assert "PENDING" in str(active.dialect_options["postgresql"]["where"])

    assert not indexes(Deployment)["ix_deployments_live"].unique
    assert not indexes(App)["ix_apps_live"].unique


@pytest.mark.integration
def test_a_second_active_task_is_rejected_by_the_database(db):
    """B-23: In der Praxis fehlte der Index, die Race-Absicherung in
    ``prepare_task_in_tx`` (``IntegrityError`` -> ``ActiveTaskExistsError``)
    war toter Code. Das Vorab-Lesen wird hier ausgeschaltet, als hätten zwei
    Anfragen es gleichzeitig bestanden."""
    import uuid
    from unittest.mock import patch

    from app.models import Task, TaskStatus, TaskType, UserRole
    from app.services import task_service
    from tests.test_phase0_security import _app, _deployment, _user

    owner = _user(db, UserRole.TEACHER)
    dep = _deployment(db, owner, _app(db, owner))
    db.add(Task(taskId=uuid.uuid4(), deploymentId=dep.deploymentId, celeryTaskId="a",
                type=TaskType.DEPLOY, status=TaskStatus.RUNNING))
    db.commit()

    with patch.object(task_service.crud_tasks, "get_tasks", return_value=[]), pytest.raises(
        task_service.ActiveTaskExistsError
    ):
        task_service.prepare_task_in_tx(db, dep.deploymentId, TaskType.PAUSE)


@pytest.mark.integration
def test_migrations_build_the_same_schema_as_the_models():
    """B-23: Die Testdatenbank entsteht per ``create_all``, nicht per Alembic;
    ein Fehler in den Migrationen fiel nie auf. Dieser Test fährt alle
    Migrationen auf eine leere Datenbank und lässt Alembic gegen die Modelle
    vergleichen (``alembic check``)."""
    import os
    import subprocess
    import sys
    import uuid
    from pathlib import Path

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from tests.conftest import _TEST_DB_URL

    name = f"alembic_check_{uuid.uuid4().hex[:8]}"
    base = make_url(_TEST_DB_URL)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.set(database=name).render_as_string(hide_password=False)
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root, env=env, capture_output=True, text=True, timeout=240,
        )

    try:
        upgrade = alembic("upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr[-2000:]

        engine = create_engine(url)
        with engine.connect() as conn:
            present = {r[0] for r in conn.execute(text(
                "select indexname from pg_indexes where indexname in "
                "('uq_tasks_active_per_deployment','ix_deployments_live','ix_apps_live')"
            ))}
        engine.dispose()
        assert present == {
            "uq_tasks_active_per_deployment", "ix_deployments_live", "ix_apps_live",
        }

        check = alembic("check")
        assert check.returncode == 0, check.stdout[-2000:] + check.stderr[-2000:]
        heads = alembic("heads")
        assert len(heads.stdout.strip().splitlines()) == 1, heads.stdout
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# ================================================================
# B-24 · Jeder Test läuft in einer CI-Spur
# ================================================================
def _collect(*marker_args):
    """Sammelt die gesamte Suite in einem eigenen Prozess und gibt die Node-IDs zurück.

    Ein eigener Prozess, weil ``request.session.items`` nur die Tests enthält, die
    der laufende Aufruf ohnehin ausgewählt hat; in CI mit ``-m unit`` wäre eine
    Prüfung dort immer grün."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        # ``addopts`` leer: das ``-v`` aus pyproject.toml lässt ``--collect-only``
        # sonst einen Baum statt Node-IDs ausgeben.
        [sys.executable, "-m", "pytest", "tests", "-o", "addopts=", "--collect-only", "-q",
         "-p", "no:cacheprovider", *marker_args],
        cwd=root, capture_output=True, text=True, timeout=240,
    )
    assert done.returncode in (0, 5), done.stdout[-1500:] + done.stderr[-1500:]
    return [line for line in done.stdout.splitlines() if "::" in line]


@pytest.mark.integration
def test_no_test_is_left_without_a_ci_lane():
    """B-24: CI führt ``-m unit`` und ``-m integration`` aus. 60 von 739 Tests
    trugen keine der beiden Marken und liefen dort nie, darunter die gesamte
    LTI-Launch-Suite. ``conftest.py`` setzt die fehlende Marke jetzt selbst."""
    orphans = _collect("-m", "not unit and not integration")

    assert orphans == [], f"{len(orphans)} Tests ohne Spur, z. B. {orphans[:3]}"


@pytest.mark.integration
def test_unmarked_tests_get_their_lane_from_the_directory():
    unit = _collect("-m", "unit")
    integration = _collect("-m", "integration")

    # ``tests/unit/test_email_service.py`` hatte als einzige Datei dort keine Marke.
    assert any(n.startswith("tests/unit/test_email_service.py") for n in unit)
    assert not any(n.startswith("tests/unit/") for n in integration)
    assert any(n.startswith("tests/test_lti_launch.py") for n in integration)


# ================================================================
# B-18 · Gelöschte Apps gehören nicht in die Admin-Warteschlange
# ================================================================
def _soft_deleted_app_with_pending_version(db, owner):
    import datetime as dt
    import uuid

    from app.models import AppVersionApproval, AppVersionApprovalStatus
    from tests.test_phase0_security import _app

    app = _app(db, owner)
    db.add(AppVersionApproval(
        approvalId=uuid.uuid4(), appId=app.appId, version_tag="v1",
        status=AppVersionApprovalStatus.PENDING,
    ))
    app.deleted_at = dt.datetime(2026, 1, 1)
    db.commit()
    return app


@pytest.mark.integration
def test_pending_queue_skips_deleted_apps(db):
    """B-18: ``get_pending_approvals`` filterte auf öffentliche Apps, nicht auf
    gelöschte; eine gelöschte App blieb mit ihrer offenen Version in der Liste."""
    from app.models import UserRole
    from tests.test_phase0_security import _app, _user, as_user

    admin = _user(db, UserRole.ADMIN)
    owner = _user(db, UserRole.TEACHER)
    _soft_deleted_app_with_pending_version(db, owner)
    live = _app(db, owner)
    _add_pending(db, live)

    with as_user(admin) as c:
        r = c.get("/admin/apps/versions/pending")

    assert r.status_code == 200
    assert [row["appId"] for row in r.json()] == [str(live.appId)]


def _add_pending(db, app):
    import uuid

    from app.models import AppVersionApproval, AppVersionApprovalStatus

    db.add(AppVersionApproval(
        approvalId=uuid.uuid4(), appId=app.appId, version_tag="v1",
        status=AppVersionApprovalStatus.PENDING,
    ))
    db.commit()


@pytest.mark.integration
def test_deactivating_a_deleted_app_is_a_404_not_a_500(db):
    """B-18: ``_require_app`` lädt auch gelöschte Apps, ``update_app`` nicht.
    Es kam ``None`` zurück, und die Antwortvalidierung machte daraus ein 500."""
    from app.models import UserRole
    from tests.test_phase0_security import _user, as_user

    admin = _user(db, UserRole.ADMIN)
    owner = _user(db, UserRole.TEACHER)
    app = _soft_deleted_app_with_pending_version(db, owner)

    with as_user(admin) as c:
        r = c.put(f"/admin/apps/{app.appId}")

    assert r.status_code == 404


# ================================================================
# B-17 · Ein Datenbankfehler beim Einreichen darf die übrigen Versionen nicht verschlucken
# ================================================================
@pytest.mark.integration
def test_submit_all_versions_continues_after_a_database_error(db):
    """B-17: ``with contextlib.suppress(Exception)`` um ``submit_version``. Nach
    einem echten DB-Fehler bleibt die Session im Rollback-Zustand; jede weitere
    Version scheiterte mit ``PendingRollbackError``, auch das wurde verschluckt.
    Der Nutzer sah 201, ein Teil der Versionen war nie eingereicht."""
    import uuid
    from unittest.mock import patch

    from app.models import AppVersionApproval, AppVersionApprovalStatus, UserRole
    from app.routers import apps as apps_router
    from tests.test_phase0_security import _user, as_user

    owner = _user(db, UserRole.TEACHER)
    real_submit = apps_router.crud_approvals.submit_version
    calls = []

    def flaky_submit(session, app_id, version_tag, **kwargs):
        calls.append(version_tag)
        if len(calls) == 1:
            # Ein echter Datenbankfehler mitten in der Session: zweimal derselbe
            # (appId, version_tag) verletzt ``uq_app_version_approval``.
            for _ in range(2):
                session.add(AppVersionApproval(
                    approvalId=uuid.uuid4(), appId=app_id, version_tag=version_tag,
                    status=AppVersionApprovalStatus.PENDING,
                ))
            session.commit()
        return real_submit(session, app_id=app_id, version_tag=version_tag, **kwargs)

    body = {
        "name": "mit-versionen",
        "git_link": "https://github.com/example/repo",
        "is_private": False,
        "submit_all_versions": True,
    }
    with (
        patch.object(apps_router.git_service, "verify_repository_access",
                     return_value={"success": True, "message": "ok"}),
        patch.object(apps_router.git_service, "get_versions",
                     return_value=[{"version": "v1"}, {"version": "v2"}, {"version": "v3"}]),
        patch.object(apps_router.crud_approvals, "submit_version", flaky_submit),
        as_user(owner) as c,
    ):
        r = c.post("/apps/", json=body)

    assert r.status_code == 201, r.text
    assert calls == ["v1", "v2", "v3"]
    db.expire_all()
    submitted = {
        a.version_tag
        for a in db.query(AppVersionApproval).filter(AppVersionApproval.appId == r.json()["appId"])
    }
    assert submitted == {"v2", "v3"}  # v1 scheiterte, die beiden anderen nicht


# ================================================================
# B-19 · Der Keystone-Timeout gilt für die eine Verbindung, nicht für den Prozess
# ================================================================
def _validator_payload():
    from app.models import OpenStackAuthType
    from app.schemas import OpenStackCredentialUpsert

    return OpenStackCredentialUpsert(
        auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
        auth_url="https://keystone.example/v3",
        identifier="id",
        secret="secret",
    )


@pytest.mark.unit
def test_validate_does_not_touch_the_process_wide_socket_timeout():
    """B-19: ``validate`` setzte ``socket.setdefaulttimeout(15)`` für den ganzen
    Prozess und stellte ihn danach wieder her. Zwei gleichzeitige Prüfungen
    (Sync-Endpunkte laufen im Thread-Pool) konnten 15 s dauerhaft festschreiben:
    A merkt sich ``None`` und setzt 15, B merkt sich 15, A stellt ``None`` her,
    B stellt 15 her."""
    import socket
    from unittest.mock import MagicMock, patch

    from app.services import openstack_validator

    seen = {}

    def fake_connect(**kwargs):
        seen["kwargs"] = kwargs
        seen["during"] = socket.getdefaulttimeout()
        return MagicMock()

    before = socket.getdefaulttimeout()
    with patch("app.services.openstack_validator.openstack.connect", fake_connect):
        ok, error = openstack_validator.validate(_validator_payload())

    assert (ok, error) == (True, None)
    assert seen["during"] == before
    assert socket.getdefaulttimeout() == before
    # Stattdessen bekommt genau diese Verbindung einen Timeout (keystoneauth-Session).
    assert seen["kwargs"]["api_timeout"] == 15


@pytest.mark.unit
def test_validate_reports_a_keystone_timeout_as_unreachable():
    from unittest.mock import patch

    from keystoneauth1 import exceptions as ksa_exc

    from app.services import openstack_validator

    def timing_out(**_kwargs):
        raise ksa_exc.ConnectTimeout("timed out")

    with patch("app.services.openstack_validator.openstack.connect", timing_out):
        ok, error = openstack_validator.validate(_validator_payload())

    assert ok is False
    assert error.startswith("Could not reach auth_url")


# ================================================================
# B-20 · Der OpenStack-Listen-Cache räumt auf und folgt den Zugangsdaten
# ================================================================
@pytest.fixture
def clean_openstack_cache():
    from app.services import openstack_client

    openstack_client._cache.clear()
    yield openstack_client
    openstack_client._cache.clear()


@pytest.mark.unit
def test_expired_cache_entries_are_dropped_on_the_next_write(clean_openstack_cache):
    """B-20: Abgelaufene Einträge wurden nie entfernt, nur ``invalidate_user``
    räumte auf. Der Cache wuchs mit Nutzer x Ressourcenart x Filter."""
    import uuid
    from unittest.mock import patch

    client = clean_openstack_cache
    user = uuid.uuid4()
    now = {"t": 1000.0}

    with patch.object(client.time, "monotonic", lambda: now["t"]):
        client.cached_list(user, "networks", None, lambda: [{"id": "n"}])
        client.cached_list(user, "flavors", None, lambda: [{"id": "f"}])
        assert len(client._cache) == 2

        now["t"] += client._TTL_SECONDS + 1  # beide abgelaufen
        client.cached_list(user, "images", None, lambda: [{"id": "i"}])

    assert [key[1] for key in client._cache] == ["images"]


@pytest.mark.unit
def test_a_valid_cache_entry_is_still_served_without_fetching(clean_openstack_cache):
    import uuid
    from unittest.mock import patch

    client = clean_openstack_cache
    user = uuid.uuid4()
    fetches = []

    def fetch():
        fetches.append(1)
        return [{"id": "n"}]

    with patch.object(client.time, "monotonic", lambda: 1000.0):
        client.cached_list(user, "networks", None, fetch)
        client.cached_list(user, "networks", None, fetch)

    assert len(fetches) == 1


@pytest.mark.integration
def test_changing_credentials_clears_the_users_cached_lists(db):
    """B-20: Wechselt ein Nutzer das OpenStack-Projekt, sah er bis zu 60 s lang
    Netzwerke, Flavors und Images des alten Projekts."""
    from unittest.mock import patch

    from app.models import UserRole
    from tests.test_phase0_security import _user, as_user

    owner = _user(db, UserRole.TEACHER)
    body = {
        "auth_type": "v3applicationcredential",
        "auth_url": "https://keystone.example/v3",
        "identifier": "id",
        "secret": "secret",
    }

    with (
        patch("app.routers.openstack_credentials.openstack_validator.validate",
              return_value=(True, None)),
        patch("app.routers.openstack_credentials.openstack_client.invalidate_user") as invalidate,
        as_user(owner) as c,
    ):
        put = c.put("/me/openstack-credentials", json=body)
        deleted = c.delete("/me/openstack-credentials")

    assert put.status_code == 200, put.text
    assert deleted.status_code == 204
    assert [call.args[0] for call in invalidate.call_args_list] == [owner.userId, owner.userId]


# ================================================================
# B-22 · Alle Tags laden, und gleichzeitige Klone behindern sich nicht
# ================================================================
class _Page:
    status_code = 200

    def __init__(self, items, next_url=None):
        self._items = items
        self.links = {"next": {"url": next_url}} if next_url else {}

    def json(self):
        return self._items

    def raise_for_status(self):
        return None


class _PagedSession:
    """Antwortet je URL mit einer Seite; merkt sich jeden Aufruf."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append({"url": url, "params": params, "headers": dict(headers or {})})
        return self.pages[url]


def _tag(name):
    return {"name": name, "commit": {"sha": "abcdef0123456789"}}


def _paged_service(pages):
    from app.services.git_service import GitService

    svc = GitService.__new__(GitService)
    svc.token = "T"
    svc._session = _PagedSession(pages)
    return svc


TAGS_URL = "https://api.github.com/repos/o/r/tags"
RELEASES_URL = "https://api.github.com/repos/o/r/releases"


@pytest.mark.unit
def test_get_versions_follows_the_next_page_links():
    """B-22: ``_request_tags`` las nur die erste Seite. GitHub liefert davon 30
    Tags, GitLab 20; bei mehr Tags fehlten Versionen in der App."""
    svc = _paged_service({
        TAGS_URL: _Page([_tag("v1"), _tag("v2")], next_url=TAGS_URL + "?page=2"),
        TAGS_URL + "?page=2": _Page([_tag("v3")]),
        RELEASES_URL: _Page([]),
    })

    versions = [v["version"] for v in svc.get_versions("https://github.com/o/r")]

    assert sorted(versions) == ["v1", "v2", "v3"]
    first = svc._session.calls[0]
    assert first["params"] == {"per_page": 100}  # so wenige Seiten wie möglich


@pytest.mark.unit
def test_pagination_never_follows_a_link_to_another_host():
    """Der Header trägt das Plattform-Token; ein ``Link`` auf einen fremden Host
    darf es nicht bekommen."""
    evil = "https://evil.example/steal"
    svc = _paged_service({
        TAGS_URL: _Page([_tag("v1")], next_url=evil),
        RELEASES_URL: _Page([]),
    })

    versions = [v["version"] for v in svc.get_versions("https://github.com/o/r")]

    assert versions == ["v1"]
    assert all(call["url"] != evil for call in svc._session.calls)


@pytest.mark.unit
def test_pagination_stops_after_a_fixed_number_of_pages():
    from app.services import git_service as git_module

    pages = {TAGS_URL: _Page([_tag("v0")], next_url=TAGS_URL + "?page=1"), RELEASES_URL: _Page([])}
    for n in range(1, 100):
        pages[f"{TAGS_URL}?page={n}"] = _Page([_tag(f"v{n}")], next_url=f"{TAGS_URL}?page={n + 1}")
    svc = _paged_service(pages)

    versions = svc.get_versions("https://github.com/o/r")

    assert len(versions) == git_module._MAX_PAGES


@pytest.mark.unit
def test_variable_scans_of_the_same_app_version_use_separate_clone_directories():
    """B-22: Der Klon-Pfad hing nur an App und Version. ``clone_release_vars``
    löscht ihn zu Beginn; zwei gleichzeitige Anfragen räumten sich so gegenseitig
    das Verzeichnis weg (sporadisch 500, danach übersprang die Validierung)."""
    import types
    import uuid
    from unittest.mock import patch

    from fastapi import HTTPException

    from app.services import hcl_variable_parser

    app = types.SimpleNamespace(appId=uuid.uuid4(), git_link="https://github.com/o/r")
    used = []

    def fake_clone(_url, _tag, directory_id):
        used.append(directory_id)
        raise RuntimeError("stop here")

    with patch.object(hcl_variable_parser.git_service, "clone_release_vars", fake_clone):
        for _ in range(2):
            with pytest.raises(HTTPException):
                hcl_variable_parser.load_variable_definitions(app, "v1")

    assert len(used) == 2 and used[0] != used[1]
