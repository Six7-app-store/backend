"""Integration tests for ``app.crud.teams`` und ``app.crud.courses``.

Beide CRUD-Module hängen ausschließlich an einer SQLAlchemy-Session
und enthalten genug Zweige (FK-NULL-Detach, exclude_unset,
duplicate-guard auf der Join-Tabelle), dass eine direkte Session-
basierte Testsuite die effizienteste Coverage bringt — schneller und
deutlich aussagekräftiger als ein Umweg über die HTTP-API.
"""
from __future__ import annotations

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud import courses as courses_crud
from app.crud import teams as teams_crud
from app.models import App, Course, Deployment, Team, User, UserRole, UserToTeam
from app.schemas import CourseCreate, CourseUpdate, TeamCreate, TeamUpdate


# ----------------------------------------------------------------
# Helpers — minimale Parent-Rows für FK-Pflichten.
# ----------------------------------------------------------------
def _make_user(db, *, username: str | None = None, role: UserRole = UserRole.STUDENT) -> User:
    """Lege einen User mit eindeutiger E-Mail an und committe."""
    uname = username or f"u-{uuid.uuid4().hex[:8]}"
    user = User(
        userId=uuid4(),
        email=f"{uname}@example.test",
        username=uname,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_course(db, *, name: str = "Course") -> Course:
    course = Course(courseId=uuid4(), name=name)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def _make_app(db, *, owner: User) -> App:
    app_row = App(
        appId=uuid4(),
        name=f"app-{uuid.uuid4().hex[:6]}",
        userId=owner.userId,
        is_private=False,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    return app_row


def _make_deployment(db) -> Deployment:
    """Vollständige Parent-Kette: User -> App -> Deployment."""
    owner = _make_user(db, role=UserRole.TEACHER)
    app_row = _make_app(db, owner=owner)
    deployment = Deployment(
        deploymentId=uuid4(),
        name=f"dep-{uuid.uuid4().hex[:6]}",
        userId=owner.userId,
        appId=app_row.appId,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


# ================================================================
# TEAMS
# ================================================================
@pytest.mark.integration
def test_create_team_with_user_ids_persists_team_and_memberships(db):
    """``create_team`` muss Team-Row + alle UserToTeam-Memberships schreiben."""
    deployment = _make_deployment(db)
    u1 = _make_user(db)
    u2 = _make_user(db)

    payload = TeamCreate(
        name="Alpha",
        deploymentId=deployment.deploymentId,
        userIds=[u1.userId, u2.userId],
    )
    team = teams_crud.create_team(db, payload)

    assert team.teamId is not None
    assert team.name == "Alpha"
    assert team.deploymentId == deployment.deploymentId

    memberships = (
        db.query(UserToTeam).filter(UserToTeam.teamId == team.teamId).all()
    )
    assert {m.userId for m in memberships} == {u1.userId, u2.userId}


@pytest.mark.integration
def test_create_team_rolls_back_when_a_member_does_not_exist(db):
    deployment = _make_deployment(db)
    payload = TeamCreate(name="Incomplete", deploymentId=deployment.deploymentId, userIds=[uuid4()])
    with pytest.raises(IntegrityError):
        teams_crud.create_team(db, payload)
    db.rollback()
    assert db.query(Team).filter_by(name="Incomplete").count() == 0


@pytest.mark.integration
def test_create_team_without_users_creates_empty_team(db):
    """``userIds=[]`` darf keine Membership-Rows erzeugen."""
    deployment = _make_deployment(db)
    payload = TeamCreate(
        name="Solo", deploymentId=deployment.deploymentId, userIds=[]
    )
    team = teams_crud.create_team(db, payload)

    rows = db.query(UserToTeam).filter(UserToTeam.teamId == team.teamId).count()
    assert rows == 0


@pytest.mark.integration
def test_get_team_returns_row_when_found(db):
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="Hit", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()

    found = teams_crud.get_team(db, team.teamId)
    assert found is not None
    assert found.teamId == team.teamId


@pytest.mark.integration
def test_get_team_returns_none_when_missing(db):
    assert teams_crud.get_team(db, uuid4()) is None


@pytest.mark.integration
def test_get_teams_without_filter_returns_all(db):
    d1 = _make_deployment(db)
    d2 = _make_deployment(db)
    db.add_all([
        Team(teamId=uuid4(), name="T1", deploymentId=d1.deploymentId),
        Team(teamId=uuid4(), name="T2", deploymentId=d2.deploymentId),
    ])
    db.commit()

    result = teams_crud.get_teams(db)
    assert len(result) >= 2


@pytest.mark.integration
def test_get_teams_filters_by_deployment_id(db):
    d1 = _make_deployment(db)
    d2 = _make_deployment(db)
    db.add_all([
        Team(teamId=uuid4(), name="T1a", deploymentId=d1.deploymentId),
        Team(teamId=uuid4(), name="T1b", deploymentId=d1.deploymentId),
        Team(teamId=uuid4(), name="T2", deploymentId=d2.deploymentId),
    ])
    db.commit()

    result = teams_crud.get_teams(db, deployment_id=d1.deploymentId)
    assert len(result) == 2
    assert all(t.deploymentId == d1.deploymentId for t in result)


@pytest.mark.integration
def test_update_team_partial_payload_only_changes_supplied_fields(db):
    """``exclude_unset`` darf unveränderte Felder nicht überschreiben."""
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="Original", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()
    db.refresh(team)
    original_deployment_id = team.deploymentId

    updated = teams_crud.update_team(db, team.teamId, TeamUpdate(name="Renamed"))

    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.deploymentId == original_deployment_id


@pytest.mark.integration
def test_update_team_returns_none_for_unknown_id(db):
    assert teams_crud.update_team(db, uuid4(), TeamUpdate(name="x")) is None


@pytest.mark.integration
def test_delete_team_returns_true_when_present(db):
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="Doomed", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()

    assert teams_crud.delete_team(db, team.teamId) is True
    assert db.query(Team).filter(Team.teamId == team.teamId).first() is None


@pytest.mark.integration
def test_delete_team_returns_false_for_unknown_id(db):
    assert teams_crud.delete_team(db, uuid4()) is False


@pytest.mark.integration
def test_add_user_to_team_returns_true_on_first_add(db):
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="T", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()
    user = _make_user(db)

    assert teams_crud.add_user_to_team(db, team.teamId, user.userId) is True

    row = (
        db.query(UserToTeam)
        .filter(UserToTeam.teamId == team.teamId, UserToTeam.userId == user.userId)
        .first()
    )
    assert row is not None


@pytest.mark.integration
def test_add_user_to_team_returns_false_on_duplicate(db):
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="T", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()
    user = _make_user(db)

    teams_crud.add_user_to_team(db, team.teamId, user.userId)
    assert teams_crud.add_user_to_team(db, team.teamId, user.userId) is False


@pytest.mark.integration
def test_remove_user_from_team_returns_true_when_present(db):
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="T", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()
    user = _make_user(db)
    db.add(UserToTeam(userId=user.userId, teamId=team.teamId))
    db.commit()

    assert teams_crud.remove_user_from_team(db, team.teamId, user.userId) is True
    remaining = (
        db.query(UserToTeam)
        .filter(UserToTeam.teamId == team.teamId, UserToTeam.userId == user.userId)
        .first()
    )
    assert remaining is None


@pytest.mark.integration
def test_remove_user_from_team_returns_false_when_absent(db):
    deployment = _make_deployment(db)
    team = Team(teamId=uuid4(), name="T", deploymentId=deployment.deploymentId)
    db.add(team)
    db.commit()
    user = _make_user(db)

    assert teams_crud.remove_user_from_team(db, team.teamId, user.userId) is False


@pytest.mark.integration
def test_create_teams_for_deployment_creates_all_teams_and_memberships(db):
    """Bulk-Helper muss zwei Teams + alle Memberships in einer Operation aufbauen."""
    deployment = _make_deployment(db)
    u1 = _make_user(db)
    u2 = _make_user(db)
    u3 = _make_user(db)

    teams_data = [
        {"name": "Bulk-A", "userIds": [u1.userId, u2.userId]},
        {"name": "Bulk-B", "userIds": [u3.userId]},
    ]
    created = teams_crud.create_teams_for_deployment(
        db, deployment.deploymentId, teams_data
    )
    db.commit()

    assert len(created) == 2
    names = {t.name for t in created}
    assert names == {"Bulk-A", "Bulk-B"}
    assert all(t.deploymentId == deployment.deploymentId for t in created)

    persisted = (
        db.query(Team).filter(Team.deploymentId == deployment.deploymentId).all()
    )
    assert {t.name for t in persisted} == {"Bulk-A", "Bulk-B"}

    by_name = {t.name: t for t in persisted}
    members_a = (
        db.query(UserToTeam).filter(UserToTeam.teamId == by_name["Bulk-A"].teamId).all()
    )
    members_b = (
        db.query(UserToTeam).filter(UserToTeam.teamId == by_name["Bulk-B"].teamId).all()
    )
    assert {m.userId for m in members_a} == {u1.userId, u2.userId}
    assert {m.userId for m in members_b} == {u3.userId}


# ================================================================
# COURSES
# ================================================================
@pytest.mark.integration
def test_create_course_then_get_course_round_trip(db):
    created = courses_crud.create_course(db, CourseCreate(name="Algorithmen 1"))
    assert created.courseId is not None
    assert created.name == "Algorithmen 1"

    fetched = courses_crud.get_course(db, created.courseId)
    assert fetched is not None
    assert fetched.courseId == created.courseId


@pytest.mark.integration
def test_get_course_returns_none_for_unknown_id(db):
    assert courses_crud.get_course(db, uuid4()) is None


@pytest.mark.integration
def test_get_courses_returns_list_of_courses(db):
    courses_crud.create_course(db, CourseCreate(name="K1"))
    courses_crud.create_course(db, CourseCreate(name="K2"))

    listed = courses_crud.get_courses(db)
    assert len(listed) >= 2
    assert {c.name for c in listed} >= {"K1", "K2"}


@pytest.mark.integration
def test_update_course_partial_payload_changes_only_supplied_fields(db):
    course = _make_course(db, name="Original")
    updated = courses_crud.update_course(
        db, course.courseId, CourseUpdate(name="Neu")
    )
    assert updated is not None
    assert updated.name == "Neu"
    assert updated.courseId == course.courseId


@pytest.mark.integration
def test_update_course_returns_none_for_unknown_id(db):
    assert courses_crud.update_course(db, uuid4(), CourseUpdate(name="x")) is None


@pytest.mark.integration
def test_delete_course_detaches_enrolled_users_then_deletes(db):
    course = _make_course(db, name="ToDelete")
    u1 = _make_user(db)
    u2 = _make_user(db)
    u1.courseId = course.courseId
    u2.courseId = course.courseId
    db.commit()

    assert courses_crud.delete_course(db, course.courseId) is True

    assert courses_crud.get_course(db, course.courseId) is None
    db.refresh(u1)
    db.refresh(u2)
    assert u1.courseId is None
    assert u2.courseId is None


@pytest.mark.integration
def test_delete_course_returns_false_for_unknown_id(db):
    assert courses_crud.delete_course(db, uuid4()) is False


@pytest.mark.integration
def test_get_course_members_returns_users_sorted_by_username(db):
    course = _make_course(db)
    u_b = _make_user(db, username="bravo")
    u_a = _make_user(db, username="alpha")
    u_c = _make_user(db, username="charlie")
    for u in (u_a, u_b, u_c):
        u.courseId = course.courseId
    db.commit()

    members = courses_crud.get_course_members(db, course.courseId)
    assert [m.username for m in members] == ["alpha", "bravo", "charlie"]


@pytest.mark.integration
def test_get_course_members_empty_for_course_without_users(db):
    course = _make_course(db)
    assert courses_crud.get_course_members(db, course.courseId) == []


@pytest.mark.integration
def test_add_users_to_course_with_empty_list_short_circuits(db):
    course = _make_course(db)
    assert courses_crud.add_users_to_course(db, course.courseId, []) == []


@pytest.mark.integration
def test_add_users_to_course_repoints_existing_users(db):
    course = _make_course(db, name="Target")
    u1 = _make_user(db)
    u2 = _make_user(db)

    affected = courses_crud.add_users_to_course(
        db, course.courseId, [u1.userId, u2.userId]
    )

    assert {u.userId for u in affected} == {u1.userId, u2.userId}
    assert all(u.courseId == course.courseId for u in affected)

    db.refresh(u1)
    db.refresh(u2)
    assert u1.courseId == course.courseId
    assert u2.courseId == course.courseId


@pytest.mark.integration
def test_remove_user_from_course_returns_true_when_member(db):
    course = _make_course(db)
    user = _make_user(db)
    user.courseId = course.courseId
    db.commit()

    assert courses_crud.remove_user_from_course(db, course.courseId, user.userId) is True
    db.refresh(user)
    assert user.courseId is None


@pytest.mark.integration
def test_remove_user_from_course_returns_false_for_wrong_course(db):
    """User ist Mitglied in Kurs A, Aufruf zielt auf Kurs B -> False, kein Side-Effect."""
    course_a = _make_course(db, name="A")
    course_b = _make_course(db, name="B")
    user = _make_user(db)
    user.courseId = course_a.courseId
    db.commit()

    assert (
        courses_crud.remove_user_from_course(db, course_b.courseId, user.userId)
        is False
    )
    db.refresh(user)
    assert user.courseId == course_a.courseId


@pytest.mark.integration
def test_remove_user_from_course_returns_false_for_unknown_user(db):
    course = _make_course(db)
    assert courses_crud.remove_user_from_course(db, course.courseId, uuid4()) is False
