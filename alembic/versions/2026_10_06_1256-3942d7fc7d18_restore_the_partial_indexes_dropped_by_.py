"""restore the partial indexes dropped by the redeploy migration

Migration 73fd123a60aa ("redeploy") was generated with --autogenerate while the
models did not declare these three partial indexes, so Alembic read them as
"in the database but not in the models" and dropped them. A database migrated
through it has been running without them since:

* ``uq_tasks_active_per_deployment``: the only thing that stops two PENDING or
  RUNNING tasks on one deployment. Without it ``prepare_task_in_tx`` could only
  rely on a read that two concurrent requests both pass.
* ``ix_deployments_live`` and ``ix_apps_live``: the "live rows only" lookups.

The models declare all three now (``app/models.py``), so the next autogenerate
leaves them alone.

Revision ID: 3942d7fc7d18
Revises: b988e7ef2934
Create Date: 2026-10-06 12:56:12.616257

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3942d7fc7d18'
down_revision: str | None = 'b988e7ef2934'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The unique index cannot be built while the data already violates it, and
    # while the index was missing nothing stopped two active tasks from landing
    # on one deployment. Refuse with a readable message instead of Postgres'
    # "could not create unique index", and do not pick a winner here: which of
    # the tasks is the real one is a decision for a person looking at the rows.
    duplicates = op.get_bind().execute(sa.text(
        'SELECT "deploymentId", count(*) FROM tasks '
        "WHERE status IN ('PENDING', 'RUNNING') "
        'GROUP BY "deploymentId" HAVING count(*) > 1'
    )).fetchall()
    if duplicates:
        listing = ", ".join(f"{row[0]} ({row[1]} active tasks)" for row in duplicates)
        raise RuntimeError(
            "Cannot create uq_tasks_active_per_deployment: these deployments have "
            f"more than one PENDING/RUNNING task: {listing}. Mark the stale tasks "
            "as FAILED or CANCELLED, then run the migration again."
        )

    op.create_index(
        'uq_tasks_active_per_deployment', 'tasks', ['deploymentId'],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        if_not_exists=True,
    )
    op.create_index(
        'ix_deployments_live', 'deployments', ['deploymentId'],
        unique=False,
        postgresql_where=sa.text('deleted_at IS NULL'),
        if_not_exists=True,
    )
    op.create_index(
        'ix_apps_live', 'apps', ['appId'],
        unique=False,
        postgresql_where=sa.text('deleted_at IS NULL'),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        'uq_tasks_active_per_deployment', table_name='tasks',
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        if_exists=True,
    )
    op.drop_index(
        'ix_deployments_live', table_name='deployments',
        postgresql_where=sa.text('deleted_at IS NULL'),
        if_exists=True,
    )
    op.drop_index(
        'ix_apps_live', table_name='apps',
        postgresql_where=sa.text('deleted_at IS NULL'),
        if_exists=True,
    )
