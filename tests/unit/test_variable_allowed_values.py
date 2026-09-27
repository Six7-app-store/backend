"""Tests for the closed value set read from a variable's ``validation``
block (``allowedValues``), which the wizard renders as a dropdown.

Pure functions — no Git clone, no FastAPI app, no DB.
"""
import pytest

from app.routers.apps import _allowed_values, _parse_one_variable


def _parse(block: str) -> dict:
    return _parse_one_variable(
        var_name="ip_mode",
        var_block=block,
        var_block_offset=0,
        file_content=block,
        file_label="terraform/variables.tf",
        source="terraform",
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
