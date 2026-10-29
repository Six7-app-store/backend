"""Transactionally prevent new tasks while a platform update is in progress."""
from sqlalchemy import text
from sqlalchemy.orm import Session


class MaintenanceInProgress(Exception):
    """An update holds the gate, so no new worker task may be created."""


def guard_task_admission(db: Session) -> None:
    """Hold a shared row lock until the task insertion transaction commits.

    Maintenance acquisition updates the same row. It therefore waits for any
    already-started admission transaction, and once committed all later
    admissions see its owner. The row is inserted by the migration; a missing
    row is an error, not permission to proceed.
    """
    row = db.execute(
        text("SELECT owner FROM platform_maintenance WHERE id = 1 FOR SHARE")
    ).one_or_none()
    if row is None:
        raise RuntimeError("platform maintenance gate is missing")
    if row.owner is not None:
        raise MaintenanceInProgress()


def acquire_maintenance(db: Session, owner: str) -> bool:
    """Claim the durable gate, waiting for in-flight admissions to commit.

    Caller must commit the transaction before waiting for tasks. A rejected
    claim never steals a gate belonging to another update.
    """
    if not owner or len(owner) > 64:
        raise ValueError("invalid maintenance owner")
    return db.execute(
        text(
            "UPDATE platform_maintenance SET owner = :owner "
            "WHERE id = 1 AND owner IS NULL RETURNING id"
        ),
        {"owner": owner},
    ).scalar_one_or_none() is not None


def release_maintenance(db: Session, owner: str) -> bool:
    """Release only the gate owned by this update, then commit separately."""
    return db.execute(
        text(
            "UPDATE platform_maintenance SET owner = NULL "
            "WHERE id = 1 AND owner = :owner RETURNING id"
        ),
        {"owner": owner},
    ).scalar_one_or_none() is not None
