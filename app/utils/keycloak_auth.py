"""
Keycloak Authentication & Authorization
Handles token validation and user management with Keycloak.
"""
import logging
import threading

from fastapi import HTTPException, status
from jose import JWTError, jwt
from keycloak import KeycloakAdmin, KeycloakAuthenticationError, KeycloakOpenID
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, UserRole

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# KEYCLOAK CLIENTS
# ----------------------------------------------------------------
def get_keycloak_client() -> KeycloakOpenID:
    """Get Keycloak OpenID client instance"""
    return KeycloakOpenID(
        server_url=settings.KEYCLOAK_SERVER_URL,
        client_id=settings.KEYCLOAK_CLIENT_ID,
        realm_name=settings.KEYCLOAK_REALM,
        client_secret_key=settings.KEYCLOAK_CLIENT_SECRET,
        verify=True,
    )


def get_keycloak_admin() -> KeycloakAdmin:
    """Get Keycloak Admin client (uses service-account client_credentials grant)."""
    return KeycloakAdmin(
        server_url=settings.KEYCLOAK_SERVER_URL,
        realm_name=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_CLIENT_ID,
        client_secret_key=settings.KEYCLOAK_CLIENT_SECRET,
        verify=True,
    )


# ----------------------------------------------------------------
# PUBLIC KEY CACHE
# ----------------------------------------------------------------
# python-keycloak's public_key() does a fresh HTTP GET on every call,
# which we make on every authenticated request. The realm signing key
# is stable for the process lifetime, so we cache the PEM-formatted key
# after the first fetch; restart the process on key rotation.
_public_key_pem: str | None = None
_public_key_lock = threading.Lock()


def _get_realm_public_key_pem() -> str:
    global _public_key_pem
    if _public_key_pem is not None:
        return _public_key_pem
    with _public_key_lock:
        if _public_key_pem is not None:
            return _public_key_pem
        raw = get_keycloak_client().public_key()
        _public_key_pem = (
            "-----BEGIN PUBLIC KEY-----\n" + raw + "\n-----END PUBLIC KEY-----"
        )
        return _public_key_pem


# ----------------------------------------------------------------
# TOKEN VALIDATION
# ----------------------------------------------------------------
def verify_keycloak_token(token: str) -> dict:
    """Validate token via Keycloak introspection (slow, server round-trip)."""
    try:
        keycloak_client = get_keycloak_client()
        token_info = keycloak_client.introspect(token)
        if not token_info.get("active"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token is not active or has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token_info
    except KeycloakAuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_keycloak_token_offline(token: str) -> dict:
    """Validate JWT signature against Keycloak public key (fast, no server round-trip)."""
    try:
        public_key = _get_realm_public_key_pem()
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_signature": True, "verify_aud": False, "verify_exp": True},
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token validation failed",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ----------------------------------------------------------------
# ROLE MAPPING
# ----------------------------------------------------------------
def map_keycloak_roles_to_app_role(keycloak_roles: list) -> UserRole:
    """Priority: admin > teacher > student"""
    if "admin" in keycloak_roles:
        return UserRole.ADMIN
    if "teacher" in keycloak_roles:
        return UserRole.TEACHER
    return UserRole.STUDENT


# ----------------------------------------------------------------
# USER SYNC (Just-in-Time Provisioning)
# ----------------------------------------------------------------
def sync_user_from_keycloak(db: Session, keycloak_user_data: dict) -> User:
    """
    Synchronize a Keycloak user into the local DB.
    Creates the user if missing, updates role/name if changed.
    Expects keys: id (or sub), username, email, roles (or realm_access.roles),
    firstName/given_name, lastName/family_name.
    """
    keycloak_id = keycloak_user_data.get("id") or keycloak_user_data.get("sub")
    email = keycloak_user_data.get("email")
    username = keycloak_user_data.get("username") or keycloak_id
    keycloak_roles = (
        keycloak_user_data.get("roles")
        or keycloak_user_data.get("realm_access", {}).get("roles", [])
    )
    app_role = map_keycloak_roles_to_app_role(keycloak_roles)
    first_name = keycloak_user_data.get("firstName") or keycloak_user_data.get("given_name")
    last_name = keycloak_user_data.get("lastName") or keycloak_user_data.get("family_name")

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak user has no email; refusing to provision.",
        )

    user = db.query(User).filter(User.keycloak_id == keycloak_id).first()
    if not user:
        # An account for this person can already exist without a
        # ``keycloak_id``: an LTI launch provisions by e-mail and has no
        # Keycloak subject to store. Adopt that row instead of inserting
        # a second one — ``users.email`` is UNIQUE, so the insert would
        # raise ``IntegrityError`` on every authenticated request the
        # person makes and lock them out of the Keycloak path for good.
        #
        # Only adopt when the email address is verified in Keycloak.
        # Keycloak does not require verification by default, so an
        # attacker who registers with a victim's address would otherwise
        # gain full access to the victim's existing account on first
        # sign-in.
        email_verified = keycloak_user_data.get("email_verified", False)
        if email_verified:
            user = db.query(User).filter(User.email == email).first()
            if user:
                logger.info(
                    "Linking Keycloak subject %s to the existing account for %s",
                    keycloak_id,
                    email,
                )
                user.keycloak_id = keycloak_id
                db.commit()
                db.refresh(user)
        elif db.query(User).filter(User.email == email).first():
            logger.warning(
                "Refusing to link Keycloak subject %s to existing account for %s: "
                "email not verified in Keycloak",
                keycloak_id,
                email,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "email_not_verified",
                    "message": "Verify your email address in Keycloak before signing in.",
                },
            )

    if not user:
        user = User(
            keycloak_id=keycloak_id,
            email=email,
            username=username,
            role=app_role,
            firstName=first_name,
            lastName=last_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    updated = False
    # Email is the identifier used for display, search, and
    # notifications; follow the Keycloak record when it changes.
    if email and user.email != email:
        user.email = email
        updated = True
    # Username is the login identifier shown in the UI and in
    # Terraform-issued credentials; keep it in sync too.
    if username and user.username != username:
        user.username = username
        updated = True
    if user.role != app_role:
        user.role = app_role
        updated = True
    if first_name and user.firstName != first_name:
        user.firstName = first_name
        updated = True
    if last_name and user.lastName != last_name:
        user.lastName = last_name
        updated = True
    if updated:
        db.commit()
        db.refresh(user)
    return user


# ----------------------------------------------------------------
# KEYCLOAK USER LOOKUP (admin API)
# ----------------------------------------------------------------
def _project_keycloak_user(u: dict, *, include_enabled: bool = False) -> dict:
    """Project a raw Keycloak user record into our simplified dict shape.

    Includes the ``enabled`` flag only when ``include_enabled`` is set,
    preserving the differing output shapes of the two call sites.
    """
    projected = {
        "id": u.get("id"),
        "username": u.get("username"),
        "email": u.get("email"),
        "firstName": u.get("firstName", ""),
        "lastName": u.get("lastName", ""),
    }
    if include_enabled:
        projected["enabled"] = u.get("enabled", True)
    return projected


def search_keycloak_users(search_query: str, max_results: int = 10) -> list[dict]:
    """Search Keycloak users by username/email/name (uses service account)."""
    try:
        keycloak_admin = get_keycloak_admin()
        users = keycloak_admin.get_users({"search": search_query, "max": max_results})
        return [
            _project_keycloak_user(u, include_enabled=True)
            for u in users
        ]
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search Keycloak users",
        )


def get_keycloak_users_by_ids(ids: list[str]) -> dict:
    """Resolve a list of Keycloak IDs to simplified user dicts."""
    result: dict = {}
    if not ids:
        return result
    try:
        kc = get_keycloak_admin()
        for kid in ids:
            try:
                user = kc.get_user(kid)
            except Exception:
                try:
                    users = kc.get_users({"search": kid, "max": 1})
                    user = users[0] if users else None
                except Exception:
                    user = None
            if not user:
                continue
            projected = _project_keycloak_user(user)
            projected["id"] = user.get("id") or kid
            result[user.get("id") or kid] = projected
        return result
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch Keycloak users",
        )


# ----------------------------------------------------------------
# JIT REFRESH FOR OUTGOING SIDE-CHANNELS (e.g. mail)
# ----------------------------------------------------------------
def refresh_user_from_keycloak(db: Session, user: User) -> User:
    """Pull the latest Keycloak record for ``user`` and reconcile our DB row.

    Used by paths that address a user outside of an active HTTP request
    (most importantly the mail notifier) so credentials go to the
    current email even if it changed since the wizard pick.

    Best-effort by design:

    * If the user has no ``keycloak_id``, there is nothing to refresh —
      return the row unchanged.
    * If the Admin API is down or the user was deleted in Keycloak, log
      at WARNING and return the row unchanged so a flaky KC doesn't
      block notifications.

    Reuses :func:`sync_user_from_keycloak` for the DB write.
    """
    if not user or not getattr(user, "keycloak_id", None):
        return user

    try:
        kc = get_keycloak_admin()
        kc_user = kc.get_user(user.keycloak_id)
    except Exception as e:
        logger.warning(
            "refresh_user_from_keycloak: get_user(%s) failed (%s); "
            "falling back to DB record for %s",
            user.keycloak_id, e, user.email,
        )
        return user

    if not kc_user:
        logger.warning(
            "refresh_user_from_keycloak: keycloak returned no user for id=%s; "
            "user may have been deleted upstream — keeping DB record for %s",
            user.keycloak_id, user.email,
        )
        return user

    # ``get_user`` doesn't include realm roles; pre-seed with the
    # current role mapping so re-running role mapping doesn't cause a
    # spurious downgrade. Role rotation goes through the auth dependency
    # on next login.
    current_role_token = []
    if user.role == UserRole.ADMIN:
        current_role_token = ["admin"]
    elif user.role == UserRole.TEACHER:
        current_role_token = ["teacher"]

    try:
        return sync_user_from_keycloak(
            db,
            {
                "id": kc_user.get("id") or user.keycloak_id,
                "email": kc_user.get("email"),
                "username": kc_user.get("username"),
                "firstName": kc_user.get("firstName"),
                "lastName": kc_user.get("lastName"),
                "roles": current_role_token,
            },
        )
    except HTTPException as e:
        # ``sync_user_from_keycloak`` raises 400 when KC returns a user
        # without an email; defend in depth so a bad record doesn't
        # block the notifier.
        logger.warning(
            "refresh_user_from_keycloak: sync rejected KC payload for %s: %s",
            user.keycloak_id, e.detail,
        )
        return user
