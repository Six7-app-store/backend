"""Persist the platform maintenance gate across process and VM restarts.

Revision ID: a6f9829e14b1
Revises: a91c6b48d230
"""
from alembic import op
import sqlalchemy as sa

revision = "a6f9829e14b1"
down_revision = "a91c6b48d230"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "platform_maintenance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner", sa.String(length=64), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_platform_maintenance_singleton"),
    )
    op.execute("INSERT INTO platform_maintenance (id, owner) VALUES (1, NULL)")


def downgrade():
    op.drop_table("platform_maintenance")
