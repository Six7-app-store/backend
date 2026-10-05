"""Unit-Tests fuer die reinen Adapter-Funktionen in ``app.services.deployment_status``.

Diese Tests beruehren weder OpenStack noch Datenbank/IO. Sie pruefen
ausschliesslich die fuenf reinen Server->Dataclass-Adapter:

* ``_view_from_cached``
* ``_lifecycle_from``
* ``_translate_power_state``
* ``_hardware_from``
* ``_addresses_from``
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.deployment_status import (
    DeploymentResourceView,
    HardwareSpec,
    LifecycleStates,
    NetworkAddress,
    _addresses_from,
    _hardware_from,
    _lifecycle_from,
    _translate_power_state,
    _view_from_cached,
)
from app.services.tofu_state_parser import TfResource


# ----------------------------------------------------------------
# _view_from_cached
# ----------------------------------------------------------------
@pytest.mark.unit
def test_view_from_cached_copies_basic_fields() -> None:
    """Adapter spiegelt die TfResource-Basisfelder in den View."""
    r = TfResource(
        address='openstack_compute_instance_v2.team_ide["Team-A"]',
        type="openstack_compute_instance_v2",
        category="instance",
        provider_id="uuid-1",
        display_name="ide-team-a",
        team="Team-A",
        attributes={"x": 1},
    )
    v = _view_from_cached(r)
    assert isinstance(v, DeploymentResourceView)
    assert v.address == r.address
    assert v.type == r.type
    assert v.category == "instance"
    assert v.provider_id == "uuid-1"
    assert v.display_name == "ide-team-a"
    assert v.team == "Team-A"


@pytest.mark.unit
def test_view_from_cached_defaults_drift_to_in_sync() -> None:
    """Frisch erzeugter View startet im ``in_sync``-Zustand."""
    r = TfResource(
        address="openstack_networking_network_v2.shared",
        type="openstack_networking_network_v2",
        category="network",
        provider_id="net-1",
        display_name="shared",
    )
    v = _view_from_cached(r)
    assert v.drift == "in_sync"
    assert v.lifecycle is None
    assert v.hardware is None
    assert v.addresses == []


@pytest.mark.unit
def test_view_from_cached_preserves_none_team() -> None:
    """Ressourcen ohne Team-Tag uebernehmen ``team=None``."""
    r = TfResource(
        address="openstack_networking_router_v2.edge",
        type="openstack_networking_router_v2",
        category="router",
        provider_id="rtr-1",
        display_name="edge",
        team=None,
    )
    v = _view_from_cached(r)
    assert v.team is None


# ----------------------------------------------------------------
# _translate_power_state
# ----------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, "NOSTATE"),
        (1, "RUNNING"),
        (3, "PAUSED"),
        (4, "SHUTDOWN"),
        (6, "CRASHED"),
        (7, "SUSPENDED"),
    ],
)
def test_translate_power_state_known_ints(raw: int, expected: str) -> None:
    """Bekannte Nova-Power-State-Enums werden auf das Label gemappt."""
    assert _translate_power_state(raw) == expected


@pytest.mark.unit
def test_translate_power_state_none_returns_none() -> None:
    """``None`` bleibt ``None`` (kein Server-Wert vorhanden)."""
    assert _translate_power_state(None) is None


@pytest.mark.unit
def test_translate_power_state_unknown_int_falls_back_to_unknown_label() -> None:
    """Unbekannte Integer-Werte werden mit Wert eingebettet."""
    assert _translate_power_state(99) == "UNKNOWN(99)"


@pytest.mark.unit
def test_translate_power_state_string_passthrough() -> None:
    """Bereits vorhandene String-Labels werden 1:1 zurueckgegeben."""
    assert _translate_power_state("RUNNING") == "RUNNING"


@pytest.mark.unit
def test_translate_power_state_unsupported_type_returns_none() -> None:
    """Andere Typen (z.B. float) liefern defensiv ``None``."""
    assert _translate_power_state(1.5) is None


# ----------------------------------------------------------------
# _lifecycle_from
# ----------------------------------------------------------------
@pytest.mark.unit
def test_lifecycle_from_active_server_object() -> None:
    """ACTIVE-Server fuellt status/task/vm/power, fault bleibt None."""
    server = SimpleNamespace(
        status="ACTIVE",
        task_state=None,
        vm_state="active",
        power_state=1,
    )
    lc = _lifecycle_from(server)
    assert isinstance(lc, LifecycleStates)
    assert lc.status == "ACTIVE"
    assert lc.task_state is None
    assert lc.vm_state == "active"
    assert lc.power_state == "RUNNING"
    assert lc.fault_message is None


@pytest.mark.unit
def test_lifecycle_from_error_with_fault_dict() -> None:
    """ERROR + fault als dict liefert ``fault.message``."""
    server = SimpleNamespace(
        status="ERROR",
        task_state=None,
        vm_state="error",
        power_state=0,
        fault={"message": "No valid host was found"},
    )
    lc = _lifecycle_from(server)
    assert lc.status == "ERROR"
    assert lc.fault_message == "No valid host was found"


@pytest.mark.unit
def test_lifecycle_from_error_with_fault_object() -> None:
    """ERROR + fault als Objekt (SDK-Munch) liefert ``message`` per getattr."""
    fault_obj = SimpleNamespace(message="hypervisor down")
    server = SimpleNamespace(
        status="ERROR",
        task_state=None,
        vm_state="error",
        power_state=0,
        fault=fault_obj,
    )
    lc = _lifecycle_from(server)
    assert lc.fault_message == "hypervisor down"


@pytest.mark.unit
def test_lifecycle_from_error_without_fault_attribute() -> None:
    """ERROR ohne fault-Attribut degradiert sauber zu ``fault_message=None``."""
    server = SimpleNamespace(
        status="ERROR",
        task_state=None,
        vm_state="error",
        power_state=0,
    )
    lc = _lifecycle_from(server)
    assert lc.status == "ERROR"
    assert lc.fault_message is None


@pytest.mark.unit
def test_lifecycle_from_non_error_skips_fault_extraction() -> None:
    """Bei status != ERROR bleibt ``fault_message`` None, auch wenn fault gesetzt waere."""
    server = SimpleNamespace(
        status="BUILD",
        task_state="spawning",
        vm_state="building",
        power_state=0,
        fault={"message": "irrelevant"},
    )
    lc = _lifecycle_from(server)
    assert lc.status == "BUILD"
    assert lc.task_state == "spawning"
    assert lc.fault_message is None


@pytest.mark.unit
def test_lifecycle_from_empty_object_returns_all_none() -> None:
    """Server ohne Attribute => alle Felder None."""
    lc = _lifecycle_from(SimpleNamespace())
    assert lc.status is None
    assert lc.task_state is None
    assert lc.vm_state is None
    assert lc.power_state is None
    assert lc.fault_message is None


# ----------------------------------------------------------------
# _hardware_from
# ----------------------------------------------------------------
@pytest.mark.unit
def test_hardware_from_flavor_as_dict_and_image_as_dict() -> None:
    """SDK liefert flavor/image als dict => alle Felder werden geholt."""
    server = SimpleNamespace(
        flavor={
            "original_name": "m1.small",
            "ram": 2048,
            "vcpus": 2,
            "disk": 20,
        },
        image={"id": "img-1"},
        availability_zone="zone-a",
        launched_at="2026-01-01T00:00:00Z",
    )
    hw = _hardware_from(server)
    assert isinstance(hw, HardwareSpec)
    assert hw.flavor_name == "m1.small"
    assert hw.ram_mb == 2048
    assert hw.vcpus == 2
    assert hw.disk_gb == 20
    assert hw.image_id == "img-1"
    assert hw.image_name is None  # stage-1 fuellt das nicht
    assert hw.availability_zone == "zone-a"
    assert hw.launched_at == "2026-01-01T00:00:00Z"


@pytest.mark.unit
def test_hardware_from_flavor_as_object_is_coerced() -> None:
    """flavor als getypter SDK-Objekt-Wert (Munch) wird best-effort geparst."""
    flavor_obj = SimpleNamespace(
        original_name="m1.medium",
        ram=4096,
        vcpus=4,
        disk=40,
    )
    image_obj = SimpleNamespace(id="img-2")
    server = SimpleNamespace(
        flavor=flavor_obj,
        image=image_obj,
        availability_zone=None,
        launched_at=None,
    )
    hw = _hardware_from(server)
    assert hw.flavor_name == "m1.medium"
    assert hw.ram_mb == 4096
    assert hw.vcpus == 4
    assert hw.disk_gb == 40
    assert hw.image_id == "img-2"


@pytest.mark.unit
def test_hardware_from_flavor_dict_with_only_name_key() -> None:
    """Fallback: kein ``original_name`` => ``name``-Key wird verwendet."""
    server = SimpleNamespace(
        flavor={"name": "tiny"},
        image=None,
    )
    hw = _hardware_from(server)
    assert hw.flavor_name == "tiny"
    assert hw.ram_mb is None
    assert hw.vcpus is None


@pytest.mark.unit
def test_hardware_from_image_absent_yields_none_image_id() -> None:
    """Server ohne image => image_id bleibt None."""
    server = SimpleNamespace(
        flavor={"original_name": "m1.tiny", "ram": 512, "vcpus": 1, "disk": 5},
        image=None,
    )
    hw = _hardware_from(server)
    assert hw.image_id is None
    assert hw.image_name is None
    assert hw.flavor_name == "m1.tiny"


@pytest.mark.unit
def test_hardware_from_completely_empty_server() -> None:
    """Server ohne flavor/image => alle Felder None, kein Crash."""
    hw = _hardware_from(SimpleNamespace())
    assert hw.flavor_name is None
    assert hw.ram_mb is None
    assert hw.vcpus is None
    assert hw.disk_gb is None
    assert hw.image_id is None
    assert hw.availability_zone is None
    assert hw.launched_at is None


# ----------------------------------------------------------------
# _addresses_from
# ----------------------------------------------------------------
@pytest.mark.unit
def test_addresses_from_empty_dict_returns_empty_list() -> None:
    """Server ohne addresses => leere Liste."""
    assert _addresses_from(SimpleNamespace(addresses={})) == []


@pytest.mark.unit
def test_addresses_from_missing_attribute_returns_empty_list() -> None:
    """Fehlendes addresses-Attribut => leere Liste."""
    assert _addresses_from(SimpleNamespace()) == []


@pytest.mark.unit
def test_addresses_from_non_dict_returns_empty_list() -> None:
    """Andere Typen unter ``addresses`` werden defensiv ignoriert."""
    server = SimpleNamespace(addresses=["not", "a", "dict"])
    assert _addresses_from(server) == []


@pytest.mark.unit
def test_addresses_from_fixed_only() -> None:
    """Ein einzelner Fixed-Eintrag fuellt fixed_ip + mac."""
    server = SimpleNamespace(
        addresses={
            "shared-net": [
                {
                    "addr": "10.0.0.5",
                    "OS-EXT-IPS:type": "fixed",
                    "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01",
                }
            ]
        }
    )
    out = _addresses_from(server)
    assert len(out) == 1
    row = out[0]
    assert isinstance(row, NetworkAddress)
    assert row.network == "shared-net"
    assert row.fixed_ip == "10.0.0.5"
    assert row.floating_ip is None
    assert row.mac == "fa:16:3e:00:00:01"


@pytest.mark.unit
def test_addresses_from_floating_only() -> None:
    """Nur Floating => floating_ip gesetzt, fixed_ip None."""
    server = SimpleNamespace(
        addresses={
            "ext-net": [
                {"addr": "203.0.113.10", "OS-EXT-IPS:type": "floating"}
            ]
        }
    )
    out = _addresses_from(server)
    assert len(out) == 1
    assert out[0].network == "ext-net"
    assert out[0].fixed_ip is None
    assert out[0].floating_ip == "203.0.113.10"


@pytest.mark.unit
def test_addresses_from_fixed_and_floating_paired_on_same_network() -> None:
    """Fixed + Floating auf demselben Netz werden in eine Zeile vereinigt."""
    server = SimpleNamespace(
        addresses={
            "shared-net": [
                {
                    "addr": "10.0.0.5",
                    "OS-EXT-IPS:type": "fixed",
                    "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:aa:bb:cc",
                },
                {"addr": "203.0.113.42", "OS-EXT-IPS:type": "floating"},
            ]
        }
    )
    out = _addresses_from(server)
    assert len(out) == 1
    row = out[0]
    assert row.network == "shared-net"
    assert row.fixed_ip == "10.0.0.5"
    assert row.floating_ip == "203.0.113.42"
    assert row.mac == "fa:16:3e:aa:bb:cc"


@pytest.mark.unit
def test_addresses_from_multiple_networks_emit_separate_rows() -> None:
    """Mehrere Netze => eine Zeile pro Netz."""
    server = SimpleNamespace(
        addresses={
            "net-a": [{"addr": "10.0.0.1", "OS-EXT-IPS:type": "fixed"}],
            "net-b": [{"addr": "10.0.1.1", "OS-EXT-IPS:type": "fixed"}],
        }
    )
    out = _addresses_from(server)
    assert len(out) == 2
    networks = {row.network for row in out}
    assert networks == {"net-a", "net-b"}


@pytest.mark.unit
def test_addresses_from_missing_type_assumed_fixed() -> None:
    """Eintrag ohne OS-EXT-IPS:type => wird als fixed interpretiert."""
    server = SimpleNamespace(
        addresses={"flat": [{"addr": "192.168.1.10"}]}
    )
    out = _addresses_from(server)
    assert len(out) == 1
    assert out[0].fixed_ip == "192.168.1.10"
    assert out[0].floating_ip is None


@pytest.mark.unit
def test_addresses_from_entries_not_list_skipped() -> None:
    """Netz mit Nicht-Listen-Wert wird uebersprungen."""
    server = SimpleNamespace(
        addresses={
            "broken": "oops",
            "good": [{"addr": "10.0.0.9", "OS-EXT-IPS:type": "fixed"}],
        }
    )
    out = _addresses_from(server)
    assert len(out) == 1
    assert out[0].network == "good"


@pytest.mark.unit
def test_addresses_from_non_dict_entry_inside_list_is_skipped() -> None:
    """Nicht-Dict-Eintraege in der Liste werden defensiv ignoriert."""
    server = SimpleNamespace(
        addresses={
            "shared-net": [
                "garbage",
                {"addr": "10.0.0.7", "OS-EXT-IPS:type": "fixed"},
            ]
        }
    )
    out = _addresses_from(server)
    assert len(out) == 1
    assert out[0].fixed_ip == "10.0.0.7"
