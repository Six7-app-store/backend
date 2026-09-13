import uuid

import pytest
from fastapi.testclient import TestClient

from app.crud import apps as crud_apps
from app.database import get_db
from app.main import app as fastapi_app
from app.models import User, UserRole
from app.schemas import AppCreate
from app.utils.auth import get_current_user
from tests.conftest import TestingSessionLocal


@pytest.mark.integration
def test_get_apps_authenticated(client):
    response = client.get("/apps/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.integration
def test_get_apps_unauthenticated(unauth_client):
    response = unauth_client.get("/apps/")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PUT /apps/{app_id} — backend#49
# ---------------------------------------------------------------------------
# ``git_link`` must be immutable on update: once an app has deployments,
# changing the repo would make existing deployments point at a different
# repo than they originally deployed.

ORIGINAL_GIT_LINK = "https://example.com/orig.git"


@pytest.fixture
def existing_app(db, mock_user):
    """Persist an app owned by ``mock_user`` and yield it."""
    return crud_apps.create_app(
        db,
        AppCreate(
            name="Original Name",
            description="Original description",
            git_link=ORIGINAL_GIT_LINK,
        ),
        mock_user.userId,
    )


@pytest.mark.integration
def test_update_app_changes_name_and_description(client, existing_app, db):
    response = client.put(
        f"/apps/{existing_app.appId}",
        json={"name": "Renamed", "description": "New description"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "New description"
    # git_link round-trips unchanged
    assert body["git_link"] == ORIGINAL_GIT_LINK

    db.expire_all()
    refreshed = crud_apps.get_app(db, existing_app.appId)
    assert refreshed.name == "Renamed"
    assert refreshed.description == "New description"
    assert refreshed.git_link == ORIGINAL_GIT_LINK


@pytest.mark.integration
def test_update_app_ignores_git_link(client, existing_app, db):
    """Sending ``git_link`` in the request body must be silently ignored."""
    response = client.put(
        f"/apps/{existing_app.appId}",
        json={
            "name": "Renamed",
            "git_link": "https://evil.example/hijack.git",
        },
    )
    assert response.status_code == 200
    assert response.json()["git_link"] == ORIGINAL_GIT_LINK

    db.expire_all()
    refreshed = crud_apps.get_app(db, existing_app.appId)
    assert refreshed.git_link == ORIGINAL_GIT_LINK
    assert refreshed.name == "Renamed"


@pytest.mark.integration
def test_update_app_not_found(client):
    response = client.put(
        f"/apps/{uuid.uuid4()}",
        json={"name": "Anything"},
    )
    assert response.status_code == 404


@pytest.mark.integration
def test_update_app_unauthorized_user_rejected(db, existing_app):
    """A STUDENT who is not the owner must not be able to update the app."""
    other_student = User(
        userId=uuid.uuid4(),
        keycloak_id="other-student-keycloak-id",
        email="other@dhbw.de",
        username="otherstudent",
        firstName="Other",
        lastName="Student",
        role=UserRole.STUDENT,
    )
    db.add(other_student)
    db.commit()
    db.refresh(other_student)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: other_student
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as student_client:
            response = student_client.put(
                f"/apps/{existing_app.appId}",
                json={"name": "Hijacked"},
            )
        assert response.status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Phase 2 — Bug #2: Teacher kann KEINE fremden Apps editieren oder löschen
# ---------------------------------------------------------------------------
# Pre-Phase-2 ließ ``ensure_resource_access`` Teacher pauschal als
# Owner-Ersatz durchgehen. Phase 2 entfernt diesen Bypass: Teacher hat
# auf fremden Apps weder Edit- noch Delete-Recht; nur Admin behält den
# blanket-Zugriff. Eigene Apps (Teacher als Owner) bleiben unverändert
# editierbar.
@pytest.mark.integration
def test_teacher_cannot_update_foreign_app(db, existing_app):
    """Phase 2 — Bug #2: ein TEACHER, der nicht Owner ist, darf
    fremde Apps NICHT editieren. Vor Phase 2: 200 (Teacher-Bypass).
    Nach Phase 2: 403.
    """
    other_teacher = User(
        userId=uuid.uuid4(),
        keycloak_id="other-teacher-keycloak-id",
        email="otherteacher@dhbw.de",
        username="otherteacher",
        firstName="Other",
        lastName="Teacher",
        role=UserRole.TEACHER,
    )
    db.add(other_teacher)
    db.commit()
    db.refresh(other_teacher)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: other_teacher
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as teacher_client:
            response = teacher_client.put(
                f"/apps/{existing_app.appId}",
                json={"name": "TeacherTriedToHijack"},
            )
        assert response.status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_teacher_cannot_delete_foreign_app(db, existing_app):
    """Phase 2 — Bug #2: ein TEACHER darf fremde Apps NICHT löschen."""
    other_teacher = User(
        userId=uuid.uuid4(),
        keycloak_id="other-teacher-keycloak-id-2",
        email="otherteacher2@dhbw.de",
        username="otherteacher2",
        firstName="Other",
        lastName="Teacher",
        role=UserRole.TEACHER,
    )
    db.add(other_teacher)
    db.commit()
    db.refresh(other_teacher)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: other_teacher
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as teacher_client:
            response = teacher_client.delete(f"/apps/{existing_app.appId}")
        assert response.status_code == 403
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_admin_can_delete_foreign_app(db, existing_app):
    """Admin behält den blanket-Delete auf fremde Apps."""
    admin = User(
        userId=uuid.uuid4(),
        keycloak_id="admin-delete-keycloak-id",
        email="admindel@dhbw.de",
        username="admindel",
        firstName="Admin",
        lastName="Del",
        role=UserRole.ADMIN,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: admin
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as admin_client:
            response = admin_client.delete(f"/apps/{existing_app.appId}")
        assert response.status_code == 204
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Phase 2 — Bug #1: Role-Change ist Admin-only
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_non_admin_cannot_change_user_role(db, mock_user):
    """Phase 2 — Bug #1: nur Admin darf ``role`` auf ``PUT /users/{id}``
    setzen. Ein Teacher-Caller bekommt 403 mit
    ``{code: "role_required", required: ["admin"]}``.
    """
    target = User(
        userId=uuid.uuid4(),
        keycloak_id="target-keycloak-id",
        email="target@dhbw.de",
        username="target",
        firstName="Target",
        lastName="User",
        role=UserRole.STUDENT,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    # mock_user has role TEACHER (see conftest).
    fastapi_app.dependency_overrides[get_current_user] = lambda: mock_user
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as teacher_client:
            response = teacher_client.put(
                f"/users/{target.userId}",
                json={"role": "admin"},
            )
        assert response.status_code == 403
        body = response.json()
        # Detail follows the structured shape from require_roles.
        detail = body.get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "role_required"
        assert detail.get("required") == ["admin"]
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.mark.integration
def test_admin_can_change_user_role(db):
    """Admin-Caller darf die Rolle eines anderen Users ändern."""
    admin = User(
        userId=uuid.uuid4(),
        keycloak_id="admin-role-change-keycloak-id",
        email="adminrole@dhbw.de",
        username="adminrole",
        firstName="Admin",
        lastName="Role",
        role=UserRole.ADMIN,
    )
    target = User(
        userId=uuid.uuid4(),
        keycloak_id="target2-keycloak-id",
        email="target2@dhbw.de",
        username="target2",
        firstName="Target",
        lastName="Two",
        role=UserRole.STUDENT,
    )
    db.add_all([admin, target])
    db.commit()
    db.refresh(admin)
    db.refresh(target)

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user] = lambda: admin
    fastapi_app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(fastapi_app) as admin_client:
            response = admin_client.put(
                f"/users/{target.userId}",
                json={"role": "teacher"},
            )
        assert response.status_code == 200
        assert response.json()["role"] == "teacher"
    finally:
        fastapi_app.dependency_overrides.clear()
