"""Binding a Moodle activity to one app, and honouring that binding.

Deep linking is a second message type on the launch endpoint. Instead
of opening the tool, Moodle asks what the activity being created should
point at; the lecturer picks, and the answer is signed back.

What it binds to is an **app**, never one environment. Which environment
a student opens is still resolved per person at launch time, so the same
activity works whether a course shares one environment or everybody has
their own — and it survives an environment being rebuilt. See
``deployment/docs/adr/0009-deep-link-bindet-an-app.md``.

The signing itself is exercised against a stub platform registration:
what matters here is that the handle is one-shot, belongs to exactly one
person, and cannot be pointed at an app the caller may not see.
"""

import uuid

import pytest

from app.config import settings
from app.models import App, Deployment, User, UserRole, UserToDeployment
from app.routers import lti as lti_router
from app.services import lti_service

pytestmark = pytest.mark.integration

ISSUER = "https://moodle.test"
RETURN_URL = f"{ISSUER}/mod/lti/contentitem_return.php"
LAUNCH_URL = "http://tool.test/lti/launch"


@pytest.fixture(autouse=True)
def lti_on(monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", True)


# ----------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------
def _app(db, owner, *, name="Nextcloud", private=False):
    row = App(name=name, userId=owner.userId, is_private=private)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _park(user, *, storage_patch, handle="h1", user_id=None):
    """Put a pending selection where the endpoint will look for it."""
    storage_patch[lti_router._deep_link_key(handle)] = {
        "iss": ISSUER,
        "deployment_id": "1",
        "settings": {"deep_link_return_url": RETURN_URL},
        "target_link_uri": LAUNCH_URL,
        "user_id": str(user_id or user.userId),
    }
    return handle


@pytest.fixture
def storage_patch(monkeypatch):
    """An in-memory stand-in for the Redis launch storage.

    ``check_value`` consumes, exactly as the real one does — that is the
    property the one-shot behaviour rests on.
    """
    store: dict = {}

    class _Storage:
        def set_value(self, key, value, exp=None):
            store[key] = value

        def get_value(self, key):
            return store.get(key)

        def check_value(self, key):
            return store.pop(key, None) is not None

    monkeypatch.setattr(lti_service, "get_launch_storage", lambda: _Storage())
    monkeypatch.setattr(lti_router, "get_launch_storage", lambda: _Storage())
    return store


@pytest.fixture
def signs(monkeypatch):
    """Stand in for the signing, and record what was asked for."""
    calls = []

    def _sign(**kwargs):
        calls.append(kwargs)
        return "signed.jwt.value", kwargs["dl_settings"]["deep_link_return_url"]

    monkeypatch.setattr(lti_router, "sign_deep_link_response", _sign)
    return calls


def _select(client, handle, app_id):
    return client.post(
        "/lti/deep-link/select", json={"handle": handle, "appId": str(app_id)}
    )


# ================================================================
# SELECTING
# ================================================================
def test_a_pick_becomes_a_signed_content_item(client, db, mock_user, storage_patch, signs):
    app = _app(db, mock_user)
    handle = _park(mock_user, storage_patch=storage_patch)

    resp = _select(client, handle, app.appId)

    assert resp.status_code == 200
    body = resp.json()
    assert body["jwt"] == "signed.jwt.value"
    assert body["returnUrl"] == RETURN_URL
    assert body["appName"] == "Nextcloud"


def test_the_app_travels_as_a_custom_parameter(client, db, mock_user, storage_patch, signs):
    """That parameter is the whole binding — it comes back on every
    launch from the activity Moodle is about to create."""
    app = _app(db, mock_user)
    handle = _park(mock_user, storage_patch=storage_patch)

    _select(client, handle, app.appId)

    assert signs[0]["custom"] == {"app_id": str(app.appId)}
    assert signs[0]["title"] == "Nextcloud"
    # Taken from the request, not from configuration — one less value
    # that can disagree with the Moodle registration.
    assert signs[0]["url"] == LAUNCH_URL


def test_a_handle_works_exactly_once(client, db, mock_user, storage_patch, signs):
    app = _app(db, mock_user)
    handle = _park(mock_user, storage_patch=storage_patch)

    assert _select(client, handle, app.appId).status_code == 200

    second = _select(client, handle, app.appId)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "lti_deep_link_expired"
    assert len(signs) == 1


def test_an_unknown_handle_is_refused(client, db, mock_user, storage_patch, signs):
    app = _app(db, mock_user)

    resp = _select(client, "never-issued", app.appId)

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "lti_deep_link_expired"
    assert signs == []


def test_a_handle_issued_to_somebody_else_is_refused(
    client, db, mock_user, storage_patch, signs
):
    """Two lecturers adding activities at the same time must not be able
    to answer each other's question."""
    other = User(email="andere@dhbw.de", username="andere", role=UserRole.TEACHER)
    db.add(other)
    db.commit()
    db.refresh(other)

    app = _app(db, mock_user)
    handle = _park(mock_user, storage_patch=storage_patch, user_id=other.userId)

    resp = _select(client, handle, app.appId)

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "lti_deep_link_foreign"
    assert signs == []


def test_an_unknown_app_is_a_404(client, db, mock_user, storage_patch, signs):
    handle = _park(mock_user, storage_patch=storage_patch)

    resp = _select(client, handle, uuid.uuid4())

    assert resp.status_code == 404
    assert signs == []


def test_a_private_app_of_somebody_else_cannot_be_bound(
    client, db, mock_user, storage_patch, signs
):
    """The id alone must not be a way past the visibility rule."""
    owner = User(email="besitzer@dhbw.de", username="besitzer", role=UserRole.TEACHER)
    db.add(owner)
    db.commit()
    db.refresh(owner)
    app = _app(db, owner, name="Geheim", private=True)
    handle = _park(mock_user, storage_patch=storage_patch)

    resp = _select(client, handle, app.appId)

    assert resp.status_code == 403
    assert signs == []


def test_students_cannot_select(student_client, db, mock_student, storage_patch, signs):
    app = _app(db, mock_student)
    handle = _park(mock_student, storage_patch=storage_patch)

    assert _select(student_client, handle, app.appId).status_code == 403
    assert signs == []


def test_the_endpoint_is_off_when_lti_is(client, db, mock_user, storage_patch, monkeypatch):
    monkeypatch.setattr(settings, "LTI_ENABLED", False)
    app = _app(db, mock_user)
    handle = _park(mock_user, storage_patch=storage_patch)

    assert _select(client, handle, app.appId).status_code == 503


# ================================================================
# PARKING THE REQUEST
# ================================================================
def test_a_deep_link_request_sends_staff_to_the_picker(db, mock_user, storage_patch):
    claims = {
        "iss": ISSUER,
        lti_service.CLAIM_DEPLOYMENT_ID: "1",
        lti_service.CLAIM_DL_SETTINGS: {"deep_link_return_url": RETURN_URL},
        lti_service.CLAIM_TARGET_LINK_URI: LAUNCH_URL,
    }

    target = lti_router._deep_link_target(mock_user, claims)

    assert target.startswith(f"{lti_service.TARGET_DEEP_LINK}?dl=")
    handle = target.split("dl=")[1]
    parked = storage_patch[lti_router._deep_link_key(handle)]
    assert parked["user_id"] == str(mock_user.userId)
    assert parked["settings"]["deep_link_return_url"] == RETURN_URL


def test_a_student_reaching_a_deep_link_request_is_not_given_one(
    db, mock_student, storage_patch
):
    """Moodle only offers content selection to course editors, but that
    is Moodle's check. Nothing is parked for a student."""
    target = lti_router._deep_link_target(mock_student, {"iss": ISSUER})

    assert target == lti_service.TARGET_ENVIRONMENTS
    assert storage_patch == {}


# ================================================================
# HONOURING THE BINDING ON A LATER LAUNCH
# ================================================================
def _deployment(db, owner, app, member):
    dep = Deployment(name="Umgebung", userId=owner.userId, appId=app.appId)
    db.add(dep)
    db.flush()
    db.add(UserToDeployment(userId=member.userId, deploymentId=dep.deploymentId))
    db.commit()
    db.refresh(dep)
    return dep


def test_the_bound_app_is_read_back_off_a_launch():
    claims = {lti_service.CLAIM_CUSTOM: {"app_id": "8c5a1f2e-0000-4000-8000-000000000001"}}

    assert lti_service.extract_custom_app_id(claims) == uuid.UUID(
        "8c5a1f2e-0000-4000-8000-000000000001"
    )


@pytest.mark.parametrize(
    "custom",
    [None, {}, {"app_id": ""}, {"app_id": "not-a-uuid"}, {"other": "x"}],
)
def test_a_missing_or_broken_binding_is_simply_ignored(custom):
    """It narrows where a launch lands and grants nothing, so a value
    that no longer parses must not cost anybody their session."""
    claims = {} if custom is None else {lti_service.CLAIM_CUSTOM: custom}

    assert lti_service.extract_custom_app_id(claims) is None


def test_a_bound_launch_opens_that_app_s_environment(db, mock_student, mock_user):
    wanted = _app(db, mock_user, name="Nextcloud")
    other = _app(db, mock_user, name="Jupyter")
    _deployment(db, mock_user, other, mock_student)
    mine = _deployment(db, mock_user, wanted, mock_student)

    target = lti_service.resolve_launch_target(
        db, mock_student, None, app_id=wanted.appId
    )

    assert target == f"{lti_service.TARGET_ENVIRONMENTS}/{mine.deploymentId}"


def test_without_a_binding_two_environments_stay_a_list(db, mock_student, mock_user):
    """The case the binding exists to remove."""
    a = _app(db, mock_user, name="Nextcloud")
    b = _app(db, mock_user, name="Jupyter")
    _deployment(db, mock_user, a, mock_student)
    _deployment(db, mock_user, b, mock_student)

    target = lti_service.resolve_launch_target(db, mock_student, None)

    assert target == lti_service.TARGET_ENVIRONMENTS


def test_a_binding_with_no_matching_environment_lands_on_the_list(
    db, mock_student, mock_user
):
    """Not on an unrelated environment that happened to be the only one.
    Being sent somewhere wrong is worse than being shown a list."""
    bound = _app(db, mock_user, name="Nextcloud")
    other = _app(db, mock_user, name="Jupyter")
    _deployment(db, mock_user, other, mock_student)

    target = lti_service.resolve_launch_target(
        db, mock_student, None, app_id=bound.appId
    )

    assert target == lti_service.TARGET_ENVIRONMENTS
