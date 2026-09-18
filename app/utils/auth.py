"""The single authentication dependency for the whole API.

There are two ways into the app store and one permission model behind
them. Keycloak issues tokens for the direct login; an LTI launch from
Moodle ends in a session token this application issues itself (see
:mod:`app.utils.lti_session`). Both arrive as a bearer token on the
same endpoints, so one dependency accepts both and resolves either to
the same :class:`~app.models.User` row.

Dispatch is on the ``iss`` claim, read from an *unverified* decode.
That decides nothing but which verifier runs: whichever branch is
taken, the token is then checked in full, and a forged ``iss`` only
picks a verifier that will reject it.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.utils.keycloak_auth import sync_user_from_keycloak, verify_keycloak_token_offline
from app.utils.lti_session import decode_session_token, is_session_token

logger = logging.getLogger(__name__)

security = HTTPBearer()

_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _peek_claims(token: str) -> dict:
    """Read claims without verifying, to pick the right verifier."""
    try:
        return jwt.get_unverified_claims(token)
    except JWTError as e:
        logger.info("Bearer token is not a readable JWT: %s", e)
        raise _INVALID from e


def _user_from_lti_session(db: Session, token: str) -> User:
    claims = decode_session_token(token)
    user_id = claims.get("sub")
    if not user_id:
        raise _INVALID

    user = db.query(User).filter(User.userId == user_id).first()
    if user is None:
        # The account was deleted after the launch. Nothing to restore
        # here — the user has to launch again.
        logger.info("LTI session references unknown user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _user_from_keycloak(db: Session, token: str) -> User:
    token_info = verify_keycloak_token_offline(token)

    keycloak_id = token_info.get("sub")
    if not keycloak_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing user ID (sub)",
        )

    return sync_user_from_keycloak(
        db,
        {
            "id": keycloak_id,
            "email": token_info.get("email"),
            "username": token_info.get("preferred_username"),
            "roles": token_info.get("realm_access", {}).get("roles", []),
            "firstName": token_info.get("given_name"),
            "lastName": token_info.get("family_name"),
        },
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to a local user, whichever kind it is."""
    token = credentials.credentials
    if is_session_token(_peek_claims(token)):
        return _user_from_lti_session(db, token)
    return _user_from_keycloak(db, token)


def get_current_keycloak_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Like :func:`get_current_user`, but refuses an LTI session.

    A few actions must be authorised by the account's owner rather than
    by whoever is holding a launched session. Linking a Moodle identity
    is the case this exists for: an LTI session is itself derived from
    the launch claims, so letting it authorise a link would close the
    loop and prove nothing.
    """
    token = credentials.credentials
    if is_session_token(_peek_claims(token)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "direct_login_required",
                "message": "Sign in directly for this action — a session "
                "launched from Moodle cannot authorise it.",
            },
        )
    return _user_from_keycloak(db, token)
