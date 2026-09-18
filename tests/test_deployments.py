"""Tests for the deployments list endpoint — focus on backend#52.

Covers:
  * Response shape (status + created_at derived from tasks)
  * Window-function status filter (no row loss across pagination)
  * Synthetic ``destroying`` status mapping
  * No-N+1 regression guard (query counter)
  * Owner/member visibility smoke check

Reuses the standard ``client`` / ``db`` / ``mock_user`` fixtures from
``tests/conftest.py`` — those create the schema and yield a TEACHER
mock user authenticated against the FastAPI app.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.crud import deployments as crud_deployments
from app.database import get_db
from app.main import app as fastapi_app
from app.models import (
    App,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    Team,
    User,
    UserRole,
    UserToTeam,
)
from app.utils.auth import get_current_user
from tests.conftest import TestingSessionLocal, engine


def _make_app(db, user_id):
    a = App(
        appId=uuid.uuid4(),
        name=f"App {uuid.uuid4().hex[:6]}",
        userId=user_id,
        git_link="https://example.com/repo.git",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _make_deployment(db, user_id, app_id, name="dep"):
    d = Deployment(
        deploymentId=uuid.uuid4(),
        name=name,
        userId=user_id,
        appId=app_id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _make_task(db, deployment_id, *, type_, status, created_at):
    t = Task(
        taskId=uuid.uuid4(),
        deploymentId=deployment_id,
        type=type_,
        status=status,
        created_at=created_at,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------------------
# Response shape — status + created_at come from tasks
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_deployments_returns_status_and_created_at(client, db, mock_user):
    """Latest task drives ``status``, first task drives ``created_at``.

    A destroy-in-flight on top of a successful deploy should surface as
    ``"destroying"`` (synthetic value from ``derive_status``), and the
    creation time must come from the OLDEST task — not the most recent
    one.
    """
    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId, name="dep-status")
    first_at = datetime(2026, 1, 1, 12, 0, 0)
    second_at = datetime(2026, 6, 1, 12, 0, 0)
    _make_task(
        db,
        deployment.deploymentId,
        type_=TaskType.DEPLOY,
        status=TaskStatus.SUCCESS,
        created_at=first_at,
    )
    _make_task(
        db,
        deployment.deploymentId,
        type_=TaskType.DESTROY,
        status=TaskStatus.RUNNING,
        created_at=second_at,
    )

    response = client.get("/deployments/")
    assert response.status_code == 200
    body = response.json()
    match = next(
        d for d in body if d["deploymentId"] == str(deployment.deploymentId)
    )
    assert match["status"] == "destroying"
    assert match["created_at"].startswith("2026-01-01")


@pytest.mark.integration
def test_list_deployments_status_none_when_no_tasks(client, db, mock_user):
    """Deployments with no task rows must still serialize cleanly."""
    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId, name="dep-empty")

    response = client.get("/deployments/")
    assert response.status_code == 200
    body = response.json()
    match = next(
        d for d in body if d["deploymentId"] == str(deployment.deploymentId)
    )
    assert match["status"] is None
    assert match["created_at"] is None


# ---------------------------------------------------------------------------
# Status filter — page size correctness
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_deployments_status_filter_does_not_lose_rows(client, db, mock_user):
    """``?status_filter=success&limit=2`` must return exactly 2 rows when
    at least 2 successful deploys exist, even if other deployments were
    in front of them in the unfiltered order.

    The old post-filter loop applied ``LIMIT`` first and then dropped
    rows that didn't match — pages could come back short or empty.
    """
    app = _make_app(db, mock_user.userId)
    base = datetime(2026, 1, 1, 0, 0, 0)
    success_ids = []
    # 3 successful deploys + 7 running deploys, mixed order. The
    # window-function filter must look across the whole table to find
    # the success ones, regardless of pagination.
    for i in range(10):
        d = _make_deployment(db, mock_user.userId, app.appId, name=f"dep-{i}")
        is_success = i in (2, 5, 8)
        _make_task(
            db,
            d.deploymentId,
            type_=TaskType.DEPLOY,
            status=TaskStatus.SUCCESS if is_success else TaskStatus.RUNNING,
            created_at=base + timedelta(minutes=i),
        )
        if is_success:
            success_ids.append(str(d.deploymentId))

    response = client.get("/deployments/?status_filter=success&limit=2")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    for item in body:
        assert item["status"] == "success"
        assert item["deploymentId"] in success_ids


@pytest.mark.integration
def test_list_deployments_destroying_filter(client, db, mock_user):
    """``?status_filter=destroying`` matches deploys with a destroy task
    in ``pending``/``running`` — the synthetic value never lives in the
    DB, so the filter must reproduce the ``derive_status`` mapping."""
    app = _make_app(db, mock_user.userId)
    base = datetime(2026, 1, 1, 0, 0, 0)
    destroying = _make_deployment(db, mock_user.userId, app.appId, name="destroying")
    plain_running = _make_deployment(db, mock_user.userId, app.appId, name="running")
    destroyed = _make_deployment(db, mock_user.userId, app.appId, name="destroyed")

    _make_task(
        db,
        destroying.deploymentId,
        type_=TaskType.DESTROY,
        status=TaskStatus.RUNNING,
        created_at=base,
    )
    _make_task(
        db,
        plain_running.deploymentId,
        type_=TaskType.DEPLOY,
        status=TaskStatus.RUNNING,
        created_at=base,
    )
    _make_task(
        db,
        destroyed.deploymentId,
        type_=TaskType.DESTROY,
        status=TaskStatus.SUCCESS,
        created_at=base,
    )

    response = client.get("/deployments/?status_filter=destroying")
    assert response.status_code == 200
    body = response.json()
    assert {d["deploymentId"] for d in body} == {str(destroying.deploymentId)}


@pytest.mark.integration
def test_list_deployments_running_filter_excludes_destroying(
    client, db, mock_user
):
    """A destroy-in-flight task has raw ``status='running'`` but its
    effective status is ``'destroying'``. ``?status_filter=running``
    must therefore NOT return it — that would contradict what
    ``derive_status`` shows on the same row."""
    app = _make_app(db, mock_user.userId)
    base = datetime(2026, 1, 1, 0, 0, 0)
    deploy_running = _make_deployment(db, mock_user.userId, app.appId, name="d-run")
    destroy_running = _make_deployment(db, mock_user.userId, app.appId, name="dx-run")

    _make_task(
        db,
        deploy_running.deploymentId,
        type_=TaskType.DEPLOY,
        status=TaskStatus.RUNNING,
        created_at=base,
    )
    _make_task(
        db,
        destroy_running.deploymentId,
        type_=TaskType.DESTROY,
        status=TaskStatus.RUNNING,
        created_at=base,
    )

    response = client.get("/deployments/?status_filter=running")
    assert response.status_code == 200
    body = response.json()
    ids = {d["deploymentId"] for d in body}
    assert ids == {str(deploy_running.deploymentId)}


# ---------------------------------------------------------------------------
# N+1 regression guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_deployments_query_count_does_not_scale_with_rows(
    client, db, mock_user
):
    """Counts SQL statements during the list call. With the bulk-fetch
    in place the list endpoint should fire a small constant number of
    queries regardless of page size; the previous implementation needed
    1 + 2N (one initial query + a latest-task and a first-task query
    per row).

    We assert the count is well below the old 1 + 2*10 = 21, which is
    enough headroom for the auth/user-row queries that conftest issues
    while still catching a regression that re-introduces per-row task
    fetches.
    """
    app = _make_app(db, mock_user.userId)
    base = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(10):
        d = _make_deployment(db, mock_user.userId, app.appId, name=f"dep-{i}")
        _make_task(
            db,
            d.deploymentId,
            type_=TaskType.DEPLOY,
            status=TaskStatus.SUCCESS,
            created_at=base + timedelta(minutes=i),
        )

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        response = client.get("/deployments/")
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    # Cap is generous: the request itself should issue ~3-6 queries
    # (auth user lookup + deployments list + 2 window-function task
    # queries). Anything close to 21 means N+1 came back.
    deployment_or_task_queries = [
        s for s in statements if "deployment" in s.lower() or "task" in s.lower()
    ]
    assert len(deployment_or_task_queries) <= 8, (
        f"too many deployment/task queries (regression?): "
        f"{len(deployment_or_task_queries)}\n"
        + "\n---\n".join(deployment_or_task_queries)
    )


# ---------------------------------------------------------------------------
# Visibility — owner vs team member smoke check
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_deployments_student_sees_team_member_deployments(db, mock_user):
    """A STUDENT who is a member of a team on someone else's deployment
    must see that deployment via the list endpoint — the OR-filter in
    ``get_deployments`` survived the rewrite."""
    # Fresh student that is NOT the owner of the deployment.
    student = User(
        userId=uuid.uuid4(),
        keycloak_id="other-student",
        email="member@dhbw.de",
        username="memberstudent",
        firstName="Member",
        lastName="Student",
        role=UserRole.STUDENT,
    )
    db.add(student)
    db.commit()

    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId, name="team-dep")
    team = Team(
        teamId=uuid.uuid4(),
        name="Team A",
        deploymentId=deployment.deploymentId,
    )
    db.add(team)
    db.commit()
    db.add(UserToTeam(teamId=team.teamId, userId=student.userId))
    db.commit()

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: student
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as student_client:
            response = student_client.get("/deployments/")
        assert response.status_code == 200
        body = response.json()
        assert any(
            d["deploymentId"] == str(deployment.deploymentId) for d in body
        ), "team member should see the deployment they are picked into"
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Create — role gate
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_create_deployment_rejects_student(db, mock_user):
    """A student must not be able to create a deployment.

    Students are handed an environment a teacher set up for them. The
    gate sits before the app lookup, so the 403 must arrive even for an
    appId that does not exist — otherwise the endpoint would leak which
    apps are present.
    """
    student = User(
        userId=uuid.uuid4(),
        keycloak_id="create-student",
        email="creator@dhbw.de",
        username="createstudent",
        firstName="Create",
        lastName="Student",
        role=UserRole.STUDENT,
    )
    db.add(student)
    db.commit()

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: student
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as student_client:
            response = student_client.post(
                "/deployments/",
                json={
                    "name": "student-attempt",
                    "appId": str(uuid.uuid4()),
                    "releaseTag": "main",
                },
            )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "role_required"
        assert set(detail["required"]) == {"teacher", "admin"}
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pure helper — derive_status
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_status", "task_type", "expected"),
    [
        (None, None, None),
        (TaskStatus.PENDING, TaskType.DEPLOY, "pending"),
        (TaskStatus.RUNNING, TaskType.DEPLOY, "running"),
        (TaskStatus.SUCCESS, TaskType.DEPLOY, "success"),
        (TaskStatus.FAILED, TaskType.DEPLOY, "failed"),
        (TaskStatus.PENDING, TaskType.DESTROY, "destroying"),
        (TaskStatus.RUNNING, TaskType.DESTROY, "destroying"),
        (TaskStatus.SUCCESS, TaskType.DESTROY, "destroyed"),
        (TaskStatus.FAILED, TaskType.DESTROY, "failed"),
        (TaskStatus.CANCELLED, TaskType.DESTROY, "cancelled"),
    ],
)
def test_derive_status_mapping(task_status, task_type, expected):
    assert crud_deployments.derive_status(task_status, task_type) == expected
