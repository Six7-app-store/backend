"""Tests for what the wizard reads from a variable's ``validation`` block:
the closed value set (``allowedValues``), rendered as a dropdown or as a
narrowed resource picker, and the bounds of a number (``minValue``,
``maxValue``).

Pure functions — no Git clone, no FastAPI app, no DB.
"""
import pytest

from app.routers.apps import _allowed_values, _number_range, _parse_one_variable


def _parse(block: str, name: str = "ip_mode") -> dict:
    return _parse_one_variable(
        var_name=name,
        var_block=block,
        var_block_offset=0,
        file_content=block,
        file_label="tofu/variables.tofu",
    )


@pytest.mark.unit
def test_contains_validation_becomes_allowed_values():
    block = """
  description = "Adressfamilie der Instanzen"
  type        = string
  default     = "ipv4"

  validation {
    condition     = contains(["ipv4", "ipv6", "dual"], var.ip_mode)
    error_message = "ip_mode muss ipv4, ipv6 oder dual sein."
  }
"""
    var = _parse(block)
    assert var["allowedValues"] == ["ipv4", "ipv6", "dual"]
    assert var["default"] == "ipv4"
    # The validation block must not leak into the other fields.
    assert var["type"] == "string"
    assert var["description"] == "Adressfamilie der Instanzen"


@pytest.mark.unit
@pytest.mark.parametrize(
    "condition,expected",
    [
        ('contains(toset(["a", "b"]), var.ip_mode)', ["a", "b"]),
        ('contains(tolist(["a"]), var.ip_mode)', ["a"]),
        ('contains([\n  "a",\n  "b",\n], var.ip_mode)', ["a", "b"]),
        ("contains([1, 2, 4], var.ip_mode)", [1, 2, 4]),
    ],
)
def test_contains_variants(condition, expected):
    assert _allowed_values("ip_mode", f"validation {{ condition = {condition} }}") == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "block",
    [
        # No validation at all.
        'type = string\ndefault = "x"',
        # A range check is not a closed set.
        "validation { condition = var.ip_mode > 0 }",
        # Refers to a local — not knowable from the variables file.
        "validation { condition = contains(local.modes, var.ip_mode) }",
        "validation { condition = contains([local.a, \"b\"], var.ip_mode) }",
        # Validates a different variable.
        'validation { condition = contains(["a"], var.other) }',
        # A prefix of the name must not match.
        'validation { condition = contains(["a"], var.ip_mode_extra) }',
        # Empty list.
        "validation { condition = contains([], var.ip_mode) }",
    ],
)
def test_no_allowed_values(block):
    assert _allowed_values("ip_mode", block) is None
    assert "allowedValues" not in _parse(block)


@pytest.mark.unit
def test_flavor_marker_keeps_allowed_values():
    # The app narrows the flavor picker to the sizes it supports.
    block = """
  description = "Größe der VM @openstack:flavor"
  type        = string
  default     = "gp1.small"

  validation {
    condition     = contains(["gp1.small", "gp1.medium"], var.flavor)
    error_message = "Nur gp1.small oder gp1.medium."
  }
"""
    var = _parse(block, name="flavor")
    assert var["osType"] == "flavor"
    assert var["osMode"] == "name"
    assert var["allowedValues"] == ["gp1.small", "gp1.medium"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "condition,expected",
    [
        ("var.disk_gb >= 0 && var.disk_gb <= 200", (0, 200)),
        ("0 <= var.disk_gb && 200 >= var.disk_gb", (0, 200)),
        ("var.disk_gb >= 10", (10, None)),
        ("var.disk_gb <= 1.5", (None, 1.5)),
        ("var.disk_gb >= -5 && var.disk_gb <= 5", (-5, 5)),
        # Strict bounds and bounds from locals are left to OpenTofu.
        ("var.disk_gb > 0 && var.disk_gb < 200", (None, None)),
        ("var.disk_gb >= local.min_gb", (None, None)),
        # Another variable's bound, or a longer name, must not count.
        ("var.other >= 1", (None, None)),
        ("var.disk_gb_extra >= 1", (None, None)),
        ("floor(var.disk_gb) == var.disk_gb", (None, None)),
    ],
)
def test_number_range(condition, expected):
    assert _number_range("disk_gb", f"validation {{ condition = {condition} }}") == expected


@pytest.mark.unit
def test_number_range_in_payload():
    block = """
  description = "Zusätzlicher Speicher in GB, 0 = keiner"
  type        = number
  default     = 0

  validation {
    condition     = var.disk_gb >= 0 && var.disk_gb <= 200
    error_message = "Zwischen 0 und 200 GB."
  }
"""
    var = _parse(block, name="disk_gb")
    assert var["minValue"] == 0
    assert var["maxValue"] == 200
    assert isinstance(var["minValue"], int)
    assert var["default"] == 0


@pytest.mark.unit
def test_no_range_keys_without_bounds():
    var = _parse("type = number\ndefault = 1", name="disk_gb")
    assert "minValue" not in var
    assert "maxValue" not in var
