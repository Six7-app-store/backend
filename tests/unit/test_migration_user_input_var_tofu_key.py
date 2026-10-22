"""Row conversion of migration ``661aa473b510`` (userInputVar → ``tofu``).

The migration body only loops over rows; the decisions live in
``_to_tofu`` / ``_to_terraform``, which are tested here directly.
"""

import importlib.util
import pathlib

import pytest

_PATH = next(
    (pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions").glob(
        "*-661aa473b510_user_input_var_tofu_key.py"
    )
)
_spec = importlib.util.spec_from_file_location("migration_661aa473b510", _PATH)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


@pytest.mark.unit
def test_terraform_block_becomes_tofu_and_packer_is_dropped():
    data = {"terraform": {"flavor": "m1.small"}, "packer": {"base": "ubuntu"}, "other": 1}
    assert migration._to_tofu(data) == {"tofu": {"flavor": "m1.small"}, "other": 1}


@pytest.mark.unit
def test_packer_only_row_gets_an_empty_tofu_block():
    assert migration._to_tofu({"packer": {"x": 1}}) == {"tofu": {}}


@pytest.mark.unit
def test_row_without_old_keys_is_left_alone():
    assert migration._to_tofu({"tofu": {"a": 1}}) is None


@pytest.mark.unit
def test_downgrade_restores_terraform_and_empty_packer():
    assert migration._to_terraform({"tofu": {"a": 1}, "other": 2}) == {
        "terraform": {"a": 1},
        "packer": {},
        "other": 2,
    }


@pytest.mark.unit
def test_downgrade_leaves_rows_without_tofu_alone():
    assert migration._to_terraform({"terraform": {}}) is None


class _FakeConnection:
    """Stands in for ``op.get_bind()``: serves rows, records UPDATEs."""

    def __init__(self, rows):
        self._rows = rows
        self.updates = {}

    def execute(self, statement, params=None):
        if params is None:
            result = type("R", (), {})()
            result.fetchall = lambda: self._rows
            return result
        self.updates[params["id"]] = params["value"]
        return None


@pytest.mark.unit
def test_upgrade_rewrites_only_convertible_rows(monkeypatch):
    import json

    conn = _FakeConnection(
        [
            ("d1", json.dumps({"terraform": {"a": 1}, "packer": {}})),
            ("d2", json.dumps({"tofu": {"b": 2}})),
            ("d3", "not json"),
            ("d4", json.dumps(["a", "list"])),
        ]
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

    migration.upgrade()

    assert set(conn.updates) == {"d1"}
    assert json.loads(conn.updates["d1"]) == {"tofu": {"a": 1}}


@pytest.mark.unit
def test_downgrade_rewrites_tofu_rows(monkeypatch):
    import json

    conn = _FakeConnection([("d1", json.dumps({"tofu": {"a": 1}}))])
    monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

    migration.downgrade()

    assert json.loads(conn.updates["d1"]) == {"terraform": {"a": 1}, "packer": {}}
