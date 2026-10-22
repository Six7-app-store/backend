"""ui settings

Adds ``ui_settings``: the accent colour and the three logos an admin
can set for the instance. A single row, created on the first write;
without it every value falls back to the frontend's built-in default.

Revision ID: 3f1c2a7b9d4e
Revises: b988e7ef2934
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f1c2a7b9d4e'
down_revision: Union[str, None] = 'b988e7ef2934'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ui_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('accent_color', sa.String(length=7), nullable=True),
        sa.Column('logo_light', sa.LargeBinary(), nullable=True),
        sa.Column('logo_light_mime', sa.String(length=64), nullable=True),
        sa.Column('logo_dark', sa.LargeBinary(), nullable=True),
        sa.Column('logo_dark_mime', sa.String(length=64), nullable=True),
        sa.Column('logo_icon', sa.LargeBinary(), nullable=True),
        sa.Column('logo_icon_mime', sa.String(length=64), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('ui_settings')
