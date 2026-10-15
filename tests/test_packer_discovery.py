"""Unit tests for ``_discover_packer_templates``.

The discovery helper picks one of two layouts under ``<repo>/packer``
and rejects ambiguous or unsafe states. These tests pin the rules so
a future refactor can't silently change them — multi-image apps in
production depend on the legacy/multi distinction and on the
``[a-z][a-z0-9_-]{0,30}`` key allowlist.
"""

import os

import pytest

from app.services.hcl_variable_parser import (
    PackerTemplateDiscoveryError,
    _discover_packer_templates,
    _PackerTemplate,
)


def _touch(path: str, content: str = "") -> None:
    """Create file + parent dirs. Stdlib-only so the helper stays
    runnable on any worker without conftest fixtures."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


# 1. Empty repo (no packer/) → []
def test_no_packer_directory_returns_empty(tmp_path):
    # No packer/ at all — this is a Terraform-only app and discovery
    # must say so without raising.
    result = _discover_packer_templates(str(tmp_path))
    assert result == []


# 2. Legacy: packer/template.pkr.hcl → [_PackerTemplate(key="default", ...)]
def test_legacy_single_template_returns_default(tmp_path):
    packer_dir = tmp_path / "packer"
    packer_dir.mkdir()
    legacy = packer_dir / "template.pkr.hcl"
    legacy.write_text("source \"openstack\" \"x\" {}\n")
    # Variables file is optional — include it to exercise the
    # ``variables_path`` field too.
    (packer_dir / "variables.pkr.hcl").write_text("")

    result = _discover_packer_templates(str(tmp_path))
    assert len(result) == 1
    assert result[0].key == "default"
    assert result[0].template_path == str(legacy)
    assert result[0].variables_path == str(packer_dir / "variables.pkr.hcl")


# 3. Multi: packer/webserver/template.pkr.hcl + packer/database/template.pkr.hcl
#    → sorted list with both
def test_multi_template_returns_sorted_list(tmp_path):
    packer_dir = tmp_path / "packer"
    _touch(str(packer_dir / "webserver" / "template.pkr.hcl"))
    _touch(str(packer_dir / "webserver" / "variables.pkr.hcl"))
    _touch(str(packer_dir / "database" / "template.pkr.hcl"))
    _touch(str(packer_dir / "database" / "variables.pkr.hcl"))

    result = _discover_packer_templates(str(tmp_path))
    keys = [t.key for t in result]
    # ``database`` < ``webserver`` alphabetically — sorted order is
    # part of the contract because downstream phase tracking expects
    # deterministic ordering.
    assert keys == ["database", "webserver"]
    for t in result:
        assert isinstance(t, _PackerTemplate)
        assert t.template_path.endswith(f"{t.key}/template.pkr.hcl")
        assert t.variables_path.endswith(f"{t.key}/variables.pkr.hcl")


# 4. Multi with ignored subdir: packer/webserver/template.pkr.hcl
#    + packer/_common/scripts/ → just webserver
def test_multi_template_ignores_non_template_subdirs(tmp_path):
    packer_dir = tmp_path / "packer"
    _touch(str(packer_dir / "webserver" / "template.pkr.hcl"))
    # ``_common/`` is a shared-scripts directory — no
    # ``template.pkr.hcl`` at its root, so discovery must skip it
    # without complaining about the underscore-prefixed name.
    _touch(str(packer_dir / "_common" / "scripts" / "install.sh"))
    # ``http/`` is the classic Packer http-server boot directory —
    # also skipped silently.
    _touch(str(packer_dir / "http" / "preseed.cfg"))

    result = _discover_packer_templates(str(tmp_path))
    assert [t.key for t in result] == ["webserver"]


# 5. Coexistence: packer/template.pkr.hcl + packer/webserver/template.pkr.hcl
#    → PackerTemplateDiscoveryError
def test_legacy_and_multi_layout_coexist_raises(tmp_path):
    packer_dir = tmp_path / "packer"
    packer_dir.mkdir()
    (packer_dir / "template.pkr.hcl").write_text("")
    _touch(str(packer_dir / "webserver" / "template.pkr.hcl"))

    with pytest.raises(PackerTemplateDiscoveryError) as excinfo:
        _discover_packer_templates(str(tmp_path))
    # Message must call out both layouts so the app author can
    # diagnose without re-reading the discovery code.
    assert "BOTH" in str(excinfo.value) or "both" in str(excinfo.value).lower()
    assert "webserver" in str(excinfo.value)


# 6. Bad key: packer/Web-Server/template.pkr.hcl → PackerTemplateDiscoveryError
def test_invalid_key_raises(tmp_path):
    packer_dir = tmp_path / "packer"
    # Uppercase is rejected because the key is embedded in
    # Terraform variable names (``image_name_<key>``) and lowercase
    # snake/kebab is the only safe shape.
    _touch(str(packer_dir / "Web-Server" / "template.pkr.hcl"))

    with pytest.raises(PackerTemplateDiscoveryError) as excinfo:
        _discover_packer_templates(str(tmp_path))
    assert "Web-Server" in str(excinfo.value)


# 7. Multi without variables.pkr.hcl: returns the template,
#    variables_path points to a non-existent file (caller checks isfile).
def test_multi_template_without_variables_file(tmp_path):
    packer_dir = tmp_path / "packer"
    _touch(str(packer_dir / "webserver" / "template.pkr.hcl"))
    # Deliberately no variables.pkr.hcl — discovery still returns
    # the template, and the variables_path points to a path that
    # doesn't exist on disk. Callers gate the read with
    # ``os.path.isfile``.

    result = _discover_packer_templates(str(tmp_path))
    assert len(result) == 1
    assert result[0].key == "webserver"
    assert not os.path.isfile(result[0].variables_path)
    assert result[0].variables_path.endswith("variables.pkr.hcl")
