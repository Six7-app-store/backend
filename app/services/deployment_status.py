"""Live-status joiner for the deployment Infrastructure tab.

Marries the cached OpenTofu state (parsed by ``tofu_state_parser``)
with live OpenStack data via the per-user ``Connection`` helper from
``openstack_client``. Two stages, one per endpoint:

* Stage 1 (list view, ``build_resource_views``): one
  ``Connection.compute.find_server`` per compute instance, in a small
  ThreadPool with per-call timeout. Cheap enough for every list GET.

* Stage 2 (drawer view, ``build_resource_detail``): for a single
  compute instance, pull ports / security-group rule counts / volume
  attachments. Called only when the user opens the drawer.

A single live-fetch failure must not fail the whole endpoint: each VM
degrades independently to ``drift=missing`` (fetch returned nothing) or
``drift=stale`` (fetch raised), and TF-cached attributes survive so the
UI can still render something.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import User
from app.services.openstack_client import user_connection
from app.services.tofu_state_parser import TfResource, parse_tf_state

logger = logging.getLogger(__name__)


# Per-VM live-fetch budget. The ThreadPool fans out so total endpoint
# latency is ``max(per_call)`` not ``sum(per_call)``.
_PER_SERVER_TIMEOUT_S = 5.0
_MAX_PARALLEL_FETCHES = 8


# Live-status enums kept narrow so the frontend can switch on them.
# ``status``/``vm_state``/``power_state``/``task_state`` pass through
# as-is from OpenStack (stable vocabulary: ACTIVE/BUILD/ERROR/...).
ResourceDrift = Literal["in_sync", "stale", "missing"]


@dataclass
class LifecycleStates:
    """Server lifecycle quad — together they cover every diagnostic case."""
    status: str | None = None
    task_state: str | None = None
    vm_state: str | None = None
    power_state: str | None = None
    # Only set when ``status == "ERROR"`` — Nova fills ``fault.message``
    # with the underlying error the user needs to act on.
    fault_message: str | None = None


@dataclass
class HardwareSpec:
    """Compact hardware/image footprint for the card."""
    flavor_name: str | None = None
    ram_mb: int | None = None
    vcpus: int | None = None
    disk_gb: int | None = None
    image_id: str | None = None
    # Stage-1 leaves image_name unresolved (no extra round-trip);
    # stage-2 fills it via ``conn.image.find_image``.
    image_name: str | None = None
    availability_zone: str | None = None
    # ISO-8601 timestamp from ``OS-SRV-USG:launched_at``; the frontend
    # computes uptime against ``now``.
    launched_at: str | None = None


@dataclass
class NetworkAddress:
    """One entry per NIC × IP. Networks with multiple addresses (fixed
    + floating) show up as several rows under the same network name."""
    network: str
    fixed_ip: str | None = None
    floating_ip: str | None = None
    mac: str | None = None


@dataclass
class NetworkPort:
    """Stage-2: full neutron port info for one NIC of the server."""
    port_id: str
    network_id: str | None
    status: str | None
    mac: str | None
    fixed_ip: str | None
    security_group_ids: list[str] = field(default_factory=list)


@dataclass
class SecurityGroupSummary:
    """Stage-2: a count summary plus SG metadata (not the full rule dump)."""
    id: str
    name: str
    description: str | None
    ingress_rules: int
    egress_rules: int


@dataclass
class VolumeAttachment:
    """Stage-2: one row per attached volume."""
    volume_id: str
    device: str | None  # mountpoint inside the guest, e.g. "/dev/vdb"
    size_gb: int | None
    bootable: bool | None
    status: str | None
    name: str | None


@dataclass
class DeploymentResourceView:
    """One row in the resource list (stage 1) or the response of the
    detail endpoint (stage 2 — same shape, more fields populated)."""
    address: str
    type: str
    category: str
    team: str | None
    provider_id: str
    display_name: str
    drift: ResourceDrift = "in_sync"
    # Stage 1 (instance-only):
    lifecycle: LifecycleStates | None = None
    hardware: HardwareSpec | None = None
    addresses: list[NetworkAddress] = field(default_factory=list)
    # Stage 2 (instance-only, only set when ``include_detail=True``):
    ports: list[NetworkPort] | None = None
    security_groups: list[SecurityGroupSummary] | None = None
    volumes: list[VolumeAttachment] | None = None
    metadata: dict[str, str] | None = None


# ----------------------------------------------------------------
# Stage 1: list view
# ----------------------------------------------------------------
def build_resource_views(
    *,
    db: Session,
    user: User,
    tf_state_json: str | None,
    refresh: bool,
) -> list[DeploymentResourceView]:
    """Parse the cached state and (when refresh=True) overlay live data.

    Non-compute resources travel through with TF-cached data only;
    the UI shows them in read-only network/security sections, where
    a live refresh isn't meaningful.
    """
    cached = parse_tf_state(tf_state_json)
    if not cached:
        return []

    views: list[DeploymentResourceView] = [
        _view_from_cached(r) for r in cached
    ]

    if not refresh:
        return views

    instance_views = [v for v in views if v.category == "instance"]
    if not instance_views:
        return views

    # Single connection, reused across the fan-out. Closing it on
    # exit happens in the user_connection contextmanager.
    with user_connection(db, user) as conn:
        _enrich_instances_stage1(conn, instance_views)
    return views


def _view_from_cached(r: TfResource) -> DeploymentResourceView:
    """Build the base view from cached state alone — used as the
    starting point for both stage 1 and stage 2 enrichment."""
    return DeploymentResourceView(
        address=r.address,
        type=r.type,
        category=r.category,
        team=r.team,
        provider_id=r.provider_id,
        display_name=r.display_name,
        drift="in_sync",
    )


def _enrich_instances_stage1(
    conn: Any, instance_views: list[DeploymentResourceView]
) -> None:
    """Fetch each server and write lifecycle/hardware/addresses.

    Mutates ``instance_views`` in place. Failures are absorbed per-VM:
    the view degrades to ``drift = stale`` / ``drift = missing`` and
    other VMs continue.
    """
    def _one(view: DeploymentResourceView) -> None:
        try:
            server = conn.compute.find_server(view.provider_id, ignore_missing=True)
        except Exception as exc:  # noqa: BLE001 — SDK + transport
            logger.warning(
                "stage1: find_server(%s) failed: %s", view.provider_id, exc
            )
            view.drift = "stale"
            return
        if server is None:
            view.drift = "missing"
            return
        view.lifecycle = _lifecycle_from(server)
        view.hardware = _hardware_from(server)
        view.addresses = _addresses_from(server)

    # ThreadPool fans out the per-server calls with a per-call timeout
    # via ``Future.result(timeout=...)``. A timed-out future is
    # abandoned (the underlying HTTP request isn't cancelled).
    if not instance_views:
        return
    workers = min(_MAX_PARALLEL_FETCHES, len(instance_views))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, v): v for v in instance_views}
        for fut in as_completed(futures, timeout=_PER_SERVER_TIMEOUT_S * 4):
            try:
                fut.result(timeout=_PER_SERVER_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                view = futures[fut]
                logger.warning(
                    "stage1 timeout/error for %s: %s", view.provider_id, exc
                )
                if view.drift == "in_sync":
                    view.drift = "stale"


# ----------------------------------------------------------------
# Stage 2: detail view
# ----------------------------------------------------------------
def build_resource_detail(
    *,
    db: Session,
    user: User,
    tf_state_json: str | None,
    address: str,
) -> DeploymentResourceView | None:
    """Return one resource enriched with stage-2 data (ports, SGs,
    volumes, resolved image name, full metadata).

    Returns None if the address isn't a known instance in the cached
    state — the endpoint translates that into a 404.
    """
    cached = parse_tf_state(tf_state_json)
    target = next((r for r in cached if r.address == address), None)
    if target is None or target.category != "instance":
        return None

    view = _view_from_cached(target)
    with user_connection(db, user) as conn:
        try:
            server = conn.compute.find_server(
                target.provider_id, ignore_missing=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stage2: find_server(%s) failed: %s", target.provider_id, exc
            )
            view.drift = "stale"
            return view
        if server is None:
            view.drift = "missing"
            return view

        view.lifecycle = _lifecycle_from(server)
        view.hardware = _hardware_from(server)
        view.addresses = _addresses_from(server)
        view.metadata = dict(getattr(server, "metadata", None) or {})

        # Resolve the image name once. Compute-API only returns the
        # ID; the human-friendly label lives in the Image service.
        image_id = view.hardware.image_id if view.hardware else None
        if image_id:
            view.hardware.image_name = _resolve_image_name(conn, image_id)

        # Ports + SG summaries + volumes. Each block degrades to []
        # on failure — a partial drawer beats a 500.
        view.ports = _fetch_ports(conn, target.provider_id)
        view.security_groups = _fetch_sg_summaries(conn, view.ports)
        view.volumes = _fetch_volumes(conn, target.provider_id)

    return view


def _fetch_ports(conn: Any, server_id: str) -> list[NetworkPort]:
    out: list[NetworkPort] = []
    try:
        ports = list(conn.network.ports(device_id=server_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage2: ports(%s) failed: %s", server_id, exc)
        return out
    for p in ports:
        fixed_ips = getattr(p, "fixed_ips", None) or []
        fixed_ip = None
        if fixed_ips:
            # Each entry is ``{"ip_address": ..., "subnet_id": ...}``.
            fixed_ip = (fixed_ips[0] or {}).get("ip_address")
        out.append(
            NetworkPort(
                port_id=str(getattr(p, "id", None) or ""),
                network_id=getattr(p, "network_id", None),
                status=getattr(p, "status", None),
                mac=getattr(p, "mac_address", None),
                fixed_ip=fixed_ip,
                security_group_ids=list(getattr(p, "security_group_ids", None) or []),
            )
        )
    return out


def _fetch_sg_summaries(
    conn: Any, ports: list[NetworkPort]
) -> list[SecurityGroupSummary]:
    """Resolve the distinct SG IDs referenced by the ports into a
    per-SG summary with ingress/egress counts (not the full rule list)."""
    sg_ids: set[str] = set()
    for p in ports:
        for sid in p.security_group_ids:
            sg_ids.add(sid)
    if not sg_ids:
        return []
    out: list[SecurityGroupSummary] = []
    for sid in sg_ids:
        try:
            sg = conn.network.find_security_group(sid, ignore_missing=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage2: find_security_group(%s) failed: %s", sid, exc)
            continue
        if sg is None:
            continue
        rules = list(getattr(sg, "security_group_rules", None) or [])

        def _direction(r: Any) -> Any:
            return r.get("direction") if isinstance(r, dict) else getattr(r, "direction", None)

        ingress = sum(1 for r in rules if _direction(r) == "ingress")
        egress = sum(1 for r in rules if _direction(r) == "egress")
        out.append(
            SecurityGroupSummary(
                id=str(getattr(sg, "id", sid)),
                name=str(getattr(sg, "name", "") or sid),
                description=getattr(sg, "description", None),
                ingress_rules=ingress,
                egress_rules=egress,
            )
        )
    return out


def _fetch_volumes(conn: Any, server_id: str) -> list[VolumeAttachment]:
    """Build the per-volume attachment list. Two API hops: compute for
    attachments (volume_id + device), block_storage for
    size/bootable/status. Either side may fail independently — a missing
    block_storage just leaves the size fields None."""
    out: list[VolumeAttachment] = []
    try:
        attachments = list(conn.compute.volume_attachments(server=server_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage2: volume_attachments(%s) failed: %s", server_id, exc)
        return out

    for att in attachments:
        vol_id = getattr(att, "volume_id", None) or getattr(att, "id", None)
        if not vol_id:
            continue
        device = getattr(att, "device", None)
        size = bootable = status = name = None
        try:
            vol = conn.block_storage.find_volume(vol_id, ignore_missing=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stage2: find_volume(%s) failed: %s", vol_id, exc)
            vol = None
        if vol is not None:
            size = getattr(vol, "size", None)
            bootable_raw = getattr(vol, "is_bootable", None)
            if bootable_raw is None:
                bootable_raw = getattr(vol, "bootable", None)
            if isinstance(bootable_raw, str):
                bootable = bootable_raw.lower() == "true"
            elif isinstance(bootable_raw, bool):
                bootable = bootable_raw
            status = getattr(vol, "status", None)
            name = getattr(vol, "name", None)
        out.append(
            VolumeAttachment(
                volume_id=str(vol_id),
                device=device,
                size_gb=size,
                bootable=bootable,
                status=status,
                name=name,
            )
        )
    return out


def _resolve_image_name(conn: Any, image_id: str) -> str | None:
    try:
        img = conn.image.find_image(image_id, ignore_missing=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage2: find_image(%s) failed: %s", image_id, exc)
        return None
    return getattr(img, "name", None) if img is not None else None


# ----------------------------------------------------------------
# Server → dataclass adapters
# ----------------------------------------------------------------
def _lifecycle_from(server: Any) -> LifecycleStates:
    """Pull the four lifecycle states + fault message from a server.

    Tries the canonical snake_case attribute first, falling back to the
    raw ``OS-EXT-*`` keys, so it's robust to either SDK flavor.
    """
    status = getattr(server, "status", None)
    fault = None
    if status == "ERROR":
        fault_obj = getattr(server, "fault", None)
        if isinstance(fault_obj, dict):
            fault = fault_obj.get("message")
        else:
            fault = getattr(fault_obj, "message", None) if fault_obj else None

    return LifecycleStates(
        status=status,
        task_state=getattr(server, "task_state", None),
        vm_state=getattr(server, "vm_state", None),
        power_state=_translate_power_state(getattr(server, "power_state", None)),
        fault_message=fault,
    )


# Nova's ``power_state`` is an integer enum. We map to human-readable
# labels so the frontend doesn't need to know the table.
# https://docs.openstack.org/nova/latest/admin/manage-vm-states.html
_POWER_STATE_LABELS = {
    0: "NOSTATE",
    1: "RUNNING",
    3: "PAUSED",
    4: "SHUTDOWN",
    6: "CRASHED",
    7: "SUSPENDED",
}


def _translate_power_state(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int):
        return _POWER_STATE_LABELS.get(raw, f"UNKNOWN({raw})")
    return None


def _hardware_from(server: Any) -> HardwareSpec:
    flavor = getattr(server, "flavor", None) or {}
    if not isinstance(flavor, dict):
        # SDK may return a Munch / typed object — coerce best-effort
        flavor = {
            "original_name": getattr(flavor, "original_name", None),
            "ram": getattr(flavor, "ram", None),
            "vcpus": getattr(flavor, "vcpus", None),
            "disk": getattr(flavor, "disk", None),
        }
    image = getattr(server, "image", None) or {}
    image_id = image.get("id") if isinstance(image, dict) else getattr(image, "id", None)

    return HardwareSpec(
        flavor_name=flavor.get("original_name") or flavor.get("name"),
        ram_mb=flavor.get("ram"),
        vcpus=flavor.get("vcpus"),
        disk_gb=flavor.get("disk"),
        image_id=image_id,
        image_name=None,  # filled by stage-2 only
        availability_zone=getattr(server, "availability_zone", None),
        launched_at=getattr(server, "launched_at", None),
    )


def _addresses_from(server: Any) -> list[NetworkAddress]:
    """Flatten the ``addresses`` dict into one row per (network, IP).

    OpenStack's ``addresses`` is ``{network_name: [{"addr": ..., "type":
    "fixed"|"floating", ...}, ...]}``. We pair fixed and floating IPs
    that share the same network name into a single row when both
    exist, otherwise emit one row per address.
    """
    raw = getattr(server, "addresses", None) or {}
    if not isinstance(raw, dict):
        return []
    out: list[NetworkAddress] = []
    for network_name, entries in raw.items():
        if not isinstance(entries, list):
            continue
        fixed_ip = floating_ip = mac = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            addr = entry.get("addr")
            kind = entry.get("OS-EXT-IPS:type") or entry.get("type")
            if kind == "floating":
                floating_ip = addr
            else:
                # Some clouds omit ``OS-EXT-IPS:type``; assume fixed.
                if fixed_ip is None:
                    fixed_ip = addr
                    mac = entry.get("OS-EXT-IPS-MAC:mac_addr") or entry.get("mac_addr")
        out.append(
            NetworkAddress(
                network=str(network_name),
                fixed_ip=fixed_ip,
                floating_ip=floating_ip,
                mac=mac,
            )
        )
    return out
