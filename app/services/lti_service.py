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

import redis
from cryptography.hazmat.primitives import serialization
from pylti1p3.registration import Registration
from pylti1p3.roles import StudentRole, TeacherRole
from pylti1p3.tool_config import ToolConfDict
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import deployments as crud_deployments
from app.models import (
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

# The one IMS claim URI this module reads directly. Everything else is
# either a plain OIDC claim or goes through PyLTI1p3's role helpers.
CLAIM_CONTEXT = "https://purl.imsglobal.org/spec/lti/claim/context"


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


# ----------------------------------------------------------------
# PROVISIONING
# ----------------------------------------------------------------
def _resolve_role(identity: LaunchIdentity, existing: User | None) -> UserRole:
    """Decide the *global* role for a launching user.

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
    """
    if existing is not None and existing.role in (UserRole.ADMIN, UserRole.TEACHER):
        return existing.role
    if identity.is_instructor and settings.LTI_TRUST_INSTRUCTOR_ROLE:
        return UserRole.TEACHER
    return UserRole.STUDENT


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


def record_context(db: Session, identity: LaunchIdentity) -> LtiContext | None:
    """Remember the Moodle course the launch came from.

    Recorded, not mapped: ``courseId`` stays empty. A Moodle course and
    a Studiengruppe are different things, and guessing an equivalence
    would quietly attach people to the wrong group.
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
        )
        db.add(context)
    else:
        if identity.context_title and context.title != identity.context_title:
            context.title = identity.context_title
        if identity.context_label and context.label != identity.context_label:
            context.label = identity.context_label

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


def resolve_launch_target(
    db: Session,
    user: User,
    context: LtiContext | None,
) -> str:
    """The path the frontend should open after a launch.

    A student clicking a Moodle activity wants their environment, not a
    dashboard. Returned as a relative path — the caller hands it to the
    frontend, which refuses anything that is not one.

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
