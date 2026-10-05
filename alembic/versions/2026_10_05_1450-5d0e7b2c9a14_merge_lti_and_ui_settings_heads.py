"""merge lti and ui settings heads

``ea769204620c`` (LTI context memberships URL) and ``3f1c2a7b9d4e``
(ui settings) were both created on top of ``b988e7ef2934``, which left
the history with two heads. ``alembic upgrade head`` refuses to run in
that state ("Multiple head revisions are present"), and that is exactly
the command the staging playbook, the prod runbook and autoupdate.sh
issue. This revision joins the two branches; it changes no schema.

Revision ID: 5d0e7b2c9a14
Revises: ea769204620c, 3f1c2a7b9d4e
Create Date: 2026-10-05 14:50:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = '5d0e7b2c9a14'
down_revision: str | Sequence[str] | None = ('ea769204620c', '3f1c2a7b9d4e')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
