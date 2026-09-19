"""LTI 1.3 launch — validation, provisioning and the negative cases.

The suite runs without Moodle. A test-local RSA key pair stands in for
the platform's signing key: the tool configuration is handed the public
half as an inline ``key_set``, which short-circuits the JWKS fetch, and
the tests mint ``id_token``s with the private half. "Forged signature"
is then simply a second key pair.

The two pieces of state a real launch would have left behind are seeded
directly: the ``nonce`` in the launch storage and the ``state`` cookie.
That is exactly what ``/lti/login`` does, and skipping it here keeps
each test about one thing.
"""
import base64
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from pylti1p3.registration import Registration
from pylti1p3.tool_config import ToolConfDict

from app.config import settings
from app.main import app
from app.models import (
    IdentityProvider,
    LtiContext,
    User,
    UserIdentity,
    UserRole,
)
from app.utils.auth import get_current_keycloak_user
from app.utils.keycloak_auth import sync_user_from_keycloak
from app.utils.lti_fastapi import ConsumingCacheDataStorage, _RedisShim
from app.utils.lti_session import SESSION_ISSUER, create_session_token

ISSUER = "http://moodle.test"
CLIENT_ID = "test-client-id"
DEPLOYMENT_ID = "1"
CONTEXT_ID = "course-42"

CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
SYS_ADMIN = "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator"


# ----------------------------------------------------------------
# KEYS
# ----------------------------------------------------------------
def _make_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture(scope="module")
def platform_keys():
    """The key pair standing in for Moodle's signing key."""
    return _make_keypair()


@pytest.fixture(scope="module")
def attacker_keys():
    """A second pair — signatures made with it must be rejected."""
    return _make_keypair()


@pytest.fixture(scope="module")
def tool_keys():
    """This tool's own key pair, for /lti/jwks."""
    return _make_keypair()


# ----------------------------------------------------------------
# IN-MEMORY LAUNCH STORAGE
# ----------------------------------------------------------------
class _DictCache:
    """The calls ``ConsumingCacheDataStorage`` makes on its backend."""

    def __init__(self, store: dict):
        self._store = store

    def get(self, key):
        return self._store.get(key)

    def get_and_delete(self, key):
        return self._store.pop(key, None)

    def set(self, key, value, exp=None) -> None:
        self._store[key] = value


class _DictStorage(ConsumingCacheDataStorage):
    """Stands in for Redis.

    Subclasses ``CacheDataStorage`` rather than the bare storage base so
    the session-id handling matches production: over plain http the
    library deliberately drops the session prefix, and a storage that
    did not inherit that behaviour would demand a ``session-id`` cookie
    that never exists in a local, non-TLS launch.
    """

    def __init__(self, store: dict, **kwargs):
        self._cache = _DictCache(store)
        super().__init__(**kwargs)


@pytest.fixture
def launch_store():
    return {}


# ----------------------------------------------------------------
# WIRING
# ----------------------------------------------------------------
@pytest.fixture
def lti_env(monkeypatch, platform_keys, tool_keys, launch_store):
    """Switch LTI on and point it at the test key material."""
    _, platform_public = platform_keys
    tool_private, _ = tool_keys

    monkeypatch.setattr(settings, "LTI_ENABLED", True)
    monkeypatch.setattr(settings, "LTI_PLATFORM_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "LTI_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "LTI_DEPLOYMENT_ID", DEPLOYMENT_ID)
    monkeypatch.setattr(settings, "LTI_JWKS_URL", f"{ISSUER}/mod/lti/certs.php")
    monkeypatch.setattr(settings, "LTI_AUTH_LOGIN_URL", f"{ISSUER}/mod/lti/auth.php")
    monkeypatch.setattr(settings, "LTI_TOKEN_URL", f"{ISSUER}/mod/lti/token.php")
    monkeypatch.setattr(
        settings, "LTI_PRIVATE_KEY_B64", base64.b64encode(tool_private.encode()).decode()
    )
    monkeypatch.setattr(settings, "LTI_SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(settings, "LTI_TRUST_INSTRUCTOR_ROLE", False)
    monkeypatch.setattr(
        settings, "LTI_LAUNCH_REDIRECT_URL", "http://frontend.test/lti/callback"
    )
    monkeypatch.setattr(
        settings, "LTI_LINK_REDIRECT_URL", "http://frontend.test/lti/link"
    )

    platform_jwk = Registration.get_jwk(platform_public)

    def fake_tool_conf():
        conf = ToolConfDict(
            {
                ISSUER: {
                    "default": True,
                    "client_id": CLIENT_ID,
                    "auth_login_url": settings.LTI_AUTH_LOGIN_URL,
                    "auth_token_url": settings.LTI_TOKEN_URL,
                    "auth_audience": None,
                    "key_set_url": settings.LTI_JWKS_URL,
                    # Inline key set — no HTTP call to the platform.
                    "key_set": {"keys": [platform_jwk]},
                    "deployment_ids": [DEPLOYMENT_ID],
                }
            }
        )
        conf.set_private_key(ISSUER, tool_private, client_id=CLIENT_ID)
        conf.set_public_key(ISSUER, tool_keys[1], client_id=CLIENT_ID)
        return conf

    monkeypatch.setattr("app.routers.lti.get_tool_conf", fake_tool_conf)
    monkeypatch.setattr(
        "app.routers.lti.get_launch_storage", lambda: _DictStorage(launch_store)
    )
    return {"kid": platform_jwk["kid"]}


# ----------------------------------------------------------------
# TOKEN BUILDER
# ----------------------------------------------------------------
def _id_token(
    private_pem,
    kid,
    *,
    nonce,
    sub="moodle-user-1",
    email="anna@dhbw.de",
    roles=(LEARNER,),
    iss=ISSUER,
    aud=CLIENT_ID,
    deployment_id=DEPLOYMENT_ID,
    exp_delta=timedelta(minutes=5),
    context_id=CONTEXT_ID,
):
    now = datetime.now(UTC)
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "exp": now + exp_delta,
        "iat": now,
        "nonce": nonce,
        "given_name": "Anna",
        "family_name": "Beispiel",
        "name": "Anna Beispiel",
        f"{CLAIM}message_type": "LtiResourceLinkRequest",
        f"{CLAIM}version": "1.3.0",
        f"{CLAIM}deployment_id": deployment_id,
        f"{CLAIM}target_link_uri": "http://backend.test/lti/launch",
        f"{CLAIM}roles": list(roles),
        f"{CLAIM}resource_link": {"id": "res-1"},
    }
    if email:
        claims["email"] = email
    if context_id:
        claims[f"{CLAIM}context"] = {
            "id": context_id,
            "title": "Cloud Computing",
            "label": "CC-2026",
        }
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _launch(client, token, *, state="state-abc"):
    """POST a launch with the state cookie a real login would have set."""
    client.cookies.set(f"lti1p3-{state}", state)
    return client.post(
        "/lti/launch",
        data={"id_token": token, "state": state},
        follow_redirects=False,
    )


def _seed_nonce(store, nonce):
    """What /lti/login would have written before redirecting."""
    store[f"lti1p3-nonce-{nonce}"] = True


# ================================================================
# JWKS
# ================================================================
def test_jwks_serves_the_tool_public_key(unauth_client, lti_env):
    resp = unauth_client.get("/lti/jwks")

    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "RSA"
    assert keys[0]["alg"] == "RS256"
    assert keys[0]["kid"]


def test_lti_endpoints_are_off_unless_enabled(unauth_client, monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", False)

    assert unauth_client.get("/lti/jwks").status_code == 503
    assert unauth_client.post("/lti/launch", data={}).status_code == 503


# ================================================================
# HAPPY PATH
# ================================================================
def test_valid_launch_creates_user_identity_and_context(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-1")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-1")

    resp = _launch(unauth_client, token)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("http://frontend.test/lti/callback?token=")

    user = db.query(User).filter(User.email == "anna@dhbw.de").one()
    assert user.firstName == "Anna"
    assert user.role == UserRole.STUDENT

    identity = db.query(UserIdentity).one()
    assert identity.provider == IdentityProvider.LTI
    assert identity.issuer == ISSUER
    assert identity.subject == "moodle-user-1"
    assert identity.userId == user.userId

    context = db.query(LtiContext).one()
    assert context.context_id == CONTEXT_ID
    assert context.title == "Cloud Computing"
    # A Moodle course is not a Studiengruppe — the mapping stays open.
    assert context.courseId is None


def test_the_launch_redirect_carries_where_to_land(
    unauth_client, lti_env, launch_store, platform_keys
):
    """The callback needs more than a token.

    A student arriving from Moodle should end up at their environment,
    so the launch computes the destination and hands it over. This one
    has no environment yet, so it is the list — the narrowing itself is
    covered in ``test_lti_launch_target.py``.
    """
    _seed_nonce(launch_store, "nonce-target")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-target")

    resp = _launch(unauth_client, token, state="state-target")

    assert resp.status_code == 302
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["target"] == ["/deployments"]
    # The session token still travels alongside it.
    assert query["token"]


def test_second_launch_reuses_the_same_user(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    for nonce in ("nonce-a", "nonce-b"):
        _seed_nonce(launch_store, nonce)
        token = _id_token(platform_keys[0], lti_env["kid"], nonce=nonce)
        assert _launch(unauth_client, token, state=f"state-{nonce}").status_code == 302

    assert db.query(User).count() == 1
    assert db.query(UserIdentity).count() == 1
    assert db.query(LtiContext).count() == 1


def test_session_token_authenticates_against_the_normal_api(
    unauth_client, lti_env, launch_store, platform_keys
):
    _seed_nonce(launch_store, "nonce-me")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-me")

    location = _launch(unauth_client, token).headers["location"]
    # Parsed, not split: the redirect carries the landing target next to
    # the token, and a ``split("token=")`` would swallow it into the JWT.
    session_token = parse_qs(urlparse(location).query)["token"][0]

    claims = jwt.decode(
        session_token, settings.LTI_SESSION_SECRET, algorithms=["HS256"], issuer=SESSION_ISSUER
    )
    assert claims["lti_context_id"] == CONTEXT_ID
    assert claims["lti_context_role"] == "learner"

    me = unauth_client.get("/users/me", headers={"Authorization": f"Bearer {session_token}"})

    assert me.status_code == 200
    assert me.json()["email"] == "anna@dhbw.de"


# ================================================================
# ROLES
# ================================================================
def test_instructor_stays_a_student_unless_the_platform_is_trusted(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-t")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-t", roles=(INSTRUCTOR,)
    )

    assert _launch(unauth_client, token).status_code == 302
    assert db.query(User).one().role == UserRole.STUDENT


def test_instructor_becomes_teacher_when_the_platform_is_trusted(
    unauth_client, lti_env, launch_store, platform_keys, db, monkeypatch
):
    monkeypatch.setattr(settings, "LTI_TRUST_INSTRUCTOR_ROLE", True)
    _seed_nonce(launch_store, "nonce-t2")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-t2", roles=(INSTRUCTOR,)
    )

    assert _launch(unauth_client, token).status_code == 302
    assert db.query(User).one().role == UserRole.TEACHER


def test_moodle_site_admin_does_not_become_app_store_admin(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-adm")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-adm", roles=(SYS_ADMIN,)
    )

    assert _launch(unauth_client, token).status_code == 302
    assert db.query(User).one().role != UserRole.ADMIN


def test_launch_never_downgrades_a_linked_admin(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """An admin who launches from a course stays an admin.

    The account is reached through its LTI identity, not through the
    e-mail address — that path is closed, see the test below.
    """
    existing = User(
        userId=uuid.uuid4(),
        email="anna@dhbw.de",
        username="anna",
        role=UserRole.ADMIN,
    )
    db.add(existing)
    db.flush()
    db.add(
        UserIdentity(
            userId=existing.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="moodle-user-1",
        )
    )
    db.commit()

    _seed_nonce(launch_store, "nonce-dg")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-dg")

    assert _launch(unauth_client, token).status_code == 302

    db.expire_all()
    assert db.query(User).one().role == UserRole.ADMIN


def test_email_match_cannot_take_over_a_privileged_account(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """The account-takeover case.

    Moodle lets users edit their own e-mail address. A launch therefore
    proves only that Moodle relayed the address, never that it belongs
    to the person launching. An unlinked identity claiming a lecturer's
    address must not be handed that lecturer's account.
    """
    victim = User(
        userId=uuid.uuid4(),
        email="anna@dhbw.de",
        username="anna",
        role=UserRole.TEACHER,
    )
    db.add(victim)
    db.commit()

    _seed_nonce(launch_store, "nonce-to")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-to", sub="attacker-1"
    )

    resp = _launch(unauth_client, token)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith("http://frontend.test/lti/link?")

    db.expire_all()
    assert db.query(User).count() == 1
    assert db.query(User).one().role == UserRole.TEACHER
    # No identity was linked to the victim, and no second account made.
    assert db.query(UserIdentity).count() == 0


def test_launch_onto_a_taken_email_is_refused_and_offers_a_link(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """The e-mail claim alone never opens an existing account.

    Moodle lets anyone edit their own profile address, so a launch
    proves only that Moodle relayed it. Even a bare student account
    with nothing in it stays shut: the way in is the link challenge,
    which the account owner has to spend while signed in.
    """
    existing = User(
        userId=uuid.uuid4(),
        email="anna@dhbw.de",
        username="anna",
        role=UserRole.STUDENT,
    )
    db.add(existing)
    db.commit()

    _seed_nonce(launch_store, "nonce-taken")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-taken", sub="attacker-2"
    )

    resp = _launch(unauth_client, token)

    # A launch is a form POST from Moodle landing straight in the
    # browser, so the refusal has to be a page the person can act on,
    # not a JSON body.
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("http://frontend.test/lti/link?")
    assert parse_qs(urlparse(location).query)["challenge"][0]
    assert db.query(UserIdentity).count() == 0
    assert db.query(User).count() == 1


def test_launch_does_not_overwrite_the_account_email(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """The address on the account is not the platform's to change.

    It is where deployment credentials are mailed. Following an
    editable Moodle profile field would let a launch redirect that mail
    to an address of the launcher's choosing.
    """
    user = User(
        userId=uuid.uuid4(),
        email="anna@dhbw.de",
        username="anna",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.add(
        UserIdentity(
            userId=user.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="moodle-user-1",
        )
    )
    db.commit()

    _seed_nonce(launch_store, "nonce-mail")
    token = _id_token(
        platform_keys[0],
        lti_env["kid"],
        nonce="nonce-mail",
        email="attacker-inbox@dhbw.de",
    )

    assert _launch(unauth_client, token).status_code == 302

    db.refresh(user)
    assert user.email == "anna@dhbw.de"


# ================================================================
# NEGATIVE CASES
# ================================================================
def test_forged_signature_is_rejected(
    unauth_client, lti_env, launch_store, attacker_keys, db
):
    _seed_nonce(launch_store, "nonce-f")
    token = _id_token(attacker_keys[0], lti_env["kid"], nonce="nonce-f")

    resp = _launch(unauth_client, token)

    assert resp.status_code == 401
    assert db.query(User).count() == 0


def test_unknown_issuer_is_rejected(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-i")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-i", iss="http://evil.test"
    )

    assert _launch(unauth_client, token).status_code == 401
    assert db.query(User).count() == 0


def test_wrong_audience_is_rejected(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-au")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-au", aud="someone-else"
    )

    assert _launch(unauth_client, token).status_code == 401
    assert db.query(User).count() == 0


def test_unknown_deployment_is_rejected(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-d")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-d", deployment_id="99"
    )

    assert _launch(unauth_client, token).status_code == 401
    assert db.query(User).count() == 0


def test_expired_token_is_rejected(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-e")
    token = _id_token(
        platform_keys[0],
        lti_env["kid"],
        nonce="nonce-e",
        exp_delta=timedelta(minutes=-5),
    )

    assert _launch(unauth_client, token).status_code == 401
    assert db.query(User).count() == 0


def test_replayed_token_is_rejected_the_second_time(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-r")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-r")

    assert _launch(unauth_client, token).status_code == 302

    # The launch must have spent the nonce. Nothing removes it here —
    # that is the point: a storage that only checks for existence lets
    # the very same token through again until its own ``exp`` passes.
    assert "lti1p3-nonce-nonce-r" not in launch_store

    assert _launch(unauth_client, token, state="state-replay").status_code == 401
    assert db.query(User).count() == 1


def test_launch_without_a_token_does_nothing(unauth_client, lti_env, db):
    resp = unauth_client.post("/lti/launch", data={}, follow_redirects=False)

    assert resp.status_code == 401
    assert db.query(User).count() == 0


def test_launch_without_email_is_refused_with_a_pointer_to_moodle(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    _seed_nonce(launch_store, "nonce-ne")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-ne", email=None)

    resp = _launch(unauth_client, token)

    assert resp.status_code == 400
    assert "privacy" in resp.json()["detail"]["message"]
    assert db.query(User).count() == 0


def test_garbage_bearer_token_is_rejected(unauth_client):
    resp = unauth_client.get("/users/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert resp.status_code == 401


def test_session_token_is_rejected_when_no_secret_is_configured(
    unauth_client, monkeypatch
):
    """Reachable anonymously, so it must not surface as a 500.

    Dispatching to the LTI verifier only reads an *unverified* issuer,
    so anyone can hand-craft a token that lands there. With LTI off —
    the default — no secret is configured, and the verifier used to
    raise straight through the handler.
    """
    monkeypatch.setattr(settings, "LTI_SESSION_SECRET", "")
    forged = jwt.encode({"iss": SESSION_ISSUER, "sub": "x"}, "irrelevant", algorithm="HS256")

    resp = unauth_client.get("/users/me", headers={"Authorization": f"Bearer {forged}"})

    assert resp.status_code == 401


# ================================================================
# KEYCLOAK INTEROP
# ================================================================
def test_keycloak_sign_in_after_an_lti_launch_reuses_the_account(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """Same person, both sign-in paths — one row, not two.

    A launch creates the account without a ``keycloak_id``. When that
    person later signs in through Keycloak, the sync must adopt the
    existing row: ``users.email`` is UNIQUE, so inserting a second one
    raises ``IntegrityError``. That happens on *every* authenticated
    request, not just at login, which would lock the person out of the
    regular path for good.
    """
    _seed_nonce(launch_store, "nonce-kc")
    token = _id_token(platform_keys[0], lti_env["kid"], nonce="nonce-kc")
    assert _launch(unauth_client, token).status_code == 302

    launched = db.query(User).one()
    assert launched.keycloak_id is None

    synced = sync_user_from_keycloak(
        db,
        {
            "sub": "keycloak-sub-1",
            "email": "anna@dhbw.de",
            "email_verified": True,
            "username": "anna",
            "realm_access": {"roles": ["student"]},
        },
    )

    assert synced.userId == launched.userId
    assert synced.keycloak_id == "keycloak-sub-1"
    assert db.query(User).count() == 1


def test_launch_storage_never_writes_a_key_without_an_expiry():
    """Every cache key must forget itself on its own.

    PyLTI1p3 leaves ``exp`` out on some writes. A key stored without one
    never leaves Redis, so the storage has to supply a lifetime itself
    rather than pass the omission through.
    """

    class _RecordingClient:
        def __init__(self):
            self.writes = []

        def setex(self, key, ttl, payload):
            self.writes.append((key, ttl))

        def set(self, key, payload):
            self.writes.append((key, None))

    client = _RecordingClient()

    _RedisShim(client).set("lti1p3-nonce-x", True)

    assert client.writes == [("lti1p3-nonce-x", 3600)]


# ================================================================
# LINK CHALLENGE
# ================================================================
@pytest.fixture
def signed_in_directly():
    """Authenticate the way a direct Keycloak login would.

    The link endpoint deliberately does not accept a launched session,
    so these tests cannot reuse the LTI token — they stand in for the
    Keycloak half instead.
    """

    def _sign_in(user):
        app.dependency_overrides[get_current_keycloak_user] = lambda: user
        return user

    yield _sign_in
    app.dependency_overrides.pop(get_current_keycloak_user, None)


def _challenge_for(client, lti_env, launch_store, platform_keys, *, nonce, sub):
    """Run a launch that gets refused and hand back its challenge."""
    _seed_nonce(launch_store, nonce)
    token = _id_token(platform_keys[0], lti_env["kid"], nonce=nonce, sub=sub)
    resp = _launch(client, token, state=f"state-{nonce}")
    assert resp.status_code == 302
    return parse_qs(urlparse(resp.headers["location"]).query)["challenge"][0]


def test_spending_a_link_challenge_attaches_the_moodle_identity(
    unauth_client, lti_env, launch_store, platform_keys, db, signed_in_directly
):
    """The full way in: Moodle signs for the identity, the owner for the account."""
    owner = User(
        userId=uuid.uuid4(),
        email="anna@dhbw.de",
        username="anna",
        role=UserRole.STUDENT,
    )
    db.add(owner)
    db.commit()

    challenge = _challenge_for(
        unauth_client, lti_env, launch_store, platform_keys, nonce="nonce-l1", sub="moodle-anna"
    )
    signed_in_directly(owner)

    resp = unauth_client.post("/lti/link", json={"challenge": challenge})

    assert resp.status_code == 200
    assert resp.json()["status"] == "linked"
    identity = db.query(UserIdentity).one()
    assert identity.userId == owner.userId
    assert identity.subject == "moodle-anna"

    # And the launch that was refused a moment ago now signs in.
    _seed_nonce(launch_store, "nonce-l2")
    token = _id_token(
        platform_keys[0], lti_env["kid"], nonce="nonce-l2", sub="moodle-anna"
    )
    second = _launch(unauth_client, token, state="state-l2")

    assert second.status_code == 302
    assert db.query(User).count() == 1


def test_a_link_challenge_can_only_be_spent_once(
    unauth_client, lti_env, launch_store, platform_keys, db, signed_in_directly
):
    """A leaked challenge is worth nothing once it has been used."""
    owner = User(
        userId=uuid.uuid4(), email="anna@dhbw.de", username="anna", role=UserRole.STUDENT
    )
    thief = User(
        userId=uuid.uuid4(), email="mallory@dhbw.de", username="mallory", role=UserRole.STUDENT
    )
    db.add_all([owner, thief])
    db.commit()

    challenge = _challenge_for(
        unauth_client, lti_env, launch_store, platform_keys, nonce="nonce-l3", sub="moodle-anna"
    )
    signed_in_directly(owner)
    assert unauth_client.post("/lti/link", json={"challenge": challenge}).status_code == 200

    signed_in_directly(thief)
    resp = unauth_client.post("/lti/link", json={"challenge": challenge})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "lti_link_challenge_spent"
    assert db.query(UserIdentity).count() == 1


def test_link_refuses_a_moodle_identity_that_belongs_to_another_account(
    unauth_client, lti_env, launch_store, platform_keys, db, signed_in_directly
):
    """A linked identity does not move. Otherwise the link step could take it."""
    owner = User(
        userId=uuid.uuid4(), email="anna@dhbw.de", username="anna", role=UserRole.STUDENT
    )
    other = User(
        userId=uuid.uuid4(), email="mallory@dhbw.de", username="mallory", role=UserRole.STUDENT
    )
    db.add_all([owner, other])
    db.commit()
    db.add(
        UserIdentity(
            userId=owner.userId,
            provider=IdentityProvider.LTI,
            issuer=ISSUER,
            subject="moodle-anna",
        )
    )
    db.commit()

    # A launch by that identity now signs in as the owner, so the
    # challenge has to be minted the way a second platform account
    # would arrive: same subject, address already taken.
    from app.routers import lti as lti_router
    from app.utils.lti_session import create_link_challenge

    challenge = create_link_challenge(
        issuer=ISSUER, subject="moodle-anna", email="anna@dhbw.de", jti="jti-x"
    )
    # Through the router's own storage — the fixture points that at the
    # in-memory store, and the endpoint looks the marker up there.
    lti_router.get_launch_storage().set_value(
        lti_router._challenge_key("jti-x"), True, exp=600
    )

    signed_in_directly(other)
    resp = unauth_client.post("/lti/link", json={"challenge": challenge})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "lti_identity_taken"
    assert db.query(UserIdentity).one().userId == owner.userId


def test_a_launched_session_cannot_authorise_a_link(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """The session is derived from the claim the link step has to verify."""
    owner = User(
        userId=uuid.uuid4(), email="anna@dhbw.de", username="anna", role=UserRole.STUDENT
    )
    db.add(owner)
    db.commit()

    challenge = _challenge_for(
        unauth_client, lti_env, launch_store, platform_keys, nonce="nonce-l4", sub="moodle-anna"
    )
    session = create_session_token(owner.userId)

    resp = unauth_client.post(
        "/lti/link",
        json={"challenge": challenge},
        headers={"Authorization": f"Bearer {session}"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "direct_login_required"
    assert db.query(UserIdentity).count() == 0


def test_a_link_challenge_is_not_a_session_token(
    unauth_client, lti_env, launch_store, platform_keys, db
):
    """Both are signed with the same secret, so the type claim has to separate them."""
    db.add(
        User(userId=uuid.uuid4(), email="anna@dhbw.de", username="anna", role=UserRole.STUDENT)
    )
    db.commit()

    challenge = _challenge_for(
        unauth_client, lti_env, launch_store, platform_keys, nonce="nonce-l5", sub="moodle-anna"
    )

    resp = unauth_client.get("/users/me", headers={"Authorization": f"Bearer {challenge}"})

    assert resp.status_code == 401


# ================================================================
# H-9 REGRESSION — account takeover via unverified email
# ================================================================
def test_keycloak_unverified_email_cannot_adopt_lti_account(db):
    """An attacker who registers in Keycloak with a victim's email address
    must NOT be able to adopt the victim's LTI-provisioned account when
    ``email_verified`` is False or absent.

    Before the fix, ``sync_user_from_keycloak`` would find the existing row
    by email and silently link the attacker's Keycloak subject to it —
    handing them the victim's deployments, credentials, and team memberships.
    """
    # Victim account created by a prior LTI launch (no keycloak_id).
    from app.models import User, UserRole
    victim = User(
        email="victim@dhbw.de",
        username="victim",
        role=UserRole.STUDENT,
    )
    db.add(victim)
    db.commit()
    db.refresh(victim)

    # Attacker signs in via Keycloak with the same email, but WITHOUT
    # email verification.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        sync_user_from_keycloak(
            db,
            {
                "id": "attacker-kc-sub",
                "email": "victim@dhbw.de",
                "email_verified": False,
                "username": "attacker",
                "realm_access": {"roles": ["student"]},
            },
        )

    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert detail.get("code") == "email_not_verified"

    # The victim row must be untouched — no keycloak_id written.
    db.refresh(victim)
    assert victim.keycloak_id is None
    assert db.query(User).count() == 1


def test_keycloak_verified_email_can_adopt_lti_account(db):
    """When ``email_verified`` is True the adoption is allowed — this is
    the legitimate first Keycloak sign-in for a user who was provisioned
    by an earlier LTI launch.
    """
    from app.models import User, UserRole
    existing = User(
        email="legit@dhbw.de",
        username="legit",
        role=UserRole.STUDENT,
    )
    db.add(existing)
    db.commit()
    db.refresh(existing)

    synced = sync_user_from_keycloak(
        db,
        {
            "id": "kc-sub-legit",
            "email": "legit@dhbw.de",
            "email_verified": True,
            "username": "legit",
            "realm_access": {"roles": ["student"]},
        },
    )

    assert synced.userId == existing.userId
    assert synced.keycloak_id == "kc-sub-legit"
    assert db.query(User).count() == 1
