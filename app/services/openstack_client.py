"""Shared OpenStack client layer for FastAPI endpoints.

Three responsibilities:

1. Build auth kwargs from the encrypted user credentials (single
   source of truth).
2. Open one connection per request, exposed as a context manager so
   endpoints can close it cleanly.
3. A process-local TTL cache for list responses: the wizard fires
   several GETs in quick succession, and a 60s cache avoids a Keystone
   token refresh per click. A frontend refresh button triggers
   ``invalidate_user``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any
from uuid import UUID

import openstack
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.crud import openstack_credentials as crud_creds
from app.models import User

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# Connection-Bau
# ----------------------------------------------------------------
def _build_connect_kwargs(creds: dict) -> dict:
    """Build the kwargs dict for ``openstack.connect`` from decrypted
    user credentials. Supports password and application-credential
    (v3applicationcredential) auth.
    """
    base = {
        "auth_url": creds["auth_url"],
        "region_name": creds.get("region_name"),
        "interface": creds.get("interface") or "public",
        "identity_api_version": creds.get("identity_api_version") or "3",
    }
    if creds["auth_type"] == "v3applicationcredential":
        base.update(
            {
                "auth_type": "v3applicationcredential",
                "application_credential_id": creds["identifier"],
                "application_credential_secret": creds["secret"],
            }
        )
    else:
        base.update(
            {
                "auth_type": "password",
                "username": creds["identifier"],
                "password": creds["secret"],
                "project_id": creds.get("project_id"),
                "project_name": creds.get("project_name"),
                "user_domain_name": creds.get("user_domain_name"),
                "project_domain_name": creds.get("project_domain_name")
                or creds.get("user_domain_name"),
            }
        )
    return base


@contextmanager
def user_connection(db: Session, user: User) -> Iterator[Any]:
    """Yield an ``openstack.Connection`` for the user.

    - 412 if no credentials are stored (frontend shows a CTA banner)
    - 502 for a transient OpenStack error on connect (500s are reserved
      for backend bugs)
    """
    try:
        creds = crud_creds.get_decrypted_for_backend(db, user.userId)
    except crud_creds.NoCredentialError:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"reason": "openstack_credentials_missing"},
        )

    conn = None
    try:
        conn = openstack.connect(**_build_connect_kwargs(creds))
        yield conn
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — SDK raises many types
        logger.warning(
            "OpenStack connect failed for user %s: %s", user.userId, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "openstack_unavailable", "message": str(exc)},
        )
    finally:
        # Close politely; the SDK tolerates leaving it to GC. Ignore errors.
        if conn is not None:
            with suppress(Exception):
                conn.close()


# ----------------------------------------------------------------
# TTL-Cache für Resource-Listen
# ----------------------------------------------------------------
# Key = (user_id, resource_kind, frozenset of filter items)
# Value = (expiry_epoch, data)
_CacheKey = tuple[str, str, frozenset]
_cache: dict[_CacheKey, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
_TTL_SECONDS = 60.0


def _make_key(user_id: UUID, kind: str, filters: dict | None) -> _CacheKey:
    items: frozenset = frozenset((filters or {}).items())
    return (str(user_id), kind, items)


def cached_list(
    user_id: UUID,
    kind: str,
    filters: dict | None,
    fetch: Callable[[], list[dict]],
) -> list[dict]:
    """TTL-cache wrapper. ``fetch`` is called only when no valid entry
    exists. The cache is process-local and in-memory; multiple backend
    instances run independent caches, which is fine since the data is
    allowed to be up to 60s stale.
    """
    key = _make_key(user_id, kind, filters)
    now = time.monotonic()

    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    # Two concurrent requests may both fetch here; inefficient but not
    # incorrect, and simpler than a per-key lock map.
    data = fetch()

    with _cache_lock:
        # Entries are only ever read by their own key, so an expired one would
        # stay in memory forever. Sweep them while we hold the lock anyway.
        current = time.monotonic()
        for stale in [k for k, (expires, _) in _cache.items() if expires <= current]:
            del _cache[stale]
        _cache[key] = (now + _TTL_SECONDS, data)

    return data


def invalidate_user(user_id: UUID, kind: str | None = None) -> int:
    """Invalidate the cache for one user (triggered by the frontend
    refresh button). Without ``kind`` all resource types for the user
    are removed. Returns the number of removed entries.
    """
    user_str = str(user_id)
    removed = 0
    with _cache_lock:
        for key in list(_cache.keys()):
            if key[0] != user_str:
                continue
            if kind is not None and key[1] != kind:
                continue
            del _cache[key]
            removed += 1
    return removed
