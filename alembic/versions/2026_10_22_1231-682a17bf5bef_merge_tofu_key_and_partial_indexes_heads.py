"""merge tofu key and partial indexes heads

``661aa473b510`` (user input var tofu key, from main) and ``3942d7fc7d18``
(restore the partial indexes, from the backend refactor on develop) both
descend from ``b988e7ef2934`` and only met when develop and main were
brought together again. That left two heads, and ``alembic upgrade head`` —
the command the staging playbook, the prod runbook and autoupdate.sh run —
refuses to start in that state ("Multiple head revisions are present").
This revision joins the two branches; it changes no schema.

Revision ID: 682a17bf5bef
Revises: 661aa473b510, 3942d7fc7d18
Create Date: 2026-10-22 12:31:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '682a17bf5bef'
down_revision: str | Sequence[str] | None = ('661aa473b510', '3942d7fc7d18')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
