"""Background reconciler for stuck Celery tasks.

The Celery event listener is the primary path for syncing task state
into the DB, but events can be lost (backend restart, dropped message,
worker death mid-handler), leaving tasks stuck in PENDING/RUNNING.

This reconciler runs as a coroutine inside the FastAPI lifespan. Every
30 seconds it:

1. Selects all tasks in PENDING or RUNNING.
2. Reconciles each against its Celery AsyncResult.
3. Flips tasks with ``celeryTaskId IS NULL`` older than the grace
   window to FAILED (dispatch never completed).

Multi-instance safety: a Postgres session-scoped advisory lock gates
each pass, so only one FastAPI process runs the reconcile body per
tick. The pass is idempotent regardless.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from celery.result import AsyncResult
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.crud import locks as crud_locks
from app.crud import tasks as crud_tasks
from app.database import SessionLocal
from app.models import Task, TaskStatus
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# Constant key for the advisory lock. Distinct from the per-user lock
# helper's hashtext()-derived range.
RECONCILER_ADVISORY_LOCK_KEY = 4242424242

RECONCILE_INTERVAL_SECONDS = 30

# Grace window for a celery_id-NULL task (dispatch committed the row
# but send_task never stamped an id). Older than this → fair game.
DISPATCH_GRACE_SECONDS = 60

# Grace window for a Celery PENDING state with a known celery_id (in
# RabbitMQ but no worker picked it up). Older → treated as lost.
CELERY_PENDING_GRACE_SECONDS = 3600


async def run_reconciler() -> None:
    """Top-level coroutine started from the FastAPI lifespan."""
    logger.info(
        "Reconciler loop starting (interval=%ss)",
        RECONCILE_INTERVAL_SECONDS,
    )
    try:
        while True:
            try:
                await asyncio.to_thread(_reconcile_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reconciler pass failed; will retry next tick")
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Reconciler loop cancelled — shutting down")
        raise


def _reconcile_once() -> None:
    """One reconciliation pass. Sync because SessionLocal is sync."""
    db: Session = SessionLocal()
    try:
        if not _try_acquire_lock(db):
            logger.debug("Reconciler skipped: another instance holds the lock")
            return

        try:
            stuck = (
                db.query(Task)
                .filter(Task.status.in_((TaskStatus.PENDING, TaskStatus.RUNNING)))
                .all()
            )
            if stuck:
                logger.debug("Reconciler examining %d stuck task(s)", len(stuck))
            for task in stuck:
                try:
                    _reconcile_task(db, task)
                except Exception:
                    logger.exception(
                        "Failed to reconcile task %s — continuing",
                        task.taskId,
                    )
        finally:
            _release_lock(db)
    finally:
        db.close()


def _try_acquire_lock(db: Session) -> bool:
    row = db.execute(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": RECONCILER_ADVISORY_LOCK_KEY},
    ).scalar()
    # The first SELECT opens an implicit TX; commit it so the read
    # doesn't tie up the connection. (Lock is session-scoped.)
    db.commit()
    return bool(row)


def _release_lock(db: Session) -> None:
    db.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": RECONCILER_ADVISORY_LOCK_KEY},
    )
    db.commit()


def _reconcile_task(db: Session, task: Task) -> None:
    now = utcnow()

    # Per-deployment advisory lock — serialises against the request
    # handlers so the reconciler's "stuck task → FAILED" decision can't
    # race a handler's status read. Xact-scoped; released on the first
    # ``update_task`` commit below.
    crud_locks.acquire_deployment_xact_lock(db, task.deploymentId)

    if task.celeryTaskId is None:
        age = now - task.created_at if task.created_at else timedelta(0)
        if age >= timedelta(seconds=DISPATCH_GRACE_SECONDS):
            logger.warning(
                "Marking task %s FAILED: celery_id NULL after %ss",
                task.taskId,
                int(age.total_seconds()),
            )
            crud_tasks.update_task(db, task.taskId, {
                "status": TaskStatus.FAILED,
                "finished_at": now,
                "logs": "Reconciler: Celery dispatch did not complete (no task id stamped)",
            })
        return

    async_result = AsyncResult(task.celeryTaskId, app=celery_app)
    state = async_result.state  # PENDING / STARTED / SUCCESS / FAILURE / REVOKED ...

    if state == "SUCCESS":
        logs_str, tf_state_str, outputs_str = _extract_success_payload(async_result.result)
        crud_tasks.update_task(db, task.taskId, {
            "status": TaskStatus.SUCCESS,
            "finished_at": now,
            "logs": logs_str,
            "tf_state": tf_state_str,
            "outputs": outputs_str,
        })
        logger.info("Reconciled task %s -> SUCCESS", task.taskId)

    elif state == "FAILURE":
        crud_tasks.update_task(db, task.taskId, {
            "status": TaskStatus.FAILED,
            "finished_at": now,
            "logs": _extract_failure_logs(async_result),
        })
        logger.info("Reconciled task %s -> FAILED", task.taskId)

    elif state == "REVOKED":
        crud_tasks.update_task(db, task.taskId, {
            "status": TaskStatus.CANCELLED,
            "finished_at": now,
        })
        logger.info("Reconciled task %s -> CANCELLED", task.taskId)

    elif state == "STARTED" and task.status == TaskStatus.PENDING:
        # task-started event was lost; promote PENDING -> RUNNING.
        crud_tasks.update_task(db, task.taskId, {
            "status": TaskStatus.RUNNING,
            "started_at": task.started_at or now,
        })
        logger.info("Reconciled task %s -> RUNNING (task-started event missed)", task.taskId)

    elif state == "PENDING":
        age = now - task.created_at if task.created_at else timedelta(0)
        if age >= timedelta(seconds=CELERY_PENDING_GRACE_SECONDS):
            logger.warning(
                "Marking task %s FAILED: stuck in Celery PENDING for %ss",
                task.taskId,
                int(age.total_seconds()),
            )
            crud_tasks.update_task(db, task.taskId, {
                "status": TaskStatus.FAILED,
                "finished_at": now,
                "logs": "Reconciler: task stuck in Celery PENDING beyond grace window",
            })


def _extract_success_payload(result):
    """Mirror the success-event handler's shape so reconciled rows
    look identical to event-driven rows."""
    if not isinstance(result, dict):
        return None, None, None
    logs_data = result.get("logs")
    tf_state = result.get("tf_state")
    outputs = result.get("tofu_outputs")
    logs_str = (
        json.dumps(logs_data, ensure_ascii=False) if isinstance(logs_data, list)
        else logs_data if isinstance(logs_data, str)
        else None
    )
    tf_state_str = (
        tf_state if isinstance(tf_state, str)
        else (json.dumps(tf_state) if tf_state else None)
    )
    outputs_str = (
        outputs if isinstance(outputs, str)
        else (json.dumps(outputs) if outputs else None)
    )
    return logs_str, tf_state_str, outputs_str


def _extract_failure_logs(async_result: AsyncResult) -> str:
    try:
        info = async_result.info
        if isinstance(info, BaseException):
            return f"Task failed: {type(info).__name__}: {str(info)[:500]}"
        return f"Task failed: {str(info)[:500]}"
    except Exception:
        return "Task failed (reconciler could not retrieve traceback)"
