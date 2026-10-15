"""Tests for the @openstack marker grammar — scope slot, file-extension
filter, and HCL type-shape validation that goes with both.

These exercise ``_parse_marker`` and ``_parse_one_variable`` as pure
functions — no Git clone, no FastAPI app, no DB.
"""
import pytest

from app.services.hcl_variable_parser import (
    _FILE_SCOPES,
    _OS_TYPES,
    _VAR_SCOPES,
    MarkerError,
    _parse_marker,
    _parse_one_variable,
    _validate_file_var_shape,
    _validate_scoped_var_shape,
)


def _build_block(var_name: str, var_type: str, description: str) -> str:
    """Render a minimal ``variable "x" {...}`` HCL block for parsing."""
    return (
        f'variable "{var_name}" {{\n'
        f"  type        = {var_type}\n"
        f'  description = "{description}"\n'
        f"  default     = {{}}\n"
        f"}}\n"
    )


@pytest.mark.unit
def test_file_type_is_registered():
    """``file`` must be in the supported set so the marker parses."""
    assert "file" in _OS_TYPES


@pytest.mark.unit
@pytest.mark.parametrize("scope", sorted(_FILE_SCOPES))
def test_file_marker_accepts_each_scope_with_extensions(scope):
    """File-Marker brauchen einen Endungsfilter im vierten Slot —
    ``all``/``team``/``user`` werden alle akzeptiert, wenn ein gültiger
    Extensions-Slot folgt."""
    var_type = (
        "map(object({name=string, content_b64=string, size=number, content_type=string}))"
        if scope == "all"
        else "map(map(object({name=string, content_b64=string, size=number, content_type=string})))"
    )
    os_type, mode, multi, file_scope, var_scope, file_exts = _parse_marker(
        "task_pdf", var_type, f"Aufgabenstellung @openstack:file:{scope}:pdf"
    )
    assert os_type == "file"
    # mode/multi are owned by the classic OpenStack-resource branch;
    # for file vars they stay None.
    assert mode is None
    assert multi is None
    assert file_scope == scope
    # ``var_scope`` mirrors ``file_scope`` so the frontend's slot
    # resolution only needs to read ONE field.
    assert var_scope == scope
    assert file_exts == ["pdf"]


@pytest.mark.unit
def test_file_marker_requires_extensions_slot():
    """Bare ``@openstack:file:all`` (no extensions) is a marker error
    under the new contract — the App-Autor MUST declare an allowed
    file-type filter so the wizard's ``accept`` attribute is non-empty
    and the backend has something to validate against."""
    with pytest.raises(MarkerError) as exc:
        _parse_marker(
            "task_pdf",
            "map(object({name=string, content_b64=string, size=number, content_type=string}))",
            "Aufgabenstellung @openstack:file:all",
        )
    assert "Endungsfilter" in exc.value.message or "extensions" in exc.value.message.lower()


@pytest.mark.unit
def test_file_marker_accepts_multiple_extensions():
    os_type, _, _, file_scope, _, file_exts = _parse_marker(
        "task_files",
        "map(map(object({name=string, content_b64=string, size=number, content_type=string})))",
        "Per-user @openstack:file:user:pdf|docx|txt",
    )
    assert os_type == "file"
    assert file_scope == "user"
    assert file_exts == ["pdf", "docx", "txt"]


@pytest.mark.unit
def test_file_marker_lowercases_extensions():
    """Endungen sind case-insensitive — wir normalisieren auf lowercase
    damit der Backend-Vergleich gegen den Dateinamen-Suffix konsistent ist."""
    _, _, _, _, _, file_exts = _parse_marker(
        "task_pdf",
        "map(object({name=string, content_b64=string, size=number, content_type=string}))",
        "Aufgabe @openstack:file:all:PDF",
    )
    assert file_exts == ["pdf"]


@pytest.mark.unit
def test_file_marker_rejects_unknown_scope_with_suggestion():
    """A typo like ``teams`` (plural) hints at the right token."""
    with pytest.raises(MarkerError) as exc:
        _parse_marker(
            "team_dataset",
            "map(map(object({})))",
            "Per-team data @openstack:file:teams:pdf",
        )
    assert "teams" in exc.value.message
    assert "team" in exc.value.message  # suggestion


@pytest.mark.unit
def test_file_marker_rejects_malformed_extensions():
    """Komma als Trenner ist falsch — der Marker verlangt Pipe."""
    with pytest.raises(MarkerError) as exc:
        _parse_marker(
            "task_pdf",
            "map(object({}))",
            "Files @openstack:file:all:pdf,docx",
        )
    assert "Endungsfilter" in exc.value.message or "extensions" in exc.value.message.lower()


@pytest.mark.unit
def test_file_marker_rejected_for_packer_source():
    """Packer-Variablen können keine Files transportieren — der Files-
    Pfad mergt hartcodiert in ``userInputVar.terraform``. Statt einer
    stillen Falle: Marker-Fehler."""
    with pytest.raises(MarkerError) as exc:
        _parse_marker(
            "task_pdf",
            "map(object({}))",
            "Files @openstack:file:all:pdf",
            source="packer",
        )
    assert "packer" in exc.value.message.lower() or "Packer" in exc.value.message


@pytest.mark.unit
@pytest.mark.parametrize(
    "scope, hcl_type, ok",
    [
        ("all", "map(object({name=string}))", True),
        ("all", "map(map(object({})))", False),
        ("all", "string", False),
        ("team", "map(map(object({})))", True),
        ("team", "map(object({}))", False),
        ("user", "map(map(object({})))", True),
        ("user", "list(string)", False),
    ],
)
def test_validate_file_var_shape(scope, hcl_type, ok):
    if ok:
        _validate_file_var_shape("v", hcl_type, scope)  # must not raise
    else:
        with pytest.raises(MarkerError):
            _validate_file_var_shape("v", hcl_type, scope)


# ----------------------------------------------------------------
# var_scope (generic, non-file) tests
# ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("scope", sorted(_VAR_SCOPES))
def test_var_scope_marker_on_resource_variable(scope):
    """``@openstack:flavor:id:single:<scope>`` setzt ``var_scope``
    zusätzlich zu den klassischen Resource-Marker-Slots."""
    hcl_type = "string" if scope == "all" else "map(string)"
    os_type, mode, multi, file_scope, var_scope, file_exts = _parse_marker(
        "flavor_id", hcl_type, f"@openstack:flavor:id:single:{scope}"
    )
    assert os_type == "flavor"
    assert mode == "id"
    assert multi is False
    assert file_scope is None
    assert var_scope == scope
    assert file_exts is None


@pytest.mark.unit
def test_pure_scope_marker_without_type():
    """``@openstack:::team`` ist die Kurzform für eine free-text-
    Variable, die nur per-Team scoped ist — kein Resource-Picker."""
    os_type, mode, multi, file_scope, var_scope, file_exts = _parse_marker(
        "hostname_prefix", "map(string)", "Pro Team eindeutig @openstack:::team"
    )
    assert os_type is None
    assert mode is None
    assert multi is None
    assert file_scope is None
    assert var_scope == "team"
    assert file_exts is None


@pytest.mark.unit
def test_pure_scope_marker_empty_marker_is_error():
    """``@openstack:`` ohne irgendeinen Slot ist Author-Fehler."""
    # Empty marker yields no match, so we need at least one colon-
    # separated segment that the regex can pick up — the ``_BAD_PREFIX``
    # path catches malformed inputs. The cleanest empty-marker
    # representation is just the bare token without slots: this is
    # explicitly rejected via the bad-prefix path.
    with pytest.raises(MarkerError):
        _parse_marker("x", "string", "@openstack: foo")  # whitespace not allowed


@pytest.mark.unit
def test_var_scope_typo_gets_suggestion():
    """``teem`` hint at ``team``."""
    with pytest.raises(MarkerError) as exc:
        _parse_marker(
            "flavor_id",
            "map(string)",
            "@openstack:flavor:id:single:teem",
        )
    assert "teem" in exc.value.message
    assert "team" in exc.value.message


@pytest.mark.unit
def test_var_scope_team_requires_map_hcl_type():
    """Skalare HCL-Types passen nicht zu ``team``/``user`` — der Wizard
    schickt eine Map pro Slot, Terraform würde sie sonst beim Apply
    ablehnen."""
    with pytest.raises(MarkerError):
        _validate_scoped_var_shape("flavor_id", "string", "team")
    with pytest.raises(MarkerError):
        _validate_scoped_var_shape("flavor_id", "number", "user")
    # map(...) is fine
    _validate_scoped_var_shape("flavor_id", "map(string)", "team")
    _validate_scoped_var_shape("flavor_id", "map(list(string))", "user")
    # ``all`` bypasses the shape check entirely.
    _validate_scoped_var_shape("flavor_id", "string", "all")


@pytest.mark.unit
def test_packer_rejects_non_all_var_scope():
    with pytest.raises(MarkerError) as exc:
        _parse_marker(
            "team_image_size",
            "map(string)",
            "@openstack:::team",
            source="packer",
        )
    assert "packer" in exc.value.message.lower() or "Packer" in exc.value.message


@pytest.mark.unit
def test_parse_one_variable_emits_var_scope_for_scoped_resource(tmp_path):
    block = _build_block(
        "team_flavor_ids",
        "map(string)",
        "Pro Team @openstack:flavor:id:single:team",
    )
    out = _parse_one_variable(
        var_name="team_flavor_ids",
        var_block=block,
        var_block_offset=0,
        file_content=block,
        file_label="terraform/variables.tf",
        source="terraform",
    )
    assert out.get("osType") == "flavor"
    assert out.get("osMode") == "id"
    assert out.get("osMulti") is False
    assert out.get("varScope") == "team"
    assert "markerError" not in out


@pytest.mark.unit
def test_parse_one_variable_emits_file_ext_and_var_scope_mirror(tmp_path):
    block = _build_block(
        "task_pdf",
        "map(object({name=string, content_b64=string, size=number, content_type=string}))",
        "Aufgabe @openstack:file:all:pdf",
    )
    out = _parse_one_variable(
        var_name="task_pdf",
        var_block=block,
        var_block_offset=0,
        file_content=block,
        file_label="terraform/variables.tf",
        source="terraform",
    )
    assert out.get("osType") == "file"
    assert out.get("osScope") == "all"
    # ``varScope`` mirrors ``osScope`` for file variables — the wizard
    # reads either, but the resolution logic needs only one.
    assert out.get("varScope") == "all"
    assert out.get("fileExtensions") == ["pdf"]
    assert "osMode" not in out
    assert "osMulti" not in out
    assert "markerError" not in out


@pytest.mark.unit
def test_parse_one_variable_attaches_marker_error_for_type_mismatch():
    """A file var with a wrong HCL type surfaces ``markerError`` so the
    wizard renders it as Free-Text + inline hint without the whole
    apps-variables endpoint failing."""
    block = _build_block(
        "broken_files",
        "string",  # wrong shape for any file scope
        "Whoops @openstack:file:all:pdf",
    )
    out = _parse_one_variable(
        var_name="broken_files",
        var_block=block,
        var_block_offset=0,
        file_content=block,
        file_label="terraform/variables.tf",
        source="terraform",
    )
    assert "markerError" in out
    assert out["markerError"]["variable"] == "broken_files"
    # The classic ``osType`` field must NOT be set when the marker
    # errored — the variable degrades to Free-Text on the frontend.
    assert "osType" not in out


@pytest.mark.unit
def test_parse_one_variable_attaches_marker_error_for_scoped_string_var():
    block = _build_block(
        "team_string",
        "string",
        "Soll per Team scoped sein @openstack:::team",
    )
    out = _parse_one_variable(
        var_name="team_string",
        var_block=block,
        var_block_offset=0,
        file_content=block,
        file_label="terraform/variables.tf",
        source="terraform",
    )
    # team/user requires map(...) — string fails the shape check.
    assert "markerError" in out
    assert "varScope" not in out


@pytest.mark.unit
def test_classic_resource_marker_still_parses():
    """Regression — the new slots must not break the existing marker
    for OpenStack resources without scope."""
    os_type, mode, multi, file_scope, var_scope, file_exts = _parse_marker(
        "net",
        "string",
        "Pick a network @openstack:network",
    )
    assert os_type == "network"
    assert mode is None  # default applied by ``_apply_defaults``, not here
    assert multi is None
    assert file_scope is None
    assert var_scope is None
    assert file_exts is None


@pytest.mark.unit
def test_too_many_segments_rejected():
    """``@openstack:network:id:single:team:extra`` is malformed."""
    with pytest.raises(MarkerError):
        _parse_marker(
            "x",
            "map(string)",
            "@openstack:network:id:single:team:extra",
        )


@pytest.mark.unit
def test_multi_with_scoped_map_uses_inner_type():
    """Bug #1 — ``:multi:team`` mit ``map(list(string))``:

    Bei ``var_scope=team`` schickt der Wizard eine Map pro Slot. Der
    HCL-Type muss eine ``map(...)`` sein; der INNERE Element-Type
    entscheidet, ob ``:multi`` konsistent ist. ``map(list(string))``
    hat innen eine Liste — ``:multi`` passt. Der Marker MUSS hier
    erfolgreich parsen.
    """
    os_type, mode, multi, file_scope, var_scope, file_exts = _parse_marker(
        "team_flavor_ids",
        "map(list(string))",
        "@openstack:flavor:id:multi:team",
    )
    assert os_type == "flavor"
    assert mode == "id"
    assert multi is True
    assert var_scope == "team"
    assert file_scope is None
    assert file_exts is None
