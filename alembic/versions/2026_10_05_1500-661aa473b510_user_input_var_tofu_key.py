"""user input var tofu key

The platform runs OpenTofu only and no longer builds images (ADR 0010 in
the deployment repository). ``deployments.userInputVar`` kept the wizard
input under the keys ``terraform`` and ``packer``; the backend and worker
now read a single ``tofu`` block. This moves the ``terraform`` block to
``tofu`` and drops ``packer`` in every stored row, so the detail view and
the file download keep working on deployments created before the switch.

Rows whose value is not a JSON object are left untouched — the application
already treats those as "no input".

Downgrade restores ``terraform`` and an empty ``packer`` block. The packer
values themselves are gone after upgrade; there is nothing left that
could use them.

Revision ID: 661aa473b510
Revises: 5d0e7b2c9a14
Create Date: 2026-10-05 15:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '661aa473b510'
down_revision: Union[str, None] = '5d0e7b2c9a14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _rewrite(convert) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text('SELECT "deploymentId", "userInputVar" FROM deployments '
                'WHERE "userInputVar" IS NOT NULL')
    ).fetchall()
    for deployment_id, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        converted = convert(data)
        if converted is None:
            continue
        connection.execute(
            sa.text('UPDATE deployments SET "userInputVar" = :value '
                    'WHERE "deploymentId" = :id'),
            {"value": json.dumps(converted), "id": deployment_id},
        )


def _to_tofu(data: dict) -> dict | None:
    if "terraform" not in data and "packer" not in data:
        return None
    out = {k: v for k, v in data.items() if k not in ("terraform", "packer")}
    out["tofu"] = data.get("terraform") or {}
    return out


def _to_terraform(data: dict) -> dict | None:
    if "tofu" not in data:
        return None
    out = {k: v for k, v in data.items() if k != "tofu"}
    out["terraform"] = data["tofu"]
    out["packer"] = {}
    return out


def upgrade() -> None:
    _rewrite(_to_tofu)


def downgrade() -> None:
    _rewrite(_to_terraform)
