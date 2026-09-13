"""Test-suite-wide fixtures and DB-isolation primitives.

# DB-Isolation: schema once per session, TRUNCATE per test

History — bis Juni 2026 baute jeder Test sein eigenes Schema auf und
riss es danach wieder ab (``Base.metadata.create_all`` /
``drop_all`` als ``autouse=True``-Fixture). Bei einer Suite von ~420
Tests sind das ~840 DDL-Wellen pro Lauf, jede mit FK-Lock-Kaskade
über ein Dutzend Tabellen. Das hat in der Praxis zwei Probleme
erzeugt:

1. **Hänger** — eine offene Test-Session, die noch eine Row in
   ``users`` hielt, blockierte das ``DROP TABLE users`` des nächsten
   Tests; psycopg2 wartete dann ewig im ``recv()`` und der Run blieb
   einfach stehen. Symptom: pytest-Prozess in ``do_sys_poll`` /
   ``futex_wait_queue`` ohne CPU-Last, kein Fortschritt mehr.
2. **Speicher** — Postgres reservierte für jede DDL-Welle frischen
   Backend-Speicher; bei 800+ Wellen sammelten sich genug Verbindungen
   und Catalog-Caches, dass der Container am Ende OOM-gekillt wurde
   (``Error 137`` SIGKILL auf dem nächstbesten Test, hier
   ``test_admin_can_deactivate_app_hides_from_students``, der vier
   Fixtures gleichzeitig zieht und so der nächste anstehende Lock-
   Wait war).

Lösung: Schema einmal pro Test-Session anlegen, zwischen den Tests
nur per ``TRUNCATE ... RESTART IDENTITY CASCADE`` reinigen. Postgres
macht das in einer einzigen Cursor-Operation, kein DDL-Lock, ~50× so
schnell wie der alte Pfad. Identische Test-Semantik (jeder Test
startet mit leeren Tabellen), aber ohne Lock-Stau und ohne
Speicherleck.

# TEST_DATABASE_URL-Gate

Wenn ``TEST_DATABASE_URL`` nicht gesetzt ist, fallen wir auf
``settings.DATABASE_URL`` zurück und warnen einmalig — local-dev mit
einer einzigen Postgres-Instanz darf das (der Entwickler akzeptiert,
dass der Dev-Datenbestand truncate-d wird). CI MUSS ``TEST_DATABASE_URL``
auf den isolierten ``postgres-test``-Service zeigen lassen, sonst
killt die Suite die Dev-Daten.
"""

import os
import uuid
import warnings

# IMPORTANT: this env-var must be set BEFORE ``app.main`` is imported.
# ``lifespan`` reads it on every ``TestClient(app)`` enter to decide
# whether to spawn the Celery event-listener thread and reconciler
# task. Without this gate, each test-client context stacks up another
# listener thread (daemon, never cleanly stopped) that holds DB +
# broker connections — within a handful of tests the SQLAlchemy pool
# is exhausted and the next legit query deadlocks. See ``app/main.py``
# for the full rationale.
os.environ.setdefault("DISABLE_BACKGROUND_TASKS", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import (  # noqa: F401  (importiert für seitliche Effekte / Metadata-Registrierung)
    App,
    User,
    UserRole,
)
from app.utils.auth import get_current_user

# ----------------------------------------------------------------
# Engine — einmal pro Test-Prozess.
#
# Connection-Pool großzügig dimensioniert, weil mehrere Fixtures
# gleichzeitig Sessions ziehen können (z.B. ``admin_client``,
# ``student_client``, ``db``, ``mock_user``, ``mock_admin`` in einem
# Test) und der dahinterliegende ``TestClient`` zusätzlich
# Dependency-overrides für ``get_db`` öffnet. Bei 7 max-Connections
# (alter Wert) trat in der Praxis Pool-Exhaustion auf, sobald die
# Background-Task-Threads (jetzt deaktiviert) noch dazukamen.
# 20 reicht mit Reserve; sequentielle Suite, Postgres-Container hat
# ``max_connections=100`` per Default.
# ----------------------------------------------------------------
_TEST_DB_URL = os.getenv("TEST_DATABASE_URL", settings.DATABASE_URL)
if _TEST_DB_URL == settings.DATABASE_URL:
    warnings.warn(
        "TEST_DATABASE_URL not set — tests will run against settings.DATABASE_URL "
        "and will TRUNCATE on tear-down. Set TEST_DATABASE_URL to a dedicated test DB.",
        UserWarning,
        stacklevel=2,
    )

engine = create_engine(
    _TEST_DB_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    pool_recycle=300,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ----------------------------------------------------------------
# Schema-Lifecycle — einmal pro pytest-Session.
# ----------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    """Create the full schema once at session start, drop at the end.

    The drop is a safety net for shared Postgres instances; on the
    isolated ``postgres-test`` service it's effectively a no-op
    because the container will be torn down anyway. Using
    ``scope='session'`` means the fixture body runs exactly twice
    (setup + teardown), not 2 × 420 = 840 times.
    """
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _truncate_tables(_setup_schema):
    """Reset DB state to empty between tests.

    Runs AFTER each test (yield first) so a failing test still leaves
    behind useful data for ``pdb`` / log inspection within the test
    itself. The ``TRUNCATE ... RESTART IDENTITY CASCADE`` form is
    Postgres-specific but the whole project is Postgres-only, so this
    is fine.

    Tables are listed in ``sorted_tables`` order (parents first) and
    we pass them in reverse so CASCADE handles the FK chain top-down
    without ``DETAIL`` warnings.

    The fixture depends on ``_setup_schema`` so pytest enforces
    ordering: schema MUST exist before the first truncate runs.
    """
    yield
    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    if not table_names:
        return
    quoted = ", ".join(f'"{n}"' for n in table_names)
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
        conn.commit()


# ----------------------------------------------------------------
# DB-Session-Fixture — frische Session pro Test, immer geschlossen.
# ----------------------------------------------------------------
@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        # Defensiv: alle laufenden Transaktionen rollen wir zurück,
        # bevor wir schließen — sonst hält ein open ``begin()`` eine
        # Connection im Pool, die der nächste ``_truncate_tables``
        # auf der gleichen Connection als Lock-Halter sieht.
        try:
            session.rollback()
        finally:
            session.close()


# ----------------------------------------------------------------
# Mock-User-Fixtures
# ----------------------------------------------------------------
@pytest.fixture
def mock_user(db):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="test-keycloak-id",
        email="test@dhbw.de",
        username="testuser",
        firstName="Test",
        lastName="User",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def mock_admin(db):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="admin-keycloak-id",
        email="admin@dhbw.de",
        username="adminuser",
        firstName="Admin",
        lastName="User",
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def mock_student(db):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="student-keycloak-id",
        email="student@dhbw.de",
        username="studentuser",
        firstName="Student",
        lastName="User",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ----------------------------------------------------------------
# FastAPI-Test-Clients
# ----------------------------------------------------------------
def _make_client(user):
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return TestClient(app)


@pytest.fixture
def client(mock_user):
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    def override_get_current_user():
        return mock_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(mock_admin):
    c = _make_client(mock_admin)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def student_client(mock_student):
    c = _make_client(mock_student)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


# ----------------------------------------------------------------
# SHARED DB HELPERS
# ----------------------------------------------------------------
def create_app_in_db(db, user, *, name="Test App", git_link="https://github.com/example/repo", is_private=False):
    db_app = App(
        appId=uuid.uuid4(),
        name=name,
        git_link=git_link,
        is_private=is_private,
        userId=user.userId,
    )
    db.add(db_app)
    db.commit()
    db.refresh(db_app)
    return db_app
