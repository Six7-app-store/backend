"""Unit tests for ``app/services/tf_state_parser.py``.

Pure-function coverage: no FastAPI, no DB, no OpenStack SDK.
"""
import pytest

from app.services.tf_state_parser import parse_tf_state

# ----------------------------------------------------------------
# Fixture state — mirrors the shape of the Online-IDE app's state
# after a successful 2-team deploy. Compute (for_each over teams),
# Port (for_each), FIP (for_each), single Network, single SG, and
# one random_password resource that the parser MUST skip.
# ----------------------------------------------------------------
_ONLINE_IDE_STATE = {
    "version": 4,
    "terraform_version": "1.7.0",
    "resources": [
        {
            "type": "openstack_compute_instance_v2",
            "name": "team_ide",
            "instances": [
                {
                    "index_key": "Team-A",
                    "attributes": {
                        "id": "uuid-vm-a",
                        "name": "online-ide-Team-A",
                        "metadata": {"team": "Team-A"},
                        "flavor_id": "flavor-1",
                    },
                },
                {
                    "index_key": "Team-B",
                    "attributes": {
                        "id": "uuid-vm-b",
                        "name": "online-ide-Team-B",
                        "metadata": {"team": "Team-B"},
                        "flavor_id": "flavor-1",
                    },
                },
            ],
        },
        {
            "type": "openstack_networking_port_v2",
            "name": "team_port",
            "instances": [
                {
                    "index_key": "Team-A",
                    "attributes": {"id": "uuid-port-a", "name": "port-A"},
                },
            ],
        },
        {
            "type": "openstack_networking_floatingip_v2",
            "name": "team_fip",
            "instances": [
                {
                    "index_key": "Team-A",
                    "attributes": {"id": "uuid-fip-a", "address": "10.0.0.1"},
                },
            ],
        },
        {
            "type": "openstack_networking_network_v2",
            "name": "shared",
            "instances": [
                {"attributes": {"id": "uuid-net", "name": "shared-net"}},
            ],
        },
        {
            # Must NOT appear in the parsed output — not in the whitelist.
            "type": "random_password",
            "name": "user_pw",
            "instances": [{"attributes": {"id": "rp-1"}}],
        },
    ],
}


@pytest.mark.unit
def test_parses_compute_instances_with_team_tag():
    result = parse_tf_state(_ONLINE_IDE_STATE)
    instances = [r for r in result if r.category == "instance"]
    assert len(instances) == 2

    teams = {r.team for r in instances}
    assert teams == {"Team-A", "Team-B"}

    a = next(r for r in instances if r.team == "Team-A")
    assert a.address == 'openstack_compute_instance_v2.team_ide["Team-A"]'
    assert a.provider_id == "uuid-vm-a"
    assert a.display_name == "online-ide-Team-A"
    assert a.type == "openstack_compute_instance_v2"
    # Raw attributes survive for downstream stage-2 callers
    assert a.attributes["flavor_id"] == "flavor-1"


@pytest.mark.unit
def test_address_format_matches_terraform_state_list():
    """``terraform state list`` prints quoted for_each keys — our
    addresses must match byte-for-byte so they round-trip into
    ``-target=`` and ``-replace=`` arguments."""
    result = parse_tf_state(_ONLINE_IDE_STATE)
    addresses = {r.address for r in result}
    assert 'openstack_compute_instance_v2.team_ide["Team-A"]' in addresses
    assert 'openstack_compute_instance_v2.team_ide["Team-B"]' in addresses
    assert 'openstack_networking_port_v2.team_port["Team-A"]' in addresses
    # Singleton (no index_key) shouldn't get a synthetic ``[0]`` suffix
    assert "openstack_networking_network_v2.shared" in addresses


@pytest.mark.unit
def test_count_index_renders_without_quotes():
    """``count.index`` produces integer ``index_key`` — address should
    use bare brackets, not quoted ones."""
    state = {
        "resources": [
            {
                "type": "openstack_compute_instance_v2",
                "name": "worker",
                "instances": [
                    {"index_key": 0, "attributes": {"id": "v0", "name": "w0"}},
                    {"index_key": 1, "attributes": {"id": "v1", "name": "w1"}},
                ],
            },
        ]
    }
    result = parse_tf_state(state)
    addresses = {r.address for r in result}
    assert addresses == {
        "openstack_compute_instance_v2.worker[0]",
        "openstack_compute_instance_v2.worker[1]",
    }


@pytest.mark.unit
def test_skips_non_whitelisted_resource_types():
    """``random_password`` and other irrelevant types must NOT leak
    into the resource list."""
    result = parse_tf_state(_ONLINE_IDE_STATE)
    types = {r.type for r in result}
    assert "random_password" not in types


@pytest.mark.unit
def test_skips_instance_without_provider_id():
    """A half-failed apply can leave a resource row in state with no
    ``id``. The parser drops it — the resource doesn't exist in OS,
    so there's nothing meaningful the UI could render for it."""
    state = {
        "resources": [
            {
                "type": "openstack_compute_instance_v2",
                "name": "team_ide",
                "instances": [
                    # Missing id — must be skipped
                    {"index_key": "Team-X", "attributes": {"name": "broken"}},
                    # Has id — must be kept
                    {"index_key": "Team-Y", "attributes": {"id": "uuid-y", "name": "ok"}},
                ],
            },
        ]
    }
    result = parse_tf_state(state)
    assert len(result) == 1
    assert result[0].provider_id == "uuid-y"


@pytest.mark.unit
def test_team_tag_missing_yields_none():
    state = {
        "resources": [
            {
                "type": "openstack_compute_instance_v2",
                "name": "lone",
                "instances": [
                    {"attributes": {"id": "uuid-1", "name": "lone-vm"}},
                ],
            },
        ]
    }
    result = parse_tf_state(state)
    assert len(result) == 1
    assert result[0].team is None  # UI renders as "Shared"


@pytest.mark.unit
def test_team_tag_only_for_compute():
    """The team-tag contract applies to compute instances. A network
    with a metadata.team field (uncommon) should not pick the tag up,
    because non-compute categories don't carry team affiliation in
    the UI."""
    state = {
        "resources": [
            {
                "type": "openstack_networking_network_v2",
                "name": "weird",
                "instances": [
                    {
                        "attributes": {
                            "id": "n1",
                            "name": "net",
                            "metadata": {"team": "Team-A"},
                        }
                    },
                ],
            },
        ]
    }
    result = parse_tf_state(state)
    assert result[0].team is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [None, "", "not-json", "{garbage"])
def test_invalid_or_empty_state_returns_empty_list(bad):
    """Pre-apply tasks have no state; the endpoint must render an
    empty Infrastructure tab in that case rather than 500."""
    assert parse_tf_state(bad) == []


@pytest.mark.unit
def test_accepts_pre_parsed_dict():
    """The Task.tf_state column stores a JSON string, but callers
    sometimes pass an already-parsed dict (e.g. from terraform CLI
    output)."""
    assert parse_tf_state({"resources": []}) == []
    result = parse_tf_state(_ONLINE_IDE_STATE)
    assert len(result) > 0


@pytest.mark.unit
def test_display_name_falls_back_to_address():
    """If the attributes don't carry a name, the address is the most
    informative label we have left."""
    state = {
        "resources": [
            {
                "type": "openstack_networking_secgroup_v2",
                "name": "sg",
                "instances": [{"attributes": {"id": "sg-1"}}],
            },
        ]
    }
    result = parse_tf_state(state)
    assert result[0].display_name == "openstack_networking_secgroup_v2.sg"
