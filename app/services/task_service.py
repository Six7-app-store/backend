"""Task lifecycle helpers.

Two-phase dispatch:

1. `prepare_task_in_tx` — INSERT a PENDING task row in the caller's
   transaction (no commit, no Celery I/O). The caller commits the
   surrounding business state and the new task row atomically.

2. `dispatch_to_celery` — outside the original TX, push the task to
   RabbitMQ. On success the row's `celeryTaskId` is stamped; on
   failure the row is marked FAILED. Either way the user sees a row
   in the deployment list reflecting reality — no splitbrain.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.crud import tasks as crud_tasks
from app.models import Task, TaskStatus, TaskType
from app.services.maintenance_gate import guard_task_admission

logger = logging.getLogger(__name__)


# Name of the Postgres partial unique index backing the "one active
# task per deployment" rule. String-matched when translating
# IntegrityError → ActiveTaskExistsError so other unique-constraint
# violations aren't swallowed.
_ACTIVE_TASK_UNIQUE_INDEX = "uq_tasks_active_per_deployment"


class ActiveTaskExistsError(Exception):
    """A PENDING/RUNNING task already exists for this deployment."""


def prepare_task_in_tx(
    db: Session,
    deployment_id: uuid.UUID,
    task_type: TaskType,
) -> Task:
    """Insert a PENDING task row in the caller's transaction.

    Does NOT call `db.commit()` — the caller is responsible for
    committing the surrounding state alongside this row, so that
    deployment + teams + task are all visible (or all rolled back)
    atomically.

    Raises `ActiveTaskExistsError` if the deployment already has a
    PENDING/RUNNING task. A Postgres partial unique index enforces this
    at the DB level too: the pre-check catches the common case, and the
    ``except IntegrityError`` around ``db.flush()`` catches the racy one
    (two concurrent requests both pass the pre-check).
    """
    guard_task_admission(db)
    existing = crud_tasks.get_tasks(db, deployment_id=deployment_id)
    for task in existing:
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            raise ActiveTaskExistsError(
                f"Deployment {deployment_id} already has active task {task.taskId}"
            )

    db_task = Task(
        deploymentId=deployment_id,
        type=task_type,
        status=TaskStatus.PENDING,
        celeryTaskId=None,
    )
    db.add(db_task)
    try:
        db.flush()
    except IntegrityError as e:
        # Race: another transaction inserted a PENDING/RUNNING task for
        # the same deployment between our pre-check and flush. Translate
        # the constraint violation into the domain exception so the
        # caller's 409 branch fires.
        if _ACTIVE_TASK_UNIQUE_INDEX in str(e.orig):
            raise ActiveTaskExistsError(
                f"Deployment {deployment_id} already has an active task "
                "(detected via DB unique constraint)"
            ) from e
        raise
    db.refresh(db_task)
    return db_task


def dispatch_to_celery(
    db: Session,
    task: Task,
    celery_task_name: str,
    celery_args: list,
) -> tuple[Task, str]:
    """Push a prepared task to Celery.

    MUST be called after the surrounding TX committed. Runs in fresh
    transactions so the task row is updated independently of any later
    request-handling commit.

    On `send_task` failure the task is marked FAILED and the original
    exception is re-raised — the caller turns that into a 503.
    """
    try:
        result = celery_app.send_task(celery_task_name, args=celery_args)
    except Exception:
        logger.exception(
            "Celery dispatch failed for task %s (deployment %s)",
            task.taskId,
            task.deploymentId,
        )
        task.status = TaskStatus.FAILED
        task.logs = "Failed to dispatch to Celery"
        db.commit()
        db.refresh(task)
        raise

    task.celeryTaskId = result.id
    db.commit()
    db.refresh(task)
    logger.info("Task %s dispatched as celery task %s", task.taskId, result.id)
    return task, result.id
