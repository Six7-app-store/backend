"""reboot task type

Revision ID: 8b2f4c1d9e70
Revises: 682a17bf5bef
Create Date: 2026-10-22 17:00:00.000000

Adds the ``REBOOT`` value to the Postgres ``tasktype`` enum so the
single-VM reboot Celery task can persist its Task row.

Same pattern as ``0e43dd2bc856`` (redeploy): UPPERCASE label because
SQLAlchemy serialises the enum member by its ``.name``; hand-written
because autogenerate doesn't diff enum values; ``ADD VALUE`` outside
of a transaction block; ``IF NOT EXISTS`` keeps it idempotent.

Downgrade: Postgres has no ``ALTER TYPE ... DROP VALUE``, so we
rebuild the enum without ``REBOOT``. Any task rows of type ``REBOOT``
are removed first — destructive, dev/local resets only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b2f4c1d9e70'
down_revision: Union[str, None] = '682a17bf5bef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("COMMIT"))
    connection.execute(sa.text("ALTER TYPE tasktype ADD VALUE IF NOT EXISTS 'REBOOT'"))


def downgrade() -> None:
    op.execute("DELETE FROM tasks WHERE type::text = 'REBOOT'")
    op.execute("ALTER TYPE tasktype RENAME TO tasktype_old")
    op.execute(
        "CREATE TYPE tasktype AS ENUM "
        "('DEPLOY', 'UPDATE', 'DESTROY', 'PAUSE', 'RESUME', 'REDEPLOY')"
    )
    op.execute(
        "ALTER TABLE tasks ALTER COLUMN type TYPE tasktype "
        "USING type::text::tasktype"
    )
    op.execute("DROP TYPE tasktype_old")
