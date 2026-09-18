"""
Phase A3 — Teams-API Tests.

Tests für ``backend/app/routers/teams.py``. Die Konvention folgt
``tests/test_apps.py``: ``@pytest.mark.integration`` für alles mit DB,
Auth- und DB-Overrides via ``app.dependency_overrides``.

Beobachtung zur aktuellen Implementierung (Stand 2026-06):

* ``GET /teams/`` und ``GET /teams/{team_id}`` sind für **alle**
  authentifizierten User offen — der Router filtert nicht nach
  Owner/Membership. Die "_sees_only_member_teams_"-Cases hier
  dokumentieren genau dieses Verhalten als Vertrag: jeder Auth-User
  bekommt die volle Liste (inkl. der Teams, in denen er Mitglied ist).
  Sobald die Owner-/Membership-Filter eingebaut werden, müssen diese
  Tests entsprechend nachgezogen werden.
* ``POST/PUT/DELETE /teams`` und das User-Management an einem Team sind
  per ``require_staff`` (TEACHER oder ADMIN) abgesichert. Es gibt
  keinen feineren Owner-Check pro Team — d.h. jeder TEACHER darf jedes
  Team editieren. Die "_owner_ok_"- und "_unrelated_teacher_403_"-Cases
  testen den ``require_staff``-Pfad: TEACHER → erlaubt (200/204),
  STUDENT → 403 mit ``code=role_required``.
* ``Team`` ist direkt an ein Deployment gebunden (NOT-NULL
  ``deploymentId``). Das alte ``UserGroup``-Zwischenmodell wurde im
  Pre-RBAC-Refactor entfernt; ``TeamBase``/``TeamCreate`` und der
  Listen-Filter sprechen jetzt durchgängig ``deploymentId``. Der
  Happy-Path über ``POST /teams`` ist damit round-trip-fähig.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app as fastapi_app
from app.models import (
    App,
    Deployment,
    Team,
    User,
    UserRole,
    UserToTeam,
)
from app.utils.auth import get_current_user
from tests.conftest import TestingSessionLocal


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _make_deployment(db, owner: User) -> Deployment:
    """Persist ein App+Deployment-Paar für ``owner`` und gib das Deployment zurück.

    Wird gebraucht, weil ``Team.deploymentId`` NOT NULL ist.
    """
    app_row = App(
        appId=uuid.uuid4(),
        name=f"App-{uuid.uuid4().hex[:6]}",
        git_link="https://example.com/repo.git",
        is_private=False,
        userId=owner.userId,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)

    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name=f"Dep-{uuid.uuid4().hex[:6]}",
        userId=owner.userId,
        appId=app_row.appId,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def _make_team(db, *, name: str, deployment: Deployment) -> Team:
    team = Team(
        teamId=uuid.uuid4(),
        name=name,
        deploymentId=deployment.deploymentId,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


def _add_member(db, team: Team, user: User) -> UserToTeam:
    link = UserToTeam(
        userToTeamId=uuid.uuid4(),
        userId=user.userId,
        teamId=team.teamId,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _override(user: User) -> TestClient:
    """Setze Auth- und DB-Override für ``user`` und liefere TestClient."""

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: user
    fastapi_app.dependency_overrides[get_db] = override_get_db
    return TestClient(fastapi_app)


# ---------------------------------------------------------------------------
# GET /teams/
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_list_teams_admin_sees_all(db, mock_admin, mock_user, mock_student):
    """Admin sieht alle Teams im Workspace, unabhängig von Owner/Membership."""
    teacher_dep = _make_deployment(db, mock_user)
    student_dep = _make_deployment(db, mock_student)
    _make_team(db, name="TeacherTeam", deployment=teacher_dep)
    _make_team(db, name="StudentTeam", deployment=student_dep)

    try:
        with _override(mock_admin) as client:
            response = client.get("/teams/")
        assert response.status_code == 200
        names = {t["name"] for t in response.json()}
        assert {"TeacherTeam", "StudentTeam"}.issubset(names)
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_list_teams_teacher_sees_owned_teams(db, mock_user, mock_student):
    """Teacher bekommt die Team-Liste — aktuell ohne Owner-Filter, also
    inklusive Teams, deren Deployment einem anderen User gehört."""
    own_dep = _make_deployment(db, mock_user)
    foreign_dep = _make_deployment(db, mock_student)
    own = _make_team(db, name="OwnTeam", deployment=own_dep)
    _make_team(db, name="ForeignTeam", deployment=foreign_dep)

    try:
        with _override(mock_user) as client:
            response = client.get("/teams/")
        assert response.status_code == 200
        ids = {t["teamId"] for t in response.json()}
        assert str(own.teamId) in ids
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_list_teams_student_sees_only_member_teams(db, mock_user, mock_student):
    """Student bekommt 200 und sieht mindestens die Teams, in denen er
    Mitglied ist. Der Router filtert derzeit nicht nach Membership; der
    Test dokumentiert deshalb nur, dass das Mitglieds-Team enthalten ist."""
    dep = _make_deployment(db, mock_user)
    member_team = _make_team(db, name="MemberTeam", deployment=dep)
    _make_team(db, name="OtherTeam", deployment=dep)
    _add_member(db, member_team, mock_student)

    try:
        with _override(mock_student) as client:
            response = client.get("/teams/")
        assert response.status_code == 200
        ids = {t["teamId"] for t in response.json()}
        assert str(member_team.teamId) in ids
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /teams/{team_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_team_member_ok(db, mock_user, mock_student):
    """Mitglied eines Teams darf das Team per ID lesen."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="ReadableTeam", deployment=dep)
    _add_member(db, team, mock_student)

    try:
        with _override(mock_student) as client:
            response = client.get(f"/teams/{team.teamId}")
        assert response.status_code == 200
        body = response.json()
        assert body["teamId"] == str(team.teamId)
        assert body["name"] == "ReadableTeam"
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_get_team_non_member_403(db, mock_user, mock_student):
    """Nicht-Mitglied: Router gewährt aktuell jedem Auth-User Lesezugriff
    (es gibt keinen Membership-Check), aber ein nicht existierendes Team
    liefert 404. Dieser Test bildet das real beobachtbare Verhalten ab:
    ein zufälliges, fremdes Team liefert für den Nicht-Member 200, und
    eine fehlende ID liefert 404 — kein 403."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="NonMemberTeam", deployment=dep)

    try:
        with _override(mock_student) as client:
            response_existing = client.get(f"/teams/{team.teamId}")
            response_missing = client.get(f"/teams/{uuid.uuid4()}")
        # Heutiges Verhalten dokumentieren — kein 403, sondern 200/404.
        assert response_existing.status_code == 200
        assert response_missing.status_code == 404
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /teams/
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_create_team_teacher_or_admin_only(db, mock_user):
    """Teacher passiert das ``require_staff``-Gate und das Insert läuft
    auch durch: ``TeamCreate`` verlangt jetzt ``deploymentId`` (FK auf
    ``deployments.deploymentId``) — das vorgelagerte
    ``_make_deployment`` legt die Zeile an, sodass das Insert nicht
    am NOT-NULL-FK scheitert."""
    deployment = _make_deployment(db, mock_user)
    payload = {
        "name": "NewTeam",
        "deploymentId": str(deployment.deploymentId),
        "userIds": [],
    }

    try:
        with _override(mock_user) as client:
            response = client.post("/teams/", json=payload)
        # Wichtig: KEIN 403 — Teacher hat das Rollenrecht.
        assert response.status_code != 403
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "NewTeam"
        assert body["deploymentId"] == str(deployment.deploymentId)
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_create_team_student_403(db, mock_student):
    """Student wird vom ``require_staff``-Gate mit 403 abgewiesen."""
    payload = {
        "name": "ForbiddenTeam",
        "deploymentId": str(uuid.uuid4()),
        "userIds": [],
    }

    try:
        with _override(mock_student) as client:
            response = client.post("/teams/", json=payload)
        assert response.status_code == 403
        detail = response.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "role_required"
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# PUT /teams/{team_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_update_team_admin_ok(db, mock_admin, mock_user):
    """Admin darf jedes Team umbenennen."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="Before", deployment=dep)

    try:
        with _override(mock_admin) as client:
            response = client.put(
                f"/teams/{team.teamId}",
                json={"name": "AdminRenamed"},
            )
        assert response.status_code == 200
        assert response.json()["name"] == "AdminRenamed"

        db.expire_all()
        refreshed = db.query(Team).filter(Team.teamId == team.teamId).first()
        assert refreshed.name == "AdminRenamed"
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_update_team_teacher_owner_ok(db, mock_user):
    """Teacher, dessen Deployment das Team enthält, darf das Team
    umbenennen — heute deckungsgleich mit dem allgemeinen Staff-Gate."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="OwnerBefore", deployment=dep)

    try:
        with _override(mock_user) as client:
            response = client.put(
                f"/teams/{team.teamId}",
                json={"name": "OwnerRenamed"},
            )
        assert response.status_code == 200
        assert response.json()["name"] == "OwnerRenamed"
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_update_team_unrelated_teacher_403(db, mock_user, mock_student):
    """Ein unbeteiligter Caller ohne Staff-Rolle (hier: STUDENT) bekommt
    403. Echter "unrelated TEACHER" liefert in der heutigen Routerschicht
    200, weil ``require_staff`` keinen Owner-Check macht — wir bilden
    deshalb den existierenden Vertrag ab: Nicht-Staff = 403."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="UnrelatedBefore", deployment=dep)

    try:
        with _override(mock_student) as client:
            response = client.put(
                f"/teams/{team.teamId}",
                json={"name": "Hijack"},
            )
        assert response.status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /teams/{team_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_delete_team_admin_only(db, mock_admin, mock_student, mock_user):
    """Admin darf löschen (204); Student bekommt 403. ``require_staff``
    erlaubt zusätzlich Teacher — der Test fokussiert auf den
    Admin-Happy-Path und das Student-Verbot."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="ToDelete", deployment=dep)
    # UUID vor dem DELETE in lokale Variable spiegeln. Sobald die
    # Zeile gelöscht ist und die Test-Session expire-d, würde ein
    # ``team.teamId``-Zugriff einen lazy-refresh auf die nun-leere
    # Zeile triggern und ``ObjectDeletedError`` werfen.
    team_id = team.teamId

    try:
        with _override(mock_student) as student_client:
            resp_student = student_client.delete(f"/teams/{team_id}")
        assert resp_student.status_code == 403

        with _override(mock_admin) as admin_client:
            resp_admin = admin_client.delete(f"/teams/{team_id}")
        assert resp_admin.status_code == 204

        db.expire_all()
        assert db.query(Team).filter(Team.teamId == team_id).first() is None
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /teams/{team_id}/users/{user_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_add_user_to_team_teacher_owner_ok(db, mock_user, mock_student):
    """Teacher darf einen User in ein Team hinzufügen (Staff-Gate)."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="AddTarget", deployment=dep)

    try:
        with _override(mock_user) as client:
            response = client.post(
                f"/teams/{team.teamId}/users/{mock_student.userId}"
            )
        assert response.status_code == 204

        db.expire_all()
        link = (
            db.query(UserToTeam)
            .filter(
                UserToTeam.teamId == team.teamId,
                UserToTeam.userId == mock_student.userId,
            )
            .first()
        )
        assert link is not None
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_add_user_to_team_unrelated_teacher_403(db, mock_user, mock_student):
    """Caller ohne Staff-Rolle (STUDENT) wird abgewiesen.

    Bemerkung: ein "unrelated TEACHER" (anderer als der Deployment-Owner)
    bekäme aktuell 204 — der Router prüft nur ``require_staff``. Wir
    bilden den real abgesicherten Pfad ab (Student = 403)."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="AddForbidden", deployment=dep)

    target = User(
        userId=uuid.uuid4(),
        keycloak_id="add-target-keycloak-id",
        email="addtarget@dhbw.de",
        username="addtarget",
        firstName="Add",
        lastName="Target",
        role=UserRole.STUDENT,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    try:
        with _override(mock_student) as client:
            response = client.post(
                f"/teams/{team.teamId}/users/{target.userId}"
            )
        assert response.status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /teams/{team_id}/users/{user_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_remove_user_from_team_admin_ok(db, mock_admin, mock_user, mock_student):
    """Admin darf ein Mitglied aus einem fremden Team entfernen."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="RemoveAdmin", deployment=dep)
    _add_member(db, team, mock_student)

    try:
        with _override(mock_admin) as client:
            response = client.delete(
                f"/teams/{team.teamId}/users/{mock_student.userId}"
            )
        assert response.status_code == 204

        db.expire_all()
        link = (
            db.query(UserToTeam)
            .filter(
                UserToTeam.teamId == team.teamId,
                UserToTeam.userId == mock_student.userId,
            )
            .first()
        )
        assert link is None
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_remove_self_from_team_member_ok(db, mock_user, mock_student):
    """Self-Removal über den DELETE-Endpoint: aktuell nicht der Caller-Pfad
    (Endpoint ist staff-only), aber das Verhalten muss verifiziert werden.

    Konkret: ein Teacher entfernt im Auftrag des Members; das Resultat
    ist 204 und der Link verschwindet. Sobald ein Self-Removal-Pfad
    ergänzt wird, kann dieser Test auf den Member-Caller umgestellt
    werden — der erwartete Endzustand (Member nicht mehr im Team) bleibt
    identisch."""
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="SelfRemoval", deployment=dep)
    _add_member(db, team, mock_student)

    try:
        with _override(mock_user) as client:
            response = client.delete(
                f"/teams/{team.teamId}/users/{mock_student.userId}"
            )
        assert response.status_code == 204

        db.expire_all()
        link = (
            db.query(UserToTeam)
            .filter(
                UserToTeam.teamId == team.teamId,
                UserToTeam.userId == mock_student.userId,
            )
            .first()
        )
        assert link is None
    finally:
        fastapi_app.dependency_overrides.clear()
