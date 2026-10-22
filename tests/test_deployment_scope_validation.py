"""Unit tests for the deployment-side scope/extension enforcement
helpers added alongside the marker-grammar extension.

Covers:
* ``_attach_files_to_user_input`` extension-filter enforcement
* ``_validate_scoped_user_input`` shape + slot-target checks

These are pure functions — no DB, no FastAPI app, no Celery.
"""
import base64

import pytest
from fastapi import HTTPException

from app.routers.deployments import (
    _attach_files_to_user_input,
    _validate_scoped_user_input,
)
from app.schemas import DeploymentFileUpload, Team


def _payload(name: str, content: bytes) -> DeploymentFileUpload:
    """Build the Pydantic model the wizard ships per slot."""
    return DeploymentFileUpload(
        name=name,
        content_b64=base64.b64encode(content).decode("ascii"),
        size=len(content),
        content_type="application/octet-stream",
    )


# ----------------------------------------------------------------
# File-extension filter (defense-in-depth on top of the wizard's
# ``accept`` attribute).
# ----------------------------------------------------------------

VAR_PDF = {
    "name": "task_pdf",
    "osType": "file",
    "osScope": "all",
    "varScope": "all",
    "fileExtensions": ["pdf"],
}


@pytest.mark.unit
def test_file_extension_filter_accepts_matching_suffix():
    out = _attach_files_to_user_input(
        user_input_var={"tofu": {}},
        files={"task_pdf": {"all": _payload("aufgabe.pdf", b"%PDF-1.4...")}},
        variable_definitions=[VAR_PDF],
    )
    assert "task_pdf" in out["tofu"]
    assert out["tofu"]["task_pdf"]["all"]["name"] == "aufgabe.pdf"


@pytest.mark.unit
def test_file_extension_filter_rejects_mismatched_suffix():
    with pytest.raises(HTTPException) as exc:
        _attach_files_to_user_input(
            user_input_var={"tofu": {}},
            files={"task_pdf": {"all": _payload("malware.exe", b"\x4d\x5a")}},
            variable_definitions=[VAR_PDF],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "file_extension_rejected"
    assert exc.value.detail["allowed"] == ["pdf"]


@pytest.mark.unit
def test_file_extension_filter_is_case_insensitive():
    """``Aufgabe.PDF`` matches a ``pdf`` filter — uploaders shouldn't
    care about file-extension case."""
    out = _attach_files_to_user_input(
        user_input_var={"tofu": {}},
        files={"task_pdf": {"all": _payload("Aufgabe.PDF", b"%PDF-1.4...")}},
        variable_definitions=[VAR_PDF],
    )
    assert "task_pdf" in out["tofu"]


@pytest.mark.unit
def test_file_extension_filter_skipped_without_definitions():
    """When no variable_definitions are passed, the helper falls back
    to its pre-change behavior — no filter applied. Keeps the helper
    usable from older code paths that don't yet load definitions."""
    out = _attach_files_to_user_input(
        user_input_var={"tofu": {}},
        files={"some_var": {"all": _payload("anything.tar", b"...")}},
        variable_definitions=None,
    )
    assert "some_var" in out["tofu"]


# ----------------------------------------------------------------
# Scoped user-input validation (team / user slot key allowlist).
# ----------------------------------------------------------------

VAR_TEAM_FLAVOR = {
    "name": "team_flavor_ids",
    "osType": "flavor",
    "osMode": "id",
    "osMulti": False,
    "varScope": "team",
}

VAR_USER_HOST = {
    "name": "user_hostname_prefix",
    "varScope": "user",
}


def _team(name: str, user_ids: list[str]) -> Team:
    return Team(name=name, userIds=user_ids)


@pytest.mark.unit
def test_scoped_team_ok_with_matching_team_names():
    _validate_scoped_user_input(
        user_input_var={
            "tofu": {"team_flavor_ids": {"Team-1": "m1.small", "Team-2": "m1.medium"}},
        },
        variable_definitions=[VAR_TEAM_FLAVOR],
        teams_payload=[_team("Team-1", ["u1"]), _team("Team-2", ["u2"])],
    )


@pytest.mark.unit
def test_scoped_team_rejects_unknown_team_name():
    with pytest.raises(HTTPException) as exc:
        _validate_scoped_user_input(
            user_input_var={
                "tofu": {"team_flavor_ids": {"Team-X": "m1.small"}},
            },
            variable_definitions=[VAR_TEAM_FLAVOR],
            teams_payload=[_team("Team-1", ["u1"])],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "unknown_scope_target"
    assert exc.value.detail["scope"] == "team"


@pytest.mark.unit
def test_scoped_team_rejects_non_map_value():
    with pytest.raises(HTTPException) as exc:
        _validate_scoped_user_input(
            user_input_var={"tofu": {"team_flavor_ids": "m1.small"}},
            variable_definitions=[VAR_TEAM_FLAVOR],
            teams_payload=[_team("Team-1", ["u1"])],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "scoped_var_not_map"


@pytest.mark.unit
def test_scoped_user_accepts_composite_keys_under_known_team():
    _validate_scoped_user_input(
        user_input_var={
            "tofu": {
                "user_hostname_prefix": {
                    "Team-1-alice": "alice-vm",
                    "Team-1-bob": "bob-vm",
                },
            },
        },
        variable_definitions=[VAR_USER_HOST],
        teams_payload=[_team("Team-1", ["uid-alice", "uid-bob"])],
    )


@pytest.mark.unit
def test_scoped_user_rejects_key_with_unknown_team_prefix():
    with pytest.raises(HTTPException) as exc:
        _validate_scoped_user_input(
            user_input_var={
                "tofu": {
                    "user_hostname_prefix": {"Other-alice": "alice-vm"},
                },
            },
            variable_definitions=[VAR_USER_HOST],
            teams_payload=[_team("Team-1", ["u1"])],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "unknown_scope_target"


@pytest.mark.unit
def test_scoped_var_skipped_when_value_absent():
    """Eine scoped Variable, die der User leer gelassen hat, darf nicht
    fälschlich als Fehler erscheinen — der HCL-Default soll greifen."""
    _validate_scoped_user_input(
        user_input_var={"tofu": {}},
        variable_definitions=[VAR_TEAM_FLAVOR],
        teams_payload=[_team("Team-1", ["u1"])],
    )


@pytest.mark.unit
def test_non_scoped_variable_is_ignored():
    """Variablen ohne ``varScope`` werden vom Scope-Validator ignoriert
    — sie laufen weiterhin durch die normalen OpenTofu-Type-Checks."""
    _validate_scoped_user_input(
        user_input_var={"tofu": {"some_string": "foo"}},
        variable_definitions=[{"name": "some_string"}],
        teams_payload=[],
    )
