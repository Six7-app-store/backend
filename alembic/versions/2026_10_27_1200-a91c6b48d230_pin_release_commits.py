"""Record immutable Git commits for approvals and deployments.

Existing rows remain NULL: the current tag cannot prove what was reviewed or
deployed historically. They need operator review instead of a blind backfill.
"""
from alembic import op
import sqlalchemy as sa

revision = "a91c6b48d230"
down_revision = "d893c109f8e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("app_version_approvals", sa.Column("commit_sha", sa.String(40), nullable=True))
    op.add_column("deployments", sa.Column("release_commit", sa.String(40), nullable=True))


def downgrade():
    op.drop_column("deployments", "release_commit")
    op.drop_column("app_version_approvals", "commit_sha")
