"""Integration tests for crud.tasks, crud.users and crud.apps.

Covers branch coverage of filter conditionals, exclude_unset semantics,
not-found returns, and the soft-delete visibility rules. Each test
uses the session-scoped schema + per-test TRUNCATE provided by
``tests/conftest.py``.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.crud import apps as apps_crud
from app.crud import tasks as tasks_crud
from app.crud import users as users_crud
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Course,
    Deployment,
    TaskStatus,
    TaskType,
    User,
    UserRole,
)
from app.schemas import (
    AppUpdate,
    TaskUpdate,
    UserCreate,
    UserUpdate,
)


# ----------------------------------------------------------------
# Helpers — minimal viable parent rows for FK references.
# ----------------------------------------------------------------
def _make_user(
    db,
    *,
    email: str | None = None,
    username: str = "alice",
    role: UserRole = UserRole.STUDENT,
    course_id=None,
) -> User:
    user = User(
        email=email or f"{uuid.uuid4().hex}@example.com",
        username=username,
        role=role,
        courseId=course_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_course(db, name: str = "Cloud 101") -> Course:
    course = Course(name=name)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def _make_app(
    db,
    *,
    user_id,
    name: str = "Demo App",
    is_private: bool = False,
    deleted: bool = False,
) -> App:
    app_row = App(
        name=name,
        description="desc",
        git_link="https://example.com/repo.git",
        is_private=is_private,
        userId=user_id,
    )
    if deleted:
        app_row.deleted_at = datetime.utcnow()
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    return app_row


def _make_deployment(db, *, user_id, app_id) -> Deployment:
    dep = Deployment(name="dep-" + uuid.uuid4().hex[:6], userId=user_id, appId=app_id)
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return dep


def _make_approval(
    db,
    *,
    app_id,
    status: AppVersionApprovalStatus = AppVersionApprovalStatus.APPROVED,
    version_tag: str = "v1.0.0",
) -> AppVersionApproval:
    appr = AppVersionApproval(appId=app_id, version_tag=version_tag, status=status)
    db.add(appr)
    db.commit()
    db.refresh(appr)
    return appr


# ----------------------------------------------------------------
# crud.tasks
# ----------------------------------------------------------------
@pytest.mark.integration
def test_create_task_persists_fields(db):
    """create_task speichert die übergebenen Felder."""
    user = _make_user(db)
    app_row = _make_app(db, user_id=user.userId)
    dep = _make_deployment(db, user_id=user.userId, app_id=app_row.appId)

    payload = {
        "deploymentId": dep.deploymentId,
        "celeryTaskId": "celery-abc",
        "type": TaskType.DEPLOY,
        "status": TaskStatus.PENDING,
        "logs": "boot",
        "current_phase": "init",
        "progress_pct": 5,
    }
    created = tasks_crud.create_task(db, payload)

    assert created.taskId is not None
    assert created.deploymentId == dep.deploymentId
    assert created.celeryTaskId == "celery-abc"
    assert created.type == TaskType.DEPLOY
    assert created.status == TaskStatus.PENDING
    assert created.current_phase == "init"
    assert created.progress_pct == 5


@pytest.mark.integration
def test_get_task_returns_none_for_unknown_id(db):
    """get_task ohne Treffer liefert None."""
    assert tasks_crud.get_task(db, uuid.uuid4()) is None


@pytest.mark.integration
def test_get_tasks_filters_by_deployment_celery_and_status(db):
    """get_tasks filtert nach deployment_id, celery_task_id und status."""
    user = _make_user(db)
    app_row = _make_app(db, user_id=user.userId)
    dep_a = _make_deployment(db, user_id=user.userId, app_id=app_row.appId)
    dep_b = _make_deployment(db, user_id=user.userId, app_id=app_row.appId)

    t1 = tasks_crud.create_task(
        db,
        {
            "deploymentId": dep_a.deploymentId,
            "celeryTaskId": "cel-1",
            "type": TaskType.DEPLOY,
            "status": TaskStatus.PENDING,
        },
    )
    t2 = tasks_crud.create_task(
        db,
        {
            "deploymentId": dep_a.deploymentId,
            "celeryTaskId": "cel-2",
            "type": TaskType.UPDATE,
            "status": TaskStatus.RUNNING,
        },
    )
    t3 = tasks_crud.create_task(
        db,
        {
            "deploymentId": dep_b.deploymentId,
            "celeryTaskId": "cel-3",
            "type": TaskType.DESTROY,
            "status": TaskStatus.SUCCESS,
        },
    )

    # No filters — all three.
    assert len(tasks_crud.get_tasks(db)) == 3

    # deployment_id filter.
    by_dep = tasks_crud.get_tasks(db, deployment_id=dep_a.deploymentId)
    ids = {t.taskId for t in by_dep}
    assert ids == {t1.taskId, t2.taskId}

    # celery_task_id filter.
    by_celery = tasks_crud.get_tasks(db, celery_task_id="cel-3")
    assert [t.taskId for t in by_celery] == [t3.taskId]

    # status filter.
    by_status = tasks_crud.get_tasks(db, status=TaskStatus.RUNNING)
    assert [t.taskId for t in by_status] == [t2.taskId]


@pytest.mark.integration
def test_update_task_accepts_dict_payload(db):
    """update_task übernimmt ein dict 1:1."""
    user = _make_user(db)
    app_row = _make_app(db, user_id=user.userId)
    dep = _make_deployment(db, user_id=user.userId, app_id=app_row.appId)
    task = tasks_crud.create_task(
        db,
        {
            "deploymentId": dep.deploymentId,
            "celeryTaskId": "cel-x",
            "type": TaskType.DEPLOY,
            "status": TaskStatus.PENDING,
        },
    )

    updated = tasks_crud.update_task(
        db, task.taskId, {"status": TaskStatus.SUCCESS, "progress_pct": 100}
    )

    assert updated is not None
    assert updated.status == TaskStatus.SUCCESS
    assert updated.progress_pct == 100


@pytest.mark.integration
def test_update_task_accepts_pydantic_model(db):
    """update_task akzeptiert auch ein TaskUpdate-Modell mit exclude_unset."""
    user = _make_user(db)
    app_row = _make_app(db, user_id=user.userId)
    dep = _make_deployment(db, user_id=user.userId, app_id=app_row.appId)
    task = tasks_crud.create_task(
        db,
        {
            "deploymentId": dep.deploymentId,
            "celeryTaskId": "cel-y",
            "type": TaskType.DEPLOY,
            "status": TaskStatus.PENDING,
            "current_phase": "init",
        },
    )

    payload = TaskUpdate(status=TaskStatus.RUNNING, current_phase="TOFU_APPLY")
    updated = tasks_crud.update_task(db, task.taskId, payload)

    assert updated is not None
    assert updated.status == TaskStatus.RUNNING
    assert updated.current_phase == "TOFU_APPLY"
    # progress_pct was not set on the update -> exclude_unset keeps it.
    assert updated.progress_pct is None


@pytest.mark.integration
def test_update_task_returns_none_for_unknown_id(db):
    """update_task liefert None, wenn der Task nicht existiert."""
    assert tasks_crud.update_task(db, uuid.uuid4(), {"status": TaskStatus.FAILED}) is None


@pytest.mark.integration
def test_delete_task_true_then_false(db):
    """delete_task: True beim ersten Mal, False danach."""
    user = _make_user(db)
    app_row = _make_app(db, user_id=user.userId)
    dep = _make_deployment(db, user_id=user.userId, app_id=app_row.appId)
    task = tasks_crud.create_task(
        db,
        {
            "deploymentId": dep.deploymentId,
            "celeryTaskId": "cel-z",
            "type": TaskType.DEPLOY,
            "status": TaskStatus.PENDING,
        },
    )

    assert tasks_crud.delete_task(db, task.taskId) is True
    assert tasks_crud.delete_task(db, task.taskId) is False


# ----------------------------------------------------------------
# crud.users
# ----------------------------------------------------------------
@pytest.mark.integration
def test_create_user_generates_id(db):
    """create_user erzeugt eine User-Row mit ID."""
    payload = UserCreate(email="new@example.com", username="newbie", role=UserRole.STUDENT)
    created = users_crud.create_user(db, payload)

    assert created.userId is not None
    assert created.email == "new@example.com"
    assert created.username == "newbie"
    assert created.role == UserRole.STUDENT


@pytest.mark.integration
def test_get_user_hit_and_miss(db):
    """get_user trifft die Row und liefert sonst None."""
    user = _make_user(db, email="hit@example.com", username="hit")

    assert users_crud.get_user(db, user.userId).email == "hit@example.com"
    assert users_crud.get_user(db, uuid.uuid4()) is None


@pytest.mark.integration
def test_get_user_by_email_hit_and_miss(db):
    """get_user_by_email trifft per E-Mail und liefert sonst None."""
    _make_user(db, email="by-email@example.com", username="be")

    assert users_crud.get_user_by_email(db, "by-email@example.com") is not None
    assert users_crud.get_user_by_email(db, "missing@example.com") is None


@pytest.mark.integration
def test_get_user_by_username_hit_and_miss(db):
    """get_user_by_username trifft per Username und liefert sonst None."""
    _make_user(db, username="bobby")

    assert users_crud.get_user_by_username(db, "bobby") is not None
    assert users_crud.get_user_by_username(db, "ghost") is None


@pytest.mark.integration
def test_get_users_filters_by_role_and_course(db):
    """get_users filtert nach role und courseId."""
    course_a = _make_course(db, name="A")
    course_b = _make_course(db, name="B")
    _make_user(db, username="s_a", role=UserRole.STUDENT, course_id=course_a.courseId)
    _make_user(db, username="s_b", role=UserRole.STUDENT, course_id=course_b.courseId)
    _make_user(db, username="t_a", role=UserRole.TEACHER, course_id=course_a.courseId)
    _make_user(db, username="admin", role=UserRole.ADMIN)

    # No filters — all four.
    assert len(users_crud.get_users(db)) == 4

    # role filter.
    students = users_crud.get_users(db, role=UserRole.STUDENT)
    assert {u.username for u in students} == {"s_a", "s_b"}

    # course filter.
    in_course_a = users_crud.get_users(db, course_id=course_a.courseId)
    assert {u.username for u in in_course_a} == {"s_a", "t_a"}

    # combined filter.
    combined = users_crud.get_users(
        db, role=UserRole.STUDENT, course_id=course_a.courseId
    )
    assert [u.username for u in combined] == ["s_a"]


@pytest.mark.integration
def test_update_user_changes_role_and_clears_course(db):
    """update_user setzt role und kann courseId auf None setzen."""
    course = _make_course(db)
    user = _make_user(
        db, username="promote", role=UserRole.STUDENT, course_id=course.courseId
    )

    updated = users_crud.update_user(
        db, user.userId, UserUpdate(role=UserRole.TEACHER, courseId=None)
    )

    assert updated is not None
    assert updated.role == UserRole.TEACHER
    assert updated.courseId is None


@pytest.mark.integration
def test_update_user_returns_none_for_unknown_id(db):
    """update_user liefert None, wenn der User fehlt."""
    assert (
        users_crud.update_user(db, uuid.uuid4(), UserUpdate(role=UserRole.ADMIN))
        is None
    )


@pytest.mark.integration
def test_delete_user_true_then_false(db):
    """delete_user: True beim ersten Mal, False danach."""
    user = _make_user(db, username="goner")

    assert users_crud.delete_user(db, user.userId) is True
    assert users_crud.delete_user(db, user.userId) is False


@pytest.mark.integration
def test_search_users_matches_username_or_email_case_insensitive(db):
    """search_users matcht ilike auf username UND email."""
    _make_user(db, email="alice@example.com", username="Alice")
    _make_user(db, email="weird@bobland.io", username="Bob")
    _make_user(db, email="charlie@example.com", username="Charlie")

    # Case-insensitive match on username.
    res_user = users_crud.search_users(db, "ali")
    assert {u.username for u in res_user} == {"Alice"}

    # Case-insensitive match on email substring.
    res_email = users_crud.search_users(db, "BOBLAND")
    assert {u.username for u in res_email} == {"Bob"}

    # Substring that matches multiple via email domain.
    res_multi = users_crud.search_users(db, "example.com")
    assert {u.username for u in res_multi} == {"Alice", "Charlie"}


# ----------------------------------------------------------------
# crud.apps
# ----------------------------------------------------------------
@pytest.mark.integration
def test_get_app_honours_include_deleted(db):
    """get_app verbirgt soft-deleted Apps, außer include_deleted=True."""
    user = _make_user(db)
    app_row = _make_app(db, user_id=user.userId, deleted=True)

    assert apps_crud.get_app(db, app_row.appId) is None
    assert apps_crud.get_app(db, app_row.appId, include_deleted=True) is not None


@pytest.mark.integration
def test_get_apps_filters_user_and_include_deleted(db):
    """get_apps filtert nach user_id und respektiert include_deleted."""
    owner = _make_user(db, username="owner")
    other = _make_user(db, username="other")
    a1 = _make_app(db, user_id=owner.userId, name="own-live")
    a2 = _make_app(db, user_id=owner.userId, name="own-deleted", deleted=True)
    a3 = _make_app(db, user_id=other.userId, name="other-live")

    # Default — soft-deleted hidden.
    all_live = apps_crud.get_apps(db)
    assert {a.appId for a in all_live} == {a1.appId, a3.appId}

    # user_id filter.
    own_live = apps_crud.get_apps(db, user_id=owner.userId)
    assert [a.appId for a in own_live] == [a1.appId]

    # include_deleted=True with user filter returns both.
    own_all = apps_crud.get_apps(db, user_id=owner.userId, include_deleted=True)
    assert {a.appId for a in own_all} == {a1.appId, a2.appId}


@pytest.mark.integration
def test_get_visible_apps_owner_sees_own_private(db):
    """Owner sieht seine eigene private App auch ohne Approval."""
    owner = _make_user(db, username="vis-owner")
    own = _make_app(db, user_id=owner.userId, name="own-private", is_private=True)

    visible = apps_crud.get_visible_apps(db, requesting_user_id=owner.userId)
    assert [a.appId for a in visible] == [own.appId]


@pytest.mark.integration
def test_get_visible_apps_hides_foreign_private(db):
    """Andere Studenten sehen fremde private Apps nicht."""
    owner = _make_user(db, username="priv-owner")
    other = _make_user(db, username="priv-other")
    _make_app(db, user_id=owner.userId, name="foreign-priv", is_private=True)

    visible = apps_crud.get_visible_apps(db, requesting_user_id=other.userId)
    assert visible == []


@pytest.mark.integration
def test_get_visible_apps_shows_public_approved(db):
    """Public + approved App ist für andere Studenten sichtbar."""
    owner = _make_user(db, username="pub-owner")
    other = _make_user(db, username="pub-other")
    pub = _make_app(db, user_id=owner.userId, name="public-app", is_private=False)
    _make_approval(db, app_id=pub.appId, status=AppVersionApprovalStatus.APPROVED)

    visible = apps_crud.get_visible_apps(db, requesting_user_id=other.userId)
    assert [a.appId for a in visible] == [pub.appId]


@pytest.mark.integration
def test_get_visible_apps_hides_public_unapproved(db):
    """Public, aber ohne approved Approval -> für Andere unsichtbar."""
    owner = _make_user(db, username="pend-owner")
    other = _make_user(db, username="pend-other")
    pending_app = _make_app(
        db, user_id=owner.userId, name="pending-app", is_private=False
    )
    _make_approval(
        db, app_id=pending_app.appId, status=AppVersionApprovalStatus.PENDING
    )

    visible = apps_crud.get_visible_apps(db, requesting_user_id=other.userId)
    assert visible == []


@pytest.mark.integration
def test_get_visible_apps_hides_soft_deleted_even_for_owner(db):
    """Soft-deleted App ist auch für den Eigentümer unsichtbar."""
    owner = _make_user(db, username="del-owner")
    _make_app(db, user_id=owner.userId, name="dead-app", deleted=True)

    visible = apps_crud.get_visible_apps(db, requesting_user_id=owner.userId)
    assert visible == []


@pytest.mark.integration
def test_update_app_excludes_image_and_git_link(db):
    """update_app ignoriert image und git_link, ändert aber name/description."""
    owner = _make_user(db)
    app_row = _make_app(
        db, user_id=owner.userId, name="orig-name", is_private=False
    )
    original_git = app_row.git_link
    original_image = app_row.image

    payload = AppUpdate(
        name="renamed",
        description="new desc",
        image="data:image/png;base64,XXXX",
        is_private=True,
    )
    # AppUpdate has extra="ignore" + git_link is not a declared field. Set it
    # via model_dump bypass: passing git_link via __pydantic_extra__ would be
    # ignored, so we only need to verify name/description/is_private update
    # while image is excluded.
    updated = apps_crud.update_app(db, app_row.appId, payload)

    assert updated is not None
    assert updated.name == "renamed"
    assert updated.description == "new desc"
    assert updated.is_private is True
    # image must NOT be smuggled into the LargeBinary column.
    assert updated.image == original_image
    # git_link is excluded defensively.
    assert updated.git_link == original_git


@pytest.mark.integration
def test_update_app_returns_none_for_unknown_id(db):
    """update_app liefert None, wenn die App fehlt."""
    assert apps_crud.update_app(db, uuid.uuid4(), AppUpdate(name="x")) is None


@pytest.mark.integration
def test_set_app_image_writes_and_clears(db):
    """set_app_image schreibt bytes+mime und löscht sie mit (None, None)."""
    owner = _make_user(db)
    app_row = _make_app(db, user_id=owner.userId)

    written = apps_crud.set_app_image(db, app_row.appId, b"PNGDATA", "image/png")
    assert written is not None
    assert written.image == b"PNGDATA"
    assert written.image_mime == "image/png"

    cleared = apps_crud.set_app_image(db, app_row.appId, None, None)
    assert cleared is not None
    assert cleared.image is None
    assert cleared.image_mime is None


@pytest.mark.integration
def test_set_app_image_returns_none_for_unknown_id(db):
    """set_app_image liefert None, wenn die App fehlt."""
    assert apps_crud.set_app_image(db, uuid.uuid4(), b"x", "image/png") is None


@pytest.mark.integration
def test_soft_delete_app_sets_deleted_at_and_hides(db):
    """soft_delete_app setzt deleted_at; get_app liefert dann None."""
    owner = _make_user(db)
    app_row = _make_app(db, user_id=owner.userId)

    assert apps_crud.soft_delete_app(db, app_row.appId) is True
    assert apps_crud.get_app(db, app_row.appId) is None

    re_read = apps_crud.get_app(db, app_row.appId, include_deleted=True)
    assert re_read is not None
    assert re_read.deleted_at is not None


@pytest.mark.integration
def test_soft_delete_app_returns_false_for_unknown_id(db):
    """soft_delete_app liefert False, wenn die App nicht existiert."""
    assert apps_crud.soft_delete_app(db, uuid.uuid4()) is False
