"""LTI 1.3 endpoints — the Moodle launch.

Three endpoints, in the order a launch touches them:

``GET  /lti/jwks``    this tool's public keyset, read by Moodle
``POST /lti/login``   OIDC third-party login initiation
``POST /lti/launch``  the signed id_token arrives and is verified
``POST /lti/link``    claims a Moodle identity for the signed-in account

Two more sit behind a launch rather than in it, both about the Moodle
course a launch came from:

``PUT  /lti/contexts/{id}``         attach it to an existing Studiengruppe
``POST /lti/contexts/{id}/import``  make the Studiengruppe from its roster

``POST /lti/launch`` also answers a *second* message type. A deep-linking
request means Moodle is not opening the tool but asking what the activity
being created should point at; the answer is signed by

``POST /lti/deep-link/select``      bind the activity to one app

The sequence looks roundabout — Moodle calls us, we redirect back to
Moodle, Moodle posts to us — and that is the point: the tool has to
start the exchange so it can issue the ``nonce`` that must come back
inside the token. Without it a captured token could simply be replayed.
"""
from __future__ import annotations

import logging
import typing as t
import uuid
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from pylti1p3.exception import LtiException, OIDCException
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

from app.config import settings
from app.database import get_db
from app.models import (
    App,
    Course,
    IdentityProvider,
    LtiContext,
    User,
    UserIdentity,
    UserRole,
)
from app.services.lti_service import (
    CLAIM_DEPLOYMENT_ID,
    CLAIM_DL_SETTINGS,
    CLAIM_TARGET_LINK_URI,
    CUSTOM_APP_ID,
    TARGET_DEEP_LINK,
    TARGET_ENVIRONMENTS,
    LtiConfigurationError,
    LtiProvisioningError,
    LtiRosterError,
    context_role_label,
    extract_custom_app_id,
    extract_identity,
    extract_memberships_url,
    get_launch_storage,
    get_tool_conf,
    get_tool_jwks,
    import_context_roster,
    provision_user,
    record_context,
    resolve_launch_target,
    sign_deep_link_response,
)
from app.utils.auth import get_current_keycloak_user, get_current_user
from app.utils.capabilities import (
    ensure_edit_course,
    ensure_view_app,
    ensure_view_course_detail,
)
from app.utils.lti_fastapi import (
    FastApiMessageLaunch,
    FastApiOIDCLogin,
    FastApiRequest,
)
from app.utils.lti_session import (
    create_link_challenge,
    create_session_token,
    decode_link_challenge,
)
from app.utils.permissions import require_staff
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

router = APIRouter()


class LtiLinkRequest(BaseModel):
    challenge: str = Field(min_length=1)


class LtiContextMapRequest(BaseModel):
    """Which local course a Moodle course belongs to. ``None`` unmaps."""

    courseId: UUID | None = None


class LtiContextResponse(BaseModel):
    ltiContextId: UUID
    issuer: str
    context_id: str
    title: str | None = None
    label: str | None = None
    courseId: UUID | None = None

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------
def _challenge_key(jti: str) -> str:
    """Where the one-shot marker for a challenge lives.

    The launch storage is reused rather than a second Redis client:
    ``check_value`` there consumes the key atomically, which is exactly
    the property a single-use ticket needs.
    """
    return f"link-challenge-{jti}"


def _link_required(identity) -> RedirectResponse:
    """Turn the refusal into a page the person can act on.

    A launch is a form POST from Moodle landing in a fresh browsing
    context, so whatever comes back is what the person sees. A JSON 403
    would be a dead end; the redirect carries the challenge to a
    frontend route that explains the situation and walks them through
    the direct sign-in.

    The challenge is issued here rather than in the service because it
    is a transport concern — the service only decides that the launch
    cannot be signed in. It grants nothing by itself: spending it needs
    a direct login, which is the whole point of the detour.
    """
    jti = uuid.uuid4().hex
    challenge = create_link_challenge(
        issuer=identity.issuer,
        subject=identity.subject,
        email=identity.email,
        jti=jti,
    )
    get_launch_storage().set_value(
        _challenge_key(jti),
        True,
        exp=settings.LTI_LINK_CHALLENGE_TTL_MINUTES * 60,
    )
    separator = "&" if "?" in settings.LTI_LINK_REDIRECT_URL else "?"
    location = (
        f"{settings.LTI_LINK_REDIRECT_URL}{separator}"
        f"{urlencode({'challenge': challenge})}"
    )
    return RedirectResponse(location, status_code=302)


def _deep_link_key(handle: str) -> str:
    """Where a pending deep-link selection is parked."""
    return f"deep-link-{handle}"


def _deep_link_target(user: User, claims: t.Mapping[str, t.Any]) -> str:
    """Park what the selection will need and point at the picker.

    Moodle's return URL and the settings that go with it are handed to
    us once, inside a message that is verified here and gone afterwards.
    The lecturer then spends time choosing, on a separate request that
    carries none of it — so it is stored server-side under a random
    handle rather than sent through the browser. What travels in the URL
    is the handle alone, and it is useless to anyone but its owner: the
    selection endpoint checks that the caller is the user it was issued
    to, and spends it on use.

    Staff only. Moodle offers content selection to course editors, but
    that is Moodle's check, not ours — a student who reaches this lands
    on their environments instead.
    """
    if user.role not in (UserRole.TEACHER, UserRole.ADMIN):
        logger.warning(
            "Deep-link request from non-staff user %s — sending them to their "
            "environments instead",
            user.userId,
        )
        return TARGET_ENVIRONMENTS

    handle = uuid.uuid4().hex
    get_launch_storage().set_value(
        _deep_link_key(handle),
        {
            "iss": claims.get("iss"),
            "deployment_id": claims.get(CLAIM_DEPLOYMENT_ID),
            "settings": claims.get(CLAIM_DL_SETTINGS) or {},
            "target_link_uri": claims.get(CLAIM_TARGET_LINK_URI),
            "user_id": str(user.userId),
        },
        exp=settings.LTI_DEEP_LINK_TTL_MINUTES * 60,
    )
    return f"{TARGET_DEEP_LINK}?dl={handle}"


def _require_lti_enabled() -> None:
    if not settings.LTI_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "lti_disabled", "message": "LTI is not enabled"},
        )


def _tool_conf():
    """Tool configuration, or a 503 that says what is missing."""
    try:
        return get_tool_conf()
    except LtiConfigurationError as e:
        # Misconfiguration, not a bad request — say so in the log and
        # keep the detail generic for the caller.
        logger.error("LTI configuration incomplete: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "lti_misconfigured", "message": str(e)},
        ) from e


async def _read_params(request: StarletteRequest) -> dict[str, t.Any]:
    """Collect request parameters for PyLTI1p3.

    The library reads parameters synchronously and cannot await, so the
    form has to be parsed here and handed over as a plain dict.
    """
    params: dict[str, t.Any] = dict(request.query_params)
    if request.method == "POST":
        form = await request.form()
        params.update(dict(form.items()))
    return params


# ----------------------------------------------------------------
# JWKS
# ----------------------------------------------------------------
@router.get("/jwks")
def lti_jwks() -> dict:
    """This tool's public keys, in JWKS form.

    Moodle fetches this while the tool is being registered and whenever
    it verifies a message we signed. Public by definition — it contains
    only public keys.

    Needs nothing but the key itself, so it answers before the platform
    values from registration exist. That ordering is the point: Moodle
    reads this URL to complete the very registration that produces them.
    """
    _require_lti_enabled()
    try:
        return get_tool_jwks()
    except LtiConfigurationError as e:
        logger.error("LTI key unusable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "lti_misconfigured", "message": str(e)},
        ) from e


# ----------------------------------------------------------------
# LOGIN INITIATION
# ----------------------------------------------------------------
@router.api_route("/login", methods=["GET", "POST"])
async def lti_login(request: StarletteRequest) -> Response:
    """Step 1: start the OIDC handshake and redirect back to Moodle.

    Registered for both verbs because the specification allows either
    and platforms differ; Moodle uses POST.
    """
    _require_lti_enabled()
    tool_conf = _tool_conf()

    params = await _read_params(request)
    lti_request = FastApiRequest(request, form_data=params)

    target = params.get("target_link_uri")
    if not target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "lti_missing_target", "message": "target_link_uri missing"},
        )

    oidc_login = FastApiOIDCLogin(
        lti_request,
        tool_conf,
        launch_data_storage=get_launch_storage(),
    )

    try:
        # enable_check_cookies() inserts an interstitial that verifies
        # cookies actually survive the round-trip. Without it, a blocked
        # third-party cookie surfaces much later as an unexplained
        # "state not found" on the launch.
        return oidc_login.enable_check_cookies().redirect(target)
    except (OIDCException, LtiException) as e:
        logger.warning("LTI login initiation failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "lti_login_failed", "message": str(e)},
        ) from e


# ----------------------------------------------------------------
# LAUNCH
# ----------------------------------------------------------------
@router.post("/launch")
async def lti_launch(
    request: StarletteRequest,
    db: Session = Depends(get_db),
) -> Response:
    """Step 2: verify the id_token, then sign the user in.

    ``validate()`` checks the signature against Moodle's published key
    plus ``iss``, ``aud``, ``exp``, ``nonce``, ``state`` and
    ``deployment_id``. Everything after it runs on verified data —
    nothing is written before that call returns.
    """
    _require_lti_enabled()
    tool_conf = _tool_conf()

    params = await _read_params(request)
    lti_request = FastApiRequest(request, form_data=params)

    message_launch = FastApiMessageLaunch(
        lti_request,
        tool_conf,
        launch_data_storage=get_launch_storage(),
    )

    try:
        message_launch.validate()
    except LtiException as e:
        # The library knows exactly which check failed; without this log
        # the 401 is unattributable and debugging costs hours.
        logger.warning("LTI launch rejected: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "lti_launch_invalid", "message": "LTI launch validation failed"},
        ) from e
    except Exception as e:
        # Not every rejection arrives as an LtiException: a launch from
        # an unregistered platform fails inside the tool-config lookup,
        # which raises a bare Exception. That is still a rejected
        # launch, not a server fault, so it must not surface as a 500 —
        # but it is logged with a traceback so a genuine defect in here
        # stays visible.
        logger.error("LTI launch failed during validation", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "lti_launch_invalid", "message": "LTI launch validation failed"},
        ) from e

    claims = message_launch.get_launch_data()
    identity = extract_identity(claims)

    try:
        user = provision_user(db, identity)
    except LtiProvisioningError as e:
        logger.warning("LTI launch could not be provisioned: %s", e.message)
        if e.code == "lti_link_required":
            return _link_required(identity)
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        ) from e

    context = record_context(
        db, identity, memberships_url=extract_memberships_url(claims)
    )

    try:
        token = create_session_token(
            user.userId,
            context_id=identity.context_id,
            context_role=context_role_label(claims),
        )
    except RuntimeError as e:
        # No signing secret configured. The launch itself was fine, so
        # this is an operator problem, not the caller's — 503, not 500,
        # and no stack trace in the response.
        logger.error("Cannot issue LTI session token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "lti_misconfigured", "message": str(e)},
        ) from e

    logger.info(
        "LTI launch accepted for user %s (iss=%s, context=%s)",
        user.userId,
        identity.issuer,
        identity.context_id,
    )

    # The token travels in the URL because this is a cross-site POST
    # landing in a fresh browsing context — a cookie set here would be
    # a third-party cookie and is dropped in exactly the frame case
    # this has to work in. The frontend takes the token out of the URL
    # and clears it from the history entry.
    # Where this launch should land. A student clicking a Moodle
    # activity wants their environment, not a dashboard they then have
    # to navigate out of.
    #
    # A deep-linking request is a different message type on the same
    # endpoint: Moodle is not opening the tool, it is asking what the
    # activity being created should point at. It gets the picker.
    if message_launch.is_deep_link_launch():
        target = _deep_link_target(user, claims)
    else:
        target = resolve_launch_target(
            db, user, context, app_id=extract_custom_app_id(claims)
        )

    separator = "&" if "?" in settings.LTI_LAUNCH_REDIRECT_URL else "?"
    location = (
        f"{settings.LTI_LAUNCH_REDIRECT_URL}{separator}"
        f"{urlencode({'token': token, 'target': target})}"
    )
    return RedirectResponse(location, status_code=302)


# ----------------------------------------------------------------
# LINK
# ----------------------------------------------------------------
@router.post("/link")
def lti_link(
    payload: LtiLinkRequest,
    user: User = Depends(get_current_keycloak_user),
    db: Session = Depends(get_db),
) -> dict:
    """Claim the Moodle identity a refused launch asked about.

    This is the other half of the refusal in :func:`lti_launch`. The
    challenge proves Moodle signed for that identity; the direct
    sign-in behind this endpoint proves the account belongs to the
    caller. Neither half alone links anything, which is what keeps an
    editable Moodle profile field from reaching a foreign account.

    Spending a challenge is one-shot: the marker is consumed before the
    row is written, so a leaked challenge is worth nothing once used
    and nothing at all after its short lifetime.
    """
    _require_lti_enabled()

    claims = decode_link_challenge(payload.challenge)

    if not get_launch_storage().check_value(_challenge_key(claims["jti"])):
        # Already spent, or expired out of the storage. The token may
        # still verify, so this check is what makes it single-use.
        logger.info("LTI link challenge %s is spent or expired", claims["jti"])
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "lti_link_challenge_spent",
                "message": "This link request was already used. Launch the "
                "activity in Moodle again.",
            },
        )

    issuer, subject = claims["lti_iss"], claims["sub"]
    existing = (
        db.query(UserIdentity)
        .filter(
            UserIdentity.provider == IdentityProvider.LTI,
            UserIdentity.issuer == issuer,
            UserIdentity.subject == subject,
        )
        .first()
    )
    if existing is not None:
        if existing.userId == user.userId:
            return {"status": "already_linked"}
        # The Moodle identity is spoken for. Moving it would hand one
        # person's launches to another account, so it stays put.
        logger.warning(
            "LTI link refused: identity (iss=%s, sub=%s) already belongs to "
            "user %s, not %s",
            issuer,
            subject,
            existing.userId,
            user.userId,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "lti_identity_taken",
                "message": "This Moodle account is already linked to a "
                "different app store account.",
            },
        )

    db.add(
        UserIdentity(
            userId=user.userId,
            provider=IdentityProvider.LTI,
            issuer=issuer,
            subject=subject,
            last_login_at=utcnow(),
        )
    )
    db.commit()
    logger.info(
        "Linked Moodle identity (iss=%s, sub=%s) to user %s", issuer, subject, user.userId
    )
    return {"status": "linked"}


# ----------------------------------------------------------------
# CONTEXT MAPPING
# ----------------------------------------------------------------
def _load_context(db: Session, lti_context_id: UUID) -> LtiContext:
    context = db.get(LtiContext, lti_context_id)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "lti_context_not_found", "message": "Unknown Moodle course"},
        )
    return context


@router.get("/contexts/{lti_context_id}", response_model=LtiContextResponse)
def get_lti_context(
    lti_context_id: UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LtiContext:
    """Read one recorded Moodle course and its mapping.

    Reachable with an LTI session, because the teacher who needs it
    arrives straight from a launch and has no other one.
    """
    _require_lti_enabled()
    ensure_view_course_detail(user)
    return _load_context(db, lti_context_id)


@router.put("/contexts/{lti_context_id}", response_model=LtiContextResponse)
def map_lti_context(
    lti_context_id: UUID,
    payload: LtiContextMapRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LtiContext:
    """Attach a Moodle course to a local course, or detach it.

    A launch records which Moodle course it came from but deliberately
    leaves the mapping empty — a Moodle course and a Studiengruppe are
    different things, and guessing an equivalence attaches people to the
    wrong group. Somebody who teaches the course has to say so, which is
    what this endpoint is.

    The mapping is what lets a student launch resolve to one environment
    instead of a list. It is a narrowing hint, never an access grant:
    every deployment the student then sees still passes the same
    membership checks as through the normal UI.
    """
    _require_lti_enabled()
    context = _load_context(db, lti_context_id)

    if payload.courseId is not None:
        course = db.get(Course, payload.courseId)
        if course is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "course_not_found", "message": "Unknown course"},
            )
        # Rights over the course you are mapping ONTO.
        ensure_edit_course(user, course, db)
    elif context.courseId is not None:
        # Detaching: rights over the course it is currently attached to,
        # so a teacher cannot undo a colleague's mapping.
        current = db.get(Course, context.courseId)
        if current is not None:
            ensure_edit_course(user, current, db)
    else:
        ensure_view_course_detail(user)

    context.courseId = payload.courseId
    db.commit()
    db.refresh(context)
    logger.info(
        "Moodle course (iss=%s, context=%s) mapped to course %s by user %s",
        context.issuer,
        context.context_id,
        context.courseId,
        user.userId,
    )
    return context


# ----------------------------------------------------------------
# ROSTER IMPORT
# ----------------------------------------------------------------
class LtiRosterImportRequest(BaseModel):
    """Optionally override the name of the Studiengruppe being created."""

    name: str | None = Field(default=None, max_length=200)


class LtiRosterSkipResponse(BaseModel):
    """One member the import left alone, and why."""

    name: str | None = None
    email: str | None = None
    reason: str


class LtiRosterImportResponse(BaseModel):
    context: LtiContextResponse
    courseId: UUID
    courseName: str
    created: int
    matched: int
    teachers: int
    students: int
    skipped: list[LtiRosterSkipResponse]


@router.post(
    "/contexts/{lti_context_id}/import",
    response_model=LtiRosterImportResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_lti_context(
    lti_context_id: UUID,
    payload: LtiRosterImportRequest,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
) -> LtiRosterImportResponse:
    """Create the Studiengruppe behind a Moodle course and fill it from it.

    The counterpart to :func:`map_lti_context`: that one attaches a
    Moodle course to a Studiengruppe that already exists, this one makes
    the Studiengruppe when there is none to point at. Both are the same
    decision — that these two things are one — and both need a person to
    make it. Nothing here runs as a side effect of a launch.

    Reachable on an LTI session because the lecturer arrives straight
    from one. Staff only; the member list is read from Moodle with the
    tool's own key, so a student must not be able to trigger it.

    A context that is already mapped is refused rather than given a
    second Studiengruppe — the way to change an existing mapping is the
    ``PUT``, which checks rights on the course being remapped.
    """
    _require_lti_enabled()
    context = _load_context(db, lti_context_id)

    if context.courseId is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "lti_context_already_mapped",
                "message": "This Moodle course is already assigned to a "
                "Studiengruppe.",
            },
        )

    try:
        result = import_context_roster(db, context, user, name=payload.name)
    except LtiConfigurationError as e:
        logger.error("LTI configuration incomplete: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "lti_misconfigured", "message": str(e)},
        ) from e
    except LtiRosterError as e:
        # The platform said no or could not be reached. Nothing was
        # written: the roster is read before the first row is created.
        logger.warning("Roster import failed for context %s: %s", lti_context_id, e.message)
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        ) from e

    return LtiRosterImportResponse(
        context=LtiContextResponse.model_validate(context),
        courseId=result.course.courseId,
        courseName=result.course.name,
        created=result.created,
        matched=result.matched,
        teachers=result.teachers,
        students=result.students,
        skipped=[
            LtiRosterSkipResponse(name=s.name, email=s.email, reason=s.reason)
            for s in result.skipped
        ],
    )


# ----------------------------------------------------------------
# DEEP LINKING
# ----------------------------------------------------------------
class LtiDeepLinkSelectRequest(BaseModel):
    """Which app the activity being created should point at."""

    handle: str = Field(min_length=1, max_length=64)
    appId: UUID


class LtiDeepLinkSelectResponse(BaseModel):
    """The signed answer, and where the browser must post it.

    Deliberately not posted from here: ``returnUrl`` is a Moodle URL
    that authenticates the *lecturer's Moodle session*. Only their
    browser has it.
    """

    jwt: str
    returnUrl: str
    appName: str


@router.post("/deep-link/select", response_model=LtiDeepLinkSelectResponse)
def select_deep_link(
    payload: LtiDeepLinkSelectRequest,
    user: User = Depends(require_staff),
    db: Session = Depends(get_db),
) -> LtiDeepLinkSelectResponse:
    """Turn the lecturer's pick into the content item Moodle asked for.

    Binds the activity to an **app**, not to one environment. Which
    environment a student then opens is resolved per person at launch
    time, from the deployments they are a member of — so the same
    activity works for a course that shares one environment and for a
    course where everybody has their own, and it survives an environment
    being torn down and rebuilt.

    The handle is spent here. A second attempt with the same one is
    refused rather than signing a second content item: Moodle treats the
    response as the answer to one question, and the lecturer starting
    over gets a fresh question anyway.
    """
    _require_lti_enabled()

    storage = get_launch_storage()
    key = _deep_link_key(payload.handle)
    parked = storage.get_value(key)

    if not parked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "lti_deep_link_expired",
                "message": "This selection is no longer open. Add the activity "
                "in Moodle again.",
            },
        )

    if parked.get("user_id") != str(user.userId):
        # The handle is somebody else's. Refusing on ownership keeps a
        # leaked handle from letting a second lecturer answer a question
        # that was never put to them.
        logger.warning(
            "Deep-link handle issued to %s was used by %s",
            parked.get("user_id"),
            user.userId,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "lti_deep_link_foreign",
                "message": "This selection belongs to a different account.",
            },
        )

    app = db.get(App, payload.appId)
    if app is None or app.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "app_not_found", "message": "Unknown app"},
        )
    # The same visibility rule the app list uses. Without it a handle
    # could be pointed at a private app by id alone.
    ensure_view_app(user, app, db=db)

    if not storage.check_value(key):
        # Spent between the read above and here. Only one of two
        # concurrent attempts gets through; the other must not sign.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "lti_deep_link_expired",
                "message": "This selection was already completed.",
            },
        )

    try:
        jwt_value, return_url = sign_deep_link_response(
            issuer=parked.get("iss") or "",
            deployment_id=parked.get("deployment_id") or "",
            dl_settings=parked.get("settings") or {},
            title=app.name,
            url=parked.get("target_link_uri"),
            custom={CUSTOM_APP_ID: str(app.appId)},
        )
    except LtiConfigurationError as e:
        logger.error("Cannot sign deep-link response: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "lti_misconfigured", "message": str(e)},
        ) from e

    logger.info(
        "Deep link selected by user %s: app %s (%s)", user.userId, app.appId, app.name
    )
    return LtiDeepLinkSelectResponse(
        jwt=jwt_value, returnUrl=return_url, appName=app.name
    )
