"""Generic Terraform-state parser for the deployment status pipeline.

Reads the JSON blob persisted in ``Task.tf_state`` (produced by
``terraform state pull`` in the worker — see
``worker/app/tasks.py:collect_terraform_state``) and produces a flat,
typed list of resource entries the rest of the status pipeline can
join with live OpenStack data.

Design choices:

* Whitelist of resource types, not free-form pass-through: the state
  file contains random_password / data sources / Terraform-internal
  resources we don't surface. We filter to OpenStack kinds that map
  onto a meaningful UI card.

* Address strings match ``terraform state list`` exactly (for both
  ``count.index`` and ``for_each``), so they round-trip as
  ``-target=`` / ``-replace=`` arguments without translation.

* Team-Tag extraction: pulls ``metadata.team`` from Compute-Instance
  attributes; resources without the tag get ``team=None`` and render
  as ``Shared``.

The parser is a pure function — no DB, no HTTP, no SDK calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Resource types we surface in the Infrastructure tab. Anything else
# is filtered out — keeps the UI focused on infrastructure that maps
# onto a meaningful card and avoids leaking, e.g., ``random_password``
# values into a JSON dump.
ResourceCategory = Literal[
    "instance",
    "network",
    "subnet",
    "security_group",
    "floating_ip",
    "port",
]

_TYPE_TO_CATEGORY: dict[str, ResourceCategory] = {
    "openstack_compute_instance_v2": "instance",
    "openstack_networking_network_v2": "network",
    "openstack_networking_subnet_v2": "subnet",
    "openstack_networking_secgroup_v2": "security_group",
    "openstack_networking_floatingip_v2": "floating_ip",
    "openstack_networking_port_v2": "port",
}


@dataclass
class TfResource:
    """One resource instance from the Terraform state.

    Attributes:
        address: Full state address (``terraform state list`` format).
                 Examples:
                   * ``openstack_compute_instance_v2.team_ide["Team-A"]``
                   * ``openstack_networking_network_v2.shared``
                   * ``openstack_compute_instance_v2.worker[0]``
                 Used as the identity key for redeploy targeting.
        type: Raw HCL resource type, e.g. ``openstack_compute_instance_v2``.
        category: Coarse UI category.
        provider_id: The OpenStack-side UUID of the resource.
                     Pulled from ``attributes.id``.
        display_name: Human-friendly label for the card title.
                      Compute → instance name; others → resource name
                      or fallback to address.
        team: Value of ``attributes.metadata.team`` for compute
              instances, otherwise None. The team-contract for
              app-authors is to set ``metadata = { team = each.key }``
              on team-scoped compute resources.
        attributes: Raw attributes dict, kept for callers that need
                    cheap access to fields without re-parsing the
                    state (e.g. flavor_id, image_id, network info).
    """
    address: str
    type: str
    category: ResourceCategory
    provider_id: str
    display_name: str
    team: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


def parse_tf_state(state_json: str | dict | None) -> list[TfResource]:
    """Parse a ``terraform state pull`` JSON blob into a typed list.

    Accepts the raw string (as stored in ``Task.tf_state``) or an
    already-parsed dict. Returns an empty list for ``None`` / invalid
    JSON / empty state — callers don't need to special-case that, the
    Infrastructure tab simply renders "no resources yet" in those
    cases (e.g. before the first apply completes).
    """
    state = _coerce_state(state_json)
    if not state:
        return []

    out: list[TfResource] = []
    for resource_block in state.get("resources", []):
        raw_type = resource_block.get("type")
        category = _TYPE_TO_CATEGORY.get(raw_type)
        if category is None:
            # Not a UI-relevant type (random_password, data sources,
            # whatever the app declares for internal use). Skip.
            continue
        resource_name = resource_block.get("name") or "unnamed"
        for instance in resource_block.get("instances", []):
            entry = _build_entry(
                resource_block=resource_block,
                instance=instance,
                category=category,
                resource_name=resource_name,
            )
            if entry is not None:
                out.append(entry)
    return out


# ----------------------------------------------------------------
# Internals
# ----------------------------------------------------------------
def _coerce_state(state_json: str | dict | None) -> dict | None:
    """Best-effort: return a state dict or None.

    Invalid JSON is logged at WARN and treated as empty — better than
    a 500 in the resource endpoint. The TF-state column can be
    surprisingly empty for failed-deploy tasks (no ``apply`` ever
    succeeded), and we want a clean empty-list output in that case.
    """
    if state_json is None:
        return None
    if isinstance(state_json, dict):
        return state_json
    try:
        return json.loads(state_json)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("parse_tf_state: invalid state JSON (%s)", exc)
        return None


def _build_entry(
    *,
    resource_block: dict,
    instance: dict,
    category: ResourceCategory,
    resource_name: str,
) -> TfResource | None:
    """Materialise one resource INSTANCE into a ``TfResource``.

    Returns None when the instance has no provider-side ID (apply
    failed before OpenStack persisted the resource); a redeploy target
    without an ID wouldn't work, so it is dropped.
    """
    attrs = instance.get("attributes") or {}
    provider_id = attrs.get("id")
    if not provider_id:
        return None

    address = _format_address(
        resource_type=resource_block["type"],
        resource_name=resource_name,
        instance=instance,
    )
    display_name = _pick_display_name(category, attrs, address)
    team = _extract_team(category, attrs)

    return TfResource(
        address=address,
        type=resource_block["type"],
        category=category,
        provider_id=str(provider_id),
        display_name=display_name,
        team=team,
        attributes=attrs,
    )


def _format_address(
    *, resource_type: str, resource_name: str, instance: dict
) -> str:
    """Build the ``terraform state list``-style address string.

    Three shapes we need to support:
      * Simple resource (no index_key)   → ``type.name``
      * ``count`` (int index_key)        → ``type.name[0]``
      * ``for_each`` (string index_key)  → ``type.name["Team-A"]``

    The double-quotes around for_each keys MUST be present — they're
    part of the terraform CLI contract for ``-target=`` / ``-replace=``.
    The frontend URL-encodes them before the redeploy call and FastAPI
    restores them.
    """
    base = f"{resource_type}.{resource_name}"
    index_key = instance.get("index_key")
    if index_key is None:
        return base
    if isinstance(index_key, str):
        return f'{base}["{index_key}"]'
    if isinstance(index_key, int):
        return f"{base}[{index_key}]"
    # Unexpected — log and fall back to no index so the UI at least
    # renders something. Live-fetch will likely fail downstream and
    # the resource appears as drift=stale.
    logger.warning(
        "_format_address: unexpected index_key type %r for %s",
        index_key, base,
    )
    return base


def _pick_display_name(
    category: ResourceCategory, attrs: dict, fallback: str
) -> str:
    """Choose the most informative human-facing label per category.

    Order:
      1. ``name`` attribute (true for most networking + compute kinds)
      2. ``description`` for SGs that have no ``name``
      3. The address itself
    """
    name = attrs.get("name")
    if isinstance(name, str) and name:
        return name
    if category == "security_group":
        desc = attrs.get("description")
        if isinstance(desc, str) and desc:
            return desc
    return fallback


def _extract_team(category: ResourceCategory, attrs: dict) -> str | None:
    """Pull ``metadata.team`` from a compute-instance, else None.

    Apps follow the team-contract by setting
    ``metadata = { team = each.key }`` on team-scoped instances. The
    team is not inferred from the address. Only Compute-Instances carry
    the contract; other kinds are treated as shared.
    """
    if category != "instance":
        return None
    metadata = attrs.get("metadata")
    if not isinstance(metadata, dict):
        return None
    team = metadata.get("team")
    if isinstance(team, str) and team:
        return team
    return None
