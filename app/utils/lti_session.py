"""The app store's own session token, issued after an LTI launch.

Moodle's ``id_token`` is the ticket at the door — valid for exactly one
click. Everything after that runs on a token this application issues
itself.

Keycloak is not an option for these sessions. Launched from Moodle the
app store is a third-party frame, and its token renewal runs through a
hidden iframe pointed at Keycloak; browsers block third-party cookies
there, so the renewal fails and the user is bounced out mid-session.

The token is deliberately short-lived and has no refresh: when it
expires the user launches again from Moodle, which costs one click and
re-checks course membership at the same time.
"""
from __future__ import annotations

import logging
import typing as t
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException, status
from jose import JWTError, jwt

from app.config import settings
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# Marks a token as ours. The auth dependency dispatches on the ``iss``
# claim, so this string must not collide with Keycloak's issuer (which
# is always a URL).
SESSION_ISSUER = "appstore-lti"

ALGORITHM = "HS256"

# Both tokens this module issues are signed with the same secret and
# carry the same issuer, so each one names its kind. A verifier accepts
# exactly one: without this, a link challenge — handed out to an
# *unauthenticated* launch — would otherwise pass as a session token.
TYPE_SESSION = "session"
TYPE_LINK_CHALLENGE = "link-challenge"


def _secret() -> str:
    secret = settings.LTI_SESSION_SECRET
    if not secret:
        # Failing loudly beats signing with an empty key, which would
        # let anyone mint a valid session.
        raise RuntimeError(
            "LTI_SESSION_SECRET is not set — refusing to issue or verify "
            "LTI session tokens."
        )
    return secret


def create_session_token(
    user_id: UUID,
    *,
    context_id: str | None = None,
    context_role: str | None = None,
) -> str:
    """Issue a session token for ``user_id``.

    ``context_id`` and ``context_role`` carry the Moodle course and the
    role the user held *in that course* at launch time. They describe
    this session, not the user — a second launch from a different course
    yields a different token.
    """
    now = utcnow()
    claims: dict[str, t.Any] = {
        "iss": SESSION_ISSUER,
        "typ": TYPE_SESSION,
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.LTI_SESSION_TTL_MINUTES),
    }
    if context_id:
        claims["lti_context_id"] = context_id
    if context_role:
        claims["lti_context_role"] = context_role
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM)


def decode_session_token(token: str) -> dict:
    """Verify a session token and return its claims.

    Raises 401 for anything that does not verify — bad signature, wrong
    issuer, expired.
    """
    # Deliberately inside the same rejection path as a bad signature.
    # Dispatching to this verifier only takes an *unverified* ``iss``, so
    # any anonymous caller can reach it with a hand-made token — on an
    # instance without a secret (the default, since LTI ships off) an
    # escaping RuntimeError would turn that into a 500 with a stack
    # trace. Nobody is authenticated either way, so 401 is the honest
    # answer; the ERROR log is what tells an operator it is their
    # configuration and not an attack.
    try:
        secret = _secret()
    except RuntimeError as e:
        logger.error("Cannot verify LTI session tokens: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired LTI session",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    claims = _decode(token, secret, "LTI session token")
    if claims.get("typ") != TYPE_SESSION:
        logger.info("LTI session token rejected: wrong token type %s", claims.get("typ"))
        raise _session_rejected()
    return claims


def create_link_challenge(
    *, issuer: str, subject: str, email: str | None, jti: str
) -> str:
    """Issue the ticket that lets somebody claim a Moodle identity.

    Handed out when a launch carries an unknown Moodle identity whose
    e-mail address already belongs to an account. The launch itself is
    refused: the address comes from an editable Moodle profile field
    and proves nothing about who is launching.

    The challenge says only *which* Moodle identity asked. It grants
    nothing on its own — it has to be spent while signed in directly,
    which is the step that proves the account is the caller's. Both
    halves are then accounted for: Moodle signed for the identity, the
    direct login for the account.
    """
    now = utcnow()
    claims = {
        "iss": SESSION_ISSUER,
        "typ": TYPE_LINK_CHALLENGE,
        "jti": jti,
        # The Moodle identity, split the way ``user_identities`` stores
        # it. ``sub`` stays the platform's subject — this token never
        # names a local user, because which local account it ends up on
        # is exactly what has yet to be proven.
        "sub": subject,
        "lti_iss": issuer,
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=settings.LTI_LINK_CHALLENGE_TTL_MINUTES),
    }
    return jwt.encode(claims, _secret(), algorithm=ALGORITHM)


def decode_link_challenge(token: str) -> dict:
    """Verify a link challenge and return its claims."""
    try:
        secret = _secret()
    except RuntimeError as e:
        logger.error("Cannot verify LTI link challenges: %s", e)
        raise _challenge_rejected() from e

    claims = _decode(token, secret, "LTI link challenge", rejected=_challenge_rejected)
    if claims.get("typ") != TYPE_LINK_CHALLENGE:
        logger.info("LTI link challenge rejected: wrong token type %s", claims.get("typ"))
        raise _challenge_rejected()
    if not claims.get("jti") or not claims.get("sub") or not claims.get("lti_iss"):
        logger.info("LTI link challenge rejected: incomplete claims")
        raise _challenge_rejected()
    return claims


def _session_rejected() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired LTI session",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _challenge_rejected() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "lti_link_challenge_invalid",
            "message": "This link request is no longer valid. Launch the "
            "activity in Moodle again.",
        },
    )


def _decode(token: str, secret: str, what: str, rejected=_session_rejected) -> dict:
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=SESSION_ISSUER,
            options={"verify_aud": False},
        )
    except JWTError as e:
        logger.info("%s rejected: %s", what, e)
        raise rejected() from e


def is_session_token(unverified_claims: t.Mapping[str, t.Any]) -> bool:
    """Whether these claims look like one of ours.

    Read from an *unverified* decode, so this only selects which
    verifier runs — it never grants anything on its own.
    """
    return unverified_claims.get("iss") == SESSION_ISSUER
