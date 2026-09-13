"""
Read API for the OpenStack resources of the calling user.

Used by the wizard so the user no longer has to copy UUIDs from the
Horizon dashboard. Each endpoint:

- Authenticates via Keycloak token
- Obtains an OpenStack connection from the service layer (per-request)
- Caches the response 60 s process-locally (see ``services/openstack_client``)
- Reduces the SDK object to a flat dict — only the fields that the
  frontend needs for display + selection. We do not want to leak SDK
  structure (sensitive fields, unwanted size).

Error strategy: 502 for OpenStack-side failures (no 500 — that is
reserved for "backend bug"). The frontend then renders a banner
"OpenStack not reachable, enter ID manually". 412 if credentials are missing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import openstack_client
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _safe_get(obj: Any, *names: str, default: Any = None) -> Any:
    """
    SDK objects are partly dict-like, partly property objects.
    We try all passed names in order.
    """
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None:
                return v
        except Exception:  # noqa: BLE001 — some properties raise lazily
            continue
    return default


def _list_with_oserror(
    user: User,
    kind: str,
    filters: dict | None,
    fetch_fn,
) -> list[dict]:
    """
    Wrapper that runs ``fetch_fn`` through the TTL cache and
    translates OpenStack exceptions into 502s.
    """
    try:
        return openstack_client.cached_list(
            user_id=user.userId,
            kind=kind,
            filters=filters,
            fetch=fetch_fn,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OpenStack list %s failed for user %s: %s", kind, user.userId, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "openstack_list_failed", "kind": kind, "message": str(exc)},
        )


# ----------------------------------------------------------------
# Cache-Refresh
# ----------------------------------------------------------------
@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
def refresh_cache(
    kind: str | None = Query(default=None, description="Optional: nur diese Resource-Art invalidieren"),
    current_user: User = Depends(get_current_user),
):
    """
    Cache bust for the calling user. Triggered by a click on the
    "Refresh" button next to a picker — the user has just created a
    new resource in Horizon and wants to see it.
    """
    removed = openstack_client.invalidate_user(current_user.userId, kind)
    logger.info("Cache invalidated for user %s (kind=%s, %d entries removed)",
                current_user.userId, kind, removed)
    return None


# ----------------------------------------------------------------
# Networks
# ----------------------------------------------------------------
@router.get("/networks")
def list_networks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lists all networks that the user can see in their project.

    ``shared`` and ``router_external`` are included so the frontend can
    render "External Network" hints.
    """
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            return [
                {
                    "id": _safe_get(n, "id"),
                    "name": _safe_get(n, "name") or "",
                    "description": _safe_get(n, "description") or "",
                    "shared": bool(_safe_get(n, "is_shared", "shared", default=False)),
                    "external": bool(_safe_get(n, "is_router_external", "router:external", default=False)),
                    "status": _safe_get(n, "status") or "",
                }
                for n in conn.network.networks()
            ]

    return _list_with_oserror(current_user, "networks", None, fetch)


# ----------------------------------------------------------------
# Subnets — optionally filtered by network
# ----------------------------------------------------------------
@router.get("/subnets")
def list_subnets(
    network_id: str | None = Query(default=None, description="Filter: nur Subnets in diesem Network"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    With ``network_id``: subnets of that network. Without: all subnets in
    the project. Filtering happens server-side (OpenStack API), not only
    after the cache — otherwise we would have a separate cache key per
    network, and the unfiltered list cache would never help.
    """
    filters: dict[str, Any] = {}
    if network_id:
        filters["network_id"] = network_id

    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            kwargs: dict[str, Any] = {}
            if network_id:
                kwargs["network_id"] = network_id
            return [
                {
                    "id": _safe_get(s, "id"),
                    "name": _safe_get(s, "name") or "",
                    "cidr": _safe_get(s, "cidr") or "",
                    "ip_version": _safe_get(s, "ip_version", default=4),
                    "network_id": _safe_get(s, "network_id"),
                    "gateway_ip": _safe_get(s, "gateway_ip") or "",
                }
                for s in conn.network.subnets(**kwargs)
            ]

    return _list_with_oserror(current_user, "subnets", filters or None, fetch)


# ----------------------------------------------------------------
# Flavors
# ----------------------------------------------------------------
@router.get("/flavors")
def list_flavors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Compute flavors with the three spec fields the user really wants
    (CPU/RAM/Disk). ``is_public=False`` means private — we still emit
    it, the frontend can render a note.
    """
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            out: list[dict] = []
            for f in conn.compute.flavors(get_extra_specs=False):
                out.append({
                    "id": _safe_get(f, "id"),
                    "name": _safe_get(f, "name") or "",
                    "vcpus": _safe_get(f, "vcpus", default=0) or 0,
                    "ram": _safe_get(f, "ram", default=0) or 0,        # MB
                    "disk": _safe_get(f, "disk", default=0) or 0,      # GB
                    "is_public": bool(_safe_get(f, "is_public", default=True)),
                })
            return out

    return _list_with_oserror(current_user, "flavors", None, fetch)


# ----------------------------------------------------------------
# Images
# ----------------------------------------------------------------
@router.get("/images")
def list_images(
    status_filter: str = Query(
        default="active",
        alias="status",
        description="OS Image Status (default: active). Alle Stati: 'all'",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Default ``status=active`` — we do not want to show "queued" or
    "deleted" images in the picker. ``status=all`` for power users who
    really want to see everything.
    """
    filters = {"status": status_filter}

    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            kwargs: dict[str, Any] = {}
            if status_filter and status_filter != "all":
                kwargs["status"] = status_filter
            out: list[dict] = []
            for img in conn.image.images(**kwargs):
                out.append({
                    "id": _safe_get(img, "id"),
                    "name": _safe_get(img, "name") or "",
                    "status": _safe_get(img, "status") or "",
                    "visibility": _safe_get(img, "visibility") or "",
                    "size": _safe_get(img, "size") or 0,         # bytes
                    "disk_format": _safe_get(img, "disk_format") or "",
                })
            return out

    return _list_with_oserror(current_user, "images", filters, fetch)


# ----------------------------------------------------------------
# Keypairs
# ----------------------------------------------------------------
@router.get("/keypairs")
def list_keypairs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    SSH keypairs of the user. Here the identity is always the ``name``,
    never the ID — Keystone keypairs do have IDs but Terraform modules
    use the name.
    """
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            return [
                {
                    "name": _safe_get(k, "name") or "",
                    "fingerprint": _safe_get(k, "fingerprint") or "",
                    "type": _safe_get(k, "type") or "ssh",
                    # ``id`` equals the name for a keypair — we duplicate this
                    # intentionally so the picker can uniformly read ``id``.
                    "id": _safe_get(k, "name") or "",
                }
                for k in conn.compute.keypairs()
            ]

    return _list_with_oserror(current_user, "keypairs", None, fetch)


# ----------------------------------------------------------------
# Security Groups
# ----------------------------------------------------------------
@router.get("/security-groups")
def list_security_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            return [
                {
                    "id": _safe_get(sg, "id"),
                    "name": _safe_get(sg, "name") or "",
                    "description": _safe_get(sg, "description") or "",
                }
                for sg in conn.network.security_groups()
            ]

    return _list_with_oserror(current_user, "security_groups", None, fetch)


# ----------------------------------------------------------------
# Floating IP Pools (External Networks)
# ----------------------------------------------------------------
@router.get("/floating-ip-pools")
def list_floating_ip_pools(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    There is no dedicated ``Pool`` resource in OpenStack — pools are
    networks with ``router:external = true``. Terraform modules usually
    expect the **name** of the external network.
    """
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            out: list[dict] = []
            for n in conn.network.networks():
                is_ext = bool(_safe_get(n, "is_router_external", "router:external", default=False))
                if not is_ext:
                    continue
                out.append({
                    "id": _safe_get(n, "id"),
                    "name": _safe_get(n, "name") or "",
                    "description": _safe_get(n, "description") or "",
                })
            return out

    return _list_with_oserror(current_user, "floating_ip_pools", None, fetch)


# ----------------------------------------------------------------
# Volumes
# ----------------------------------------------------------------
@router.get("/volumes")
def list_volumes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Cinder volumes. The frontend filters ``status`` itself if needed
    — we emit all of them, because a user can well attach a second
    instance to an ``in-use`` volume.
    """
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            return [
                {
                    "id": _safe_get(v, "id"),
                    "name": _safe_get(v, "name") or "",
                    "size": _safe_get(v, "size") or 0,            # GB
                    "status": _safe_get(v, "status") or "",
                    "volume_type": _safe_get(v, "volume_type") or "",
                    "bootable": bool(_safe_get(v, "is_bootable", "bootable", default=False)),
                }
                for v in conn.volume.volumes()
            ]

    return _list_with_oserror(current_user, "volumes", None, fetch)


# ----------------------------------------------------------------
# Routers
# ----------------------------------------------------------------
@router.get("/routers")
def list_routers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            return [
                {
                    "id": _safe_get(r, "id"),
                    "name": _safe_get(r, "name") or "",
                    "status": _safe_get(r, "status") or "",
                    "external_gateway_info": _safe_get(r, "external_gateway_info") or None,
                }
                for r in conn.network.routers()
            ]

    return _list_with_oserror(current_user, "routers", None, fetch)


# ----------------------------------------------------------------
# Availability Zones
# ----------------------------------------------------------------
@router.get("/availability-zones")
def list_availability_zones(
    service: str = Query(
        default="compute",
        description="OpenStack-Service: compute (Nova), network (Neutron), volume (Cinder)",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    AZs differ per service. Default: compute (Nova), because that is
    the most common use case (VM placement).
    """
    filters = {"service": service}

    def fetch() -> list[dict]:
        with openstack_client.user_connection(db, current_user) as conn:
            if service == "compute":
                source = conn.compute.availability_zones()
            elif service == "network":
                source = conn.network.availability_zones()
            elif service == "volume":
                source = conn.volume.availability_zones()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown service '{service}' (compute|network|volume)",
                )
            out: list[dict] = []
            for az in source:
                name = _safe_get(az, "name") or ""
                if not name:
                    continue
                out.append({
                    # AZs have no UUID — the name IS the ID.
                    "id": name,
                    "name": name,
                    "state": _safe_get(az, "state", "zoneState") or "",
                })
            return out

    return _list_with_oserror(current_user, f"availability_zones_{service}", filters, fetch)
