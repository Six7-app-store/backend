"""LTI 1.3 tool configuration and launch-claim handling.

Split from the router on purpose: the router deals in HTTP, this module
deals in the trust relationship and in turning verified claims into
local rows. :mod:`app.utils.lti_fastapi` sits below both and only
adapts PyLTI1p3 to FastAPI.
"""
from __future__ import annotations

import base64
import binascii
import logging
import typing as t
import uuid

import redis
from cryptography.hazmat.primitives import serialization
from pylti1p3.deep_link import DeepLink
from pylti1p3.deep_link_resource import DeepLinkResource
from pylti1p3.exception import LtiException
from pylti1p3.names_roles import NamesRolesProvisioningService
from pylti1p3.registration import Registration
from pylti1p3.roles import StudentRole, TeacherRole
from pylti1p3.service_connector import ServiceConnector
from pylti1p3.tool_config import ToolConfDict
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import deployments as crud_deployments
from app.models import (
    Course,
    CourseTeacher,
    IdentityProvider,
    LtiContext,
    User,
    UserIdentity,
    UserRole,
)
from app.utils.lti_fastapi import RedisLaunchDataStorage
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# The IMS claim URIs this module reads directly. Everything else is
# either a plain OIDC claim or goes through PyLTI1p3's role helpers.
CLAIM_CONTEXT = "https://purl.imsglobal.org/spec/lti/claim/context"
CLAIM_NRPS = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
CLAIM_CUSTOM = "https://purl.imsglobal.org/spec/lti/claim/custom"
CLAIM_DEPLOYMENT_ID = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
CLAIM_DL_SETTINGS = "https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings"
CLAIM_TARGET_LINK_URI = "https://purl.imsglobal.org/spec/lti/claim/target_link_uri"

# The custom parameter a deep-linked activity carries. Named on both
# sides here so the value written at selection time and the value read
# at launch time cannot drift apart.
CUSTOM_APP_ID = "app_id"


class LtiConfigurationError(RuntimeError):
    """LTI is switched on but the configuration is incomplete."""


class LtiProvisioningError(Exception):
    """A validated launch cannot be turned into a local user.

    Carries the HTTP status the router should answer with, because the
    reasons differ in kind: a missing claim is a bad request, a launch
    aiming at somebody else's privileged account is a refusal.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ----------------------------------------------------------------
# KEYS
# ----------------------------------------------------------------
def _load_private_key_pem() -> str:
    """Decode the configured private key and confirm it parses."""
    raw = settings.LTI_PRIVATE_KEY_B64
    if not raw:
        raise LtiConfigurationError("LTI_PRIVATE_KEY_B64 is not set")
    try:
        pem = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        raise LtiConfigurationError("LTI_PRIVATE_KEY_B64 is not valid base64") from e
    try:
        serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as e:
        raise LtiConfigurationError(
            "LTI_PRIVATE_KEY_B64 does not decode to a PEM private key"
        ) from e
    return pem.decode("utf-8")


def _derive_public_key_pem(private_pem: str) -> str:
    """Derive the public half from the private key.

    Derived rather than configured: ``ToolConfDict.get_jwks()`` builds
    the keyset from the *public* keys only, so a tool that configures
    just the private key serves an empty ``{"keys": []}`` and Moodle
    cannot register it. Storing both would work too, but two values
    that must match are two values that can drift apart.
    """
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


# ----------------------------------------------------------------
# TOOL CONFIGURATION
# ----------------------------------------------------------------
_REQUIRED_SETTINGS = (
    "LTI_PLATFORM_ISSUER",
    "LTI_CLIENT_ID",
    "LTI_DEPLOYMENT_ID",
    "LTI_JWKS_URL",
    "LTI_AUTH_LOGIN_URL",
)


def get_tool_conf() -> ToolConfDict:
    """Build the tool configuration from settings.

    Built per call rather than at import time so a restart is enough to
    pick up changed configuration, and so importing this module never
    fails on an environment without LTI.

    One platform for now. A second Moodle is one more key in the dict —
    at that point the values belong in a table rather than in settings,
    and only this function's body changes.
    """
    missing = [name for name in _REQUIRED_SETTINGS if not getattr(settings, name)]
    if missing:
        raise LtiConfigurationError(
            "LTI is enabled but incomplete; missing: " + ", ".join(missing)
        )

    iss = settings.LTI_PLATFORM_ISSUER
    client_id = settings.LTI_CLIENT_ID
    private_pem = _load_private_key_pem()

    conf = ToolConfDict(
        {
            iss: {
                "default": True,
                "client_id": client_id,
                "auth_login_url": settings.LTI_AUTH_LOGIN_URL,
                "auth_token_url": settings.LTI_TOKEN_URL,
                "auth_audience": None,
                "key_set_url": settings.LTI_JWKS_URL,
                "key_set": None,
                "deployment_ids": [settings.LTI_DEPLOYMENT_ID],
            }
        }
    )
    conf.set_private_key(iss, private_pem, client_id=client_id)
    conf.set_public_key(iss, _derive_public_key_pem(private_pem), client_id=client_id)
    return conf


def get_tool_jwks() -> dict:
    """This tool's public keyset, built from the key alone.

    Deliberately independent of :func:`get_tool_conf`: Moodle reads the
    keyset URL while the tool is being registered, which is exactly when
    ``client_id`` and ``deployment_id`` do not exist yet. Deriving the
    keyset from the configuration would make registration impossible —
    the endpoint would 503 until it had the values that registration is
    supposed to produce.
    """
    public_pem = _derive_public_key_pem(_load_private_key_pem())
    return {"keys": [Registration.get_jwk(public_pem)]}


_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(settings.LTI_REDIS_URL)
    return _redis_client


def get_launch_storage() -> RedisLaunchDataStorage:
    """Storage for ``state`` and ``nonce`` between login and launch."""
    return RedisLaunchDataStorage(_get_redis())


# ----------------------------------------------------------------
# CLAIMS
# ----------------------------------------------------------------
class LaunchIdentity(t.NamedTuple):
    issuer: str
    subject: str
    email: str | None
    first_name: str | None
    last_name: str | None
    display_name: str | None
    is_instructor: bool
    context_id: str | None
    context_title: str | None
    context_label: str | None


def extract_identity(claims: t.Mapping[str, t.Any]) -> LaunchIdentity:
    """Pull the fields we care about out of a *validated* launch.

    Only ever call this with claims from ``MessageLaunch.validate()`` —
    nothing here re-checks anything.
    """
    context = claims.get(CLAIM_CONTEXT) or {}
    return LaunchIdentity(
        issuer=claims.get("iss", ""),
        subject=claims.get("sub", ""),
        email=claims.get("email"),
        first_name=claims.get("given_name"),
        last_name=claims.get("family_name"),
        display_name=claims.get("name"),
        # TeacherRole matches Instructor and any Administrator role,
        # including a Moodle site admin. Harmless here: the global
        # role is decided by _resolve_role, which never grants ADMIN.
        is_instructor=TeacherRole(claims).check(),
        context_id=context.get("id"),
        context_title=context.get("title"),
        context_label=context.get("label"),
    )


def context_role_label(claims: t.Mapping[str, t.Any]) -> str:
    """The launch role as a short label, for the session token."""
    if TeacherRole(claims).check():
        return "instructor"
    if StudentRole(claims).check():
        return "learner"
    return "unknown"


def extract_custom_app_id(claims: t.Mapping[str, t.Any]) -> uuid.UUID | None:
    """The app a deep-linked activity was bound to, if any.

    Written into the Moodle activity when the lecturer picked it, and
    handed back on every launch from that activity. It narrows where the
    launch lands; it grants nothing, so a value that no longer parses or
    no longer exists is simply ignored — the launch then falls back to
    the same guess an unbound activity gets.

    Custom parameters are strings by specification, and the platform
    passes them through unchecked, so the value is parsed defensively.
    """
    custom = claims.get(CLAIM_CUSTOM) or {}
    raw = custom.get(CUSTOM_APP_ID)
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        logger.info("Deep-link custom %s is not a uuid: %r", CUSTOM_APP_ID, raw)
        return None


def sign_deep_link_response(
    *,
    issuer: str,
    deployment_id: str,
    dl_settings: t.Mapping[str, t.Any],
    title: str,
    url: str | None,
    custom: t.Mapping[str, str],
) -> tuple[str, str]:
    """Build the signed answer Moodle expects back from a selection.

    Returns ``(jwt, return_url)``. The caller posts the first to the
    second as a form — the platform will not accept it any other way,
    and it must arrive from the browser rather than from us, because
    ``deep_link_return_url`` is where *Moodle's* session lives.

    ``url`` is the address the created activity will launch. Taken from
    the request's ``target_link_uri`` rather than from configuration:
    Moodle just told us where it launches this tool, and a second copy
    in the environment is a second thing that can disagree with the
    registration.
    """
    tool_conf = get_tool_conf()
    try:
        registration = tool_conf.find_registration_by_params(
            issuer, settings.LTI_CLIENT_ID
        )
    except LtiException as e:
        raise LtiConfigurationError(f"No registration for platform {issuer}") from e

    return_url = dl_settings.get("deep_link_return_url")
    if not return_url:
        # Without it there is nowhere to send the answer. The platform
        # is required to supply it, so this is a broken request.
        raise LtiConfigurationError("deep_linking_settings has no return url")

    resource = (
        DeepLinkResource()
        .set_type("ltiResourceLink")
        .set_title(title)
        .set_custom_params(dict(custom))
    )
    if url:
        resource.set_url(url)

    deep_link = DeepLink(registration, deployment_id, dict(dl_settings))
    return deep_link.get_response_jwt([resource]), return_url


def extract_memberships_url(claims: t.Mapping[str, t.Any]) -> str | None:
    """The NRPS endpoint for this launch's course, if the platform sent one.

    Kept separate from :func:`extract_identity` because it says nothing
    about the person — it is a property of the course the launch came
    from, and it is stored on the context for exactly that reason.

    Absent whenever the platform does not offer the service or the
    administrator did not enable it for this tool. That is a normal
    state, not an error: only the roster import needs it.
    """
    service = claims.get(CLAIM_NRPS) or {}
    url = service.get("context_memberships_url")
    return url or None


# ----------------------------------------------------------------
# PROVISIONING
# ----------------------------------------------------------------
def _global_role(is_instructor: bool, existing: User | None) -> UserRole:
    """Decide the *global* role for someone arriving from the platform.

    Two rules, both deliberate:

    * A Moodle course role never lowers an existing account. An admin
      who happens to open the app store from a course they are enrolled
      in as a student stays an admin.
    * Instructor only grants the global TEACHER role when an
      administrator has switched that on for this deployment
      (``LTI_TRUST_INSTRUCTOR_ROLE``). The teacher role governs foreign
      OpenStack resources, so handing it out to whoever is a trainer in
      some Moodle course is not something the platform gets to decide.
      Nothing in LTI ever grants ADMIN.

    Shared by the launch and the roster import on purpose: a person must
    not end up with a different role depending on whether they clicked
    the activity themselves or were pulled in from a member list.
    """
    if existing is not None and existing.role in (UserRole.ADMIN, UserRole.TEACHER):
        return existing.role
    if is_instructor and settings.LTI_TRUST_INSTRUCTOR_ROLE:
        return UserRole.TEACHER
    return UserRole.STUDENT


def _resolve_role(identity: LaunchIdentity, existing: User | None) -> UserRole:
    """The global role for a launching user. See :func:`_global_role`."""
    return _global_role(identity.is_instructor, existing)


def _email_is_taken(db: Session, email: str) -> bool:
    """Whether an account already holds this address.

    Matching a launch to an existing account by e-mail is not possible
    safely. The address in an LTI token is whatever the Moodle profile
    says, and Moodle lets users edit their own — the signature proves
    that Moodle relayed the claim, never that the claim is true. An
    ordinary Moodle user who types a classmate's address into their
    profile would otherwise be handed that classmate's account, with
    its OpenStack credentials, deployments and team memberships.

    So a taken address ends the launch. The caller turns this into a
    link challenge: the account's owner signs in directly once and
    spends it, and only that step — not the claim — creates the link.
    """
    return db.query(User).filter(User.email == email).first() is not None


def provision_user(db: Session, identity: LaunchIdentity) -> User:
    """Find or create the local user behind a launch.

    Lookup order:

    1. The ``(provider, issuer, subject)`` identity. This is the only
       identifier the platform guarantees to be stable, and the only
       thing that ever signs somebody into an existing account.
    2. Otherwise a new user — unless the address is already taken, in
       which case the launch is refused and the caller offers a link
       challenge. See :func:`_email_is_taken`.
    """
    if not identity.subject:
        raise LtiProvisioningError(
            "lti_missing_sub", "launch has no sub claim", status_code=400
        )

    link = (
        db.query(UserIdentity)
        .filter(
            UserIdentity.provider == IdentityProvider.LTI,
            UserIdentity.issuer == identity.issuer,
            UserIdentity.subject == identity.subject,
        )
        .first()
    )

    user: User | None = link.user if link else None

    if user is None and identity.email and _email_is_taken(db, identity.email):
        logger.warning(
            "LTI launch refused: email %s already belongs to an account and "
            "the launching identity (iss=%s, sub=%s) is not linked to it",
            identity.email,
            identity.issuer,
            identity.subject,
        )
        raise LtiProvisioningError(
            "lti_link_required",
            "This e-mail address already belongs to an account. Sign in "
            "directly once to confirm it is yours — the Moodle account "
            "is linked from there.",
            status_code=403,
        )

    if user is None:
        if not identity.email:
            # Without an address the account cannot receive deployment
            # credentials, and a placeholder would collide on the
            # unique index. The fix is in Moodle's tool privacy
            # settings, so say so.
            raise LtiProvisioningError(
                "lti_missing_email",
                "launch has no email claim — enable e-mail sharing in the "
                "Moodle tool's privacy settings",
                status_code=400,
            )
        user = User(
            email=identity.email,
            username=identity.email,
            firstName=identity.first_name,
            lastName=identity.last_name,
            role=_resolve_role(identity, None),
        )
        db.add(user)
        db.flush()
    else:
        # Keep the record current, but never downgrade the role — and
        # never follow the e-mail claim. The address on the account is
        # where deployment credentials are mailed; a Moodle profile
        # field the launching person can edit must not be able to
        # redirect that mail.
        if identity.first_name and user.firstName != identity.first_name:
            user.firstName = identity.first_name
        if identity.last_name and user.lastName != identity.last_name:
            user.lastName = identity.last_name
        new_role = _resolve_role(identity, user)
        if user.role != new_role:
            user.role = new_role

    if link is None:
        link = UserIdentity(
            userId=user.userId,
            provider=IdentityProvider.LTI,
            issuer=identity.issuer,
            subject=identity.subject,
        )
        db.add(link)
    link.last_login_at = utcnow()

    db.commit()
    db.refresh(user)
    return user


def record_context(
    db: Session,
    identity: LaunchIdentity,
    *,
    memberships_url: str | None = None,
) -> LtiContext | None:
    """Remember the Moodle course the launch came from.

    Recorded, not mapped: ``courseId`` stays empty. A Moodle course and
    a Studiengruppe are different things, and guessing an equivalence
    would quietly attach people to the wrong group.

    ``memberships_url`` is refreshed on every launch and only ever
    overwritten with a value. A platform that stops sending the claim —
    because an administrator switched the service off — leaves the old
    URL in place, where it fails loudly at call time rather than
    silently turning the import into a no-op.
    """
    if not identity.context_id:
        return None

    context = (
        db.query(LtiContext)
        .filter(
            LtiContext.issuer == identity.issuer,
            LtiContext.context_id == identity.context_id,
        )
        .first()
    )
    if context is None:
        context = LtiContext(
            issuer=identity.issuer,
            context_id=identity.context_id,
            title=identity.context_title,
            label=identity.context_label,
            memberships_url=memberships_url,
        )
        db.add(context)
    else:
        if identity.context_title and context.title != identity.context_title:
            context.title = identity.context_title
        if identity.context_label and context.label != identity.context_label:
            context.label = identity.context_label
        if memberships_url and context.memberships_url != memberships_url:
            context.memberships_url = memberships_url

    db.commit()
    db.refresh(context)
    return context


# ----------------------------------------------------------------
# WHERE A LAUNCH LANDS
# ----------------------------------------------------------------
# Frontend routes. Kept here as constants so the launch and its tests
# name the same strings, and so a route rename shows up in one place.
TARGET_DASHBOARD = "/dashboard"
TARGET_ENVIRONMENTS = "/deployments"
TARGET_MAP_COURSE = "/lti/kurs-zuordnen"
TARGET_DEEP_LINK = "/lti/auswahl"


def resolve_launch_target(
    db: Session,
    user: User,
    context: LtiContext | None,
    *,
    app_id: uuid.UUID | None = None,
) -> str:
    """The path the frontend should open after a launch.

    A student clicking a Moodle activity wants their environment, not a
    dashboard. Returned as a relative path — the caller hands it to the
    frontend, which refuses anything that is not one.

    ``app_id`` is set when the activity was deep-linked to one app. It
    replaces guesswork with what the lecturer actually chose, and is the
    strongest of the narrowings here for that reason.

    Staff land on their environments list, except when the Moodle course
    has no mapping yet: then they are sent to the page that creates one,
    because they are the only ones who can.

    For a student the candidate set is every environment they are a
    member of. When the Moodle course *is* mapped, it narrows that set
    to environments owned by a teacher of the mapped course. Exactly one
    survivor means we can open it directly; anything else falls back to
    the list, which is honest rather than a guess.

    Never raises. A launch that cannot be resolved still has to end in a
    working session, so every failure path degrades to a valid page.
    """
    is_staff = user.role in (UserRole.TEACHER, UserRole.ADMIN)

    if is_staff:
        if context is not None and context.courseId is None:
            return f"{TARGET_MAP_COURSE}?context={context.ltiContextId}"
        return TARGET_ENVIRONMENTS

    try:
        candidates = crud_deployments.get_deployments(
            db, limit=100, member_user_id=user.userId
        )
    except Exception:
        # A broken lookup must not cost the user their session — they
        # are signed in either way, they just land a page earlier.
        logger.exception("Could not resolve launch target for user %s", user.userId)
        return TARGET_DASHBOARD

    if app_id is not None:
        # The activity was deep-linked to one app, so the lecturer has
        # already said what this click is about. Unlike the course
        # mapping below this narrowing is not undone when it empties the
        # set: an empty result means the student has no environment of
        # that app, and sending them into an unrelated one because it
        # happened to be their only one would be worse than the list.
        candidates = [d for d in candidates if d.appId == app_id]

    if context is not None and context.courseId is not None:
        teacher_ids = {
            row[0]
            for row in db.query(CourseTeacher.userId)
            .filter(CourseTeacher.courseId == context.courseId)
            .all()
        }
        # Only narrow when the mapping actually resolves to teachers.
        # A mapped course with no teacher rows would otherwise empty the
        # set and send an enrolled student to an empty list.
        if teacher_ids:
            narrowed = [d for d in candidates if d.userId in teacher_ids]
            if narrowed:
                candidates = narrowed

    if len(candidates) == 1:
        return f"{TARGET_ENVIRONMENTS}/{candidates[0].deploymentId}"
    return TARGET_ENVIRONMENTS


# ----------------------------------------------------------------
# ROSTER IMPORT
# ----------------------------------------------------------------
# Reading a course's member list is the one thing the tool asks the
# platform for, rather than being told. It runs on its own request,
# long after the launch that recorded the context, so everything it
# needs is read from the stored context and the tool configuration.
NRPS_SCOPE = "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly"

# Role values in an NRPS membership. The specification sends full URIs,
# Moodle sends the bare suffix; both forms appear in the wild, so the
# comparison is on the last path segment either way.
_INSTRUCTOR_ROLES = frozenset({"Instructor", "Administrator"})

# Why a member did not become a local account. Every one of these is a
# normal outcome, not a failure: the import reports them so the person
# who triggered it can act, instead of silently doing something clever.
SKIP_NO_SUBJECT = "no_subject"
SKIP_NO_EMAIL = "no_email"
SKIP_LINK_REQUIRED = "link_required"
SKIP_OTHER_GROUP = "already_in_another_group"
SKIP_INSTRUCTOR_NOT_TRUSTED = "instructor_not_trusted"


class LtiRosterError(Exception):
    """The platform's member list could not be read.

    Separate from :class:`LtiProvisioningError` because the failure is
    on the far side of the connection: the tool is fine, the platform
    said no or could not be reached.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class SkippedMember(t.NamedTuple):
    """A member the import deliberately left alone, and why."""

    name: str | None
    email: str | None
    reason: str


class RosterImport(t.NamedTuple):
    """What one import did. Counts are of members, not of rows."""

    course: Course
    created: int
    matched: int
    teachers: int
    students: int
    skipped: list[SkippedMember]


def _role_suffix(role: str) -> str:
    """The bare role name, whether it arrived as a URI or on its own."""
    return role.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _member_is_instructor(member: t.Mapping[str, t.Any]) -> bool:
    return any(_role_suffix(r) in _INSTRUCTOR_ROLES for r in member.get("roles") or [])


def _member_is_active(member: t.Mapping[str, t.Any]) -> bool:
    """Whether this membership is current.

    The status is optional in the specification. Absent means active —
    a platform that does not track enrolment states sends no status at
    all, and treating that as inactive would import nobody.
    """
    status = member.get("status")
    return status is None or status == "Active"


def fetch_context_members(context: LtiContext) -> list[dict]:
    """Read the platform's member list for ``context``.

    The access token is fetched with the tool's own key over
    ``client_credentials``; the platform verifies it against the keyset
    it reads from ``/lti/jwks``. That fetch is the step that fails
    first on a development machine, because the platform has to reach
    the tool — the opposite direction from a launch.
    """
    if not context.memberships_url:
        raise LtiRosterError(
            "lti_nrps_unavailable",
            "This Moodle course was recorded before membership reading was "
            "available, or the platform does not offer it. Open the activity "
            "in Moodle once more, then try again.",
            status_code=409,
        )

    tool_conf = get_tool_conf()
    try:
        registration = tool_conf.find_registration_by_params(
            context.issuer, settings.LTI_CLIENT_ID
        )
    except LtiException as e:
        # The context was recorded under an issuer this tool is no
        # longer configured for.
        raise LtiRosterError(
            "lti_unknown_platform",
            f"No registration for platform {context.issuer}",
            status_code=409,
        ) from e

    service = NamesRolesProvisioningService(
        ServiceConnector(registration),
        {
            "context_memberships_url": context.memberships_url,
            "service_versions": ["2.0"],
        },
    )

    try:
        return service.get_members()
    except LtiException as e:
        logger.warning("NRPS call failed for context %s: %s", context.ltiContextId, e)
        raise LtiRosterError(
            "lti_nrps_failed",
            "Moodle refused the member list. Check that the tool is allowed "
            "to read course members.",
        ) from e
    except Exception as e:
        # Connection refused, DNS, TLS — anything below the protocol.
        logger.exception("NRPS call errored for context %s", context.ltiContextId)
        raise LtiRosterError(
            "lti_nrps_unreachable", "Moodle could not be reached for the member list."
        ) from e


def _course_name(context: LtiContext, override: str | None) -> str:
    """What to call the Studiengruppe this import creates."""
    for candidate in (override, context.title, context.label):
        if candidate and candidate.strip():
            return candidate.strip()
    return f"Moodle-Kurs {context.context_id}"


def _member_user(
    db: Session,
    context: LtiContext,
    member: t.Mapping[str, t.Any],
) -> tuple[User | None, str | None, bool]:
    """Find or create the local account behind one roster entry.

    Returns ``(user, None, created)`` on success and
    ``(None, reason, False)`` when the member was deliberately left
    alone. ``created`` distinguishes a fresh account from one that was
    already linked, which is the only difference the caller counts.

    The lookup is by ``(provider, issuer, user_id)`` and nothing else.
    ``user_id`` in a membership is the same value a launch sends as
    ``sub``, which is the only identifier the platform guarantees to be
    stable — and, unlike the address, not something the member can edit
    in their own profile.

    An address that already belongs to an account therefore does *not*
    match: it would hand that account, its OpenStack credentials and its
    deployments to whoever typed the address into Moodle. Those members
    are reported instead, and the ordinary link challenge remains the
    one way to join the two.
    """
    subject = str(member.get("user_id") or "").strip()
    if not subject:
        # Without the platform's own identifier there is nothing stable
        # to key an account on, and the address is not a substitute.
        return None, SKIP_NO_SUBJECT, False

    link = (
        db.query(UserIdentity)
        .filter(
            UserIdentity.provider == IdentityProvider.LTI,
            UserIdentity.issuer == context.issuer,
            UserIdentity.subject == subject,
        )
        .first()
    )
    if link is not None:
        return link.user, None, False

    email = (member.get("email") or "").strip()
    if not email:
        # No address means no way to send deployment credentials, and a
        # placeholder would collide on the unique index.
        return None, SKIP_NO_EMAIL, False
    if _email_is_taken(db, email):
        return None, SKIP_LINK_REQUIRED, False

    user = User(
        email=email,
        username=email,
        firstName=member.get("given_name"),
        lastName=member.get("family_name"),
        role=_global_role(_member_is_instructor(member), None),
    )
    db.add(user)
    db.flush()
    db.add(
        UserIdentity(
            userId=user.userId,
            provider=IdentityProvider.LTI,
            issuer=context.issuer,
            subject=subject,
        )
    )
    return user, None, True


def import_context_roster(
    db: Session,
    context: LtiContext,
    actor: User,
    *,
    name: str | None = None,
) -> RosterImport:
    """Create the Studiengruppe behind a Moodle course and fill it.

    This is the one place that writes ``LtiContext.courseId`` without a
    human naming an existing Studiengruppe — and it is still the human's
    decision: the endpoint behind it exists only to be pressed, by
    somebody who teaches the course, on a course that has no mapping
    yet. What is automated is the typing, not the judgement.

    Members become accounts by the rules in :func:`_member_user`.
    Instructors additionally get a ``course_teachers`` row, which stays
    inert until their global role is TEACHER — ``is_course_teacher_id``
    gates on the role first, so the row can never be a way around
    ``LTI_TRUST_INSTRUCTOR_ROLE``.

    Learners are attached to the new Studiengruppe only when they are in
    none yet. Somebody already in another group stays there and is
    reported: a Moodle enrolment is not grounds for moving a person out
    of the group their studies actually place them in.
    """
    members = fetch_context_members(context)

    course = Course(name=_course_name(context, name))
    db.add(course)
    db.flush()

    created = matched = teachers = students = 0
    skipped: list[SkippedMember] = []

    # The person pressing the button teaches this course, exactly as in
    # ``POST /courses/``. Admins are left out there too: their rights are
    # role-shaped and need no course-scoped row.
    #
    # Tracked in a set rather than re-queried: the rows below are still
    # pending in the session, and the actor is normally *also* in the
    # roster as an instructor — which is precisely the collision the
    # primary key would reject.
    teacher_ids: set = set()
    if actor.role == UserRole.TEACHER:
        db.add(CourseTeacher(courseId=course.courseId, userId=actor.userId))
        teacher_ids.add(actor.userId)

    for member in members:
        if not _member_is_active(member):
            continue

        user, reason, is_new = _member_user(db, context, member)
        if user is None:
            skipped.append(
                SkippedMember(
                    name=member.get("name"),
                    email=member.get("email"),
                    reason=reason or SKIP_NO_SUBJECT,
                )
            )
            continue

        if is_new:
            created += 1
        else:
            matched += 1

        if _member_is_instructor(member):
            if user.role == UserRole.TEACHER:
                if user.userId not in teacher_ids:
                    db.add(CourseTeacher(courseId=course.courseId, userId=user.userId))
                    teacher_ids.add(user.userId)
                teachers += 1
            else:
                # An instructor in Moodle whose global role this
                # deployment does not grant. Nothing is written for
                # them; saying so is more useful than a silent demotion.
                skipped.append(
                    SkippedMember(
                        name=member.get("name"),
                        email=member.get("email"),
                        reason=SKIP_INSTRUCTOR_NOT_TRUSTED,
                    )
                )
            continue

        if user.courseId is None:
            user.courseId = course.courseId
            students += 1
        elif user.courseId != course.courseId:
            skipped.append(
                SkippedMember(
                    name=member.get("name"),
                    email=member.get("email"),
                    reason=SKIP_OTHER_GROUP,
                )
            )
        else:
            students += 1

    context.courseId = course.courseId

    db.commit()
    db.refresh(course)
    db.refresh(context)

    logger.info(
        "Roster import for Moodle course (iss=%s, context=%s) by user %s: "
        "course %s, %d created, %d matched, %d teachers, %d students, %d skipped",
        context.issuer,
        context.context_id,
        actor.userId,
        course.courseId,
        created,
        matched,
        teachers,
        students,
        len(skipped),
    )

    return RosterImport(
        course=course,
        created=created,
        matched=matched,
        teachers=teachers,
        students=students,
        skipped=skipped,
    )
