"""Background check: are a deployment's VMs still there in OpenStack?

VMs can be deleted outside the App Store (Horizon, CLI, quota cleanup by
an admin). The deployment row then keeps showing a running app that no
longer exists. This loop notices that and sets
``Deployment.vm_missing_since``; the UI shows a hint and lets the user
remove the entry (``DELETE /deployments/{id}?entry_only=true``).

Runs as a coroutine inside the FastAPI lifespan, next to the
reconciler. Every ``CHECK_INTERVAL_SECONDS`` it:

1. Selects live deployments whose effective status means OpenStack
   resources should exist (``RESOURCE_STATUSES``).
2. Reads the compute instance IDs from the latest OpenTofu state and
   looks each one up with the owner's credentials (same stage-1 join
   the Infrastructure tab uses).
3. Sets the marker when *every* instance answered "not found", clears
   it when at least one is found, and leaves it untouched otherwise.

Only a definite "not found" counts. A lookup that raises (timeout,
expired credentials, OpenStack down) is ``drift=stale`` and never sets
the marker — otherwise an OpenStack outage would flag every deployment.
The loop never deletes anything itself; removing the entry stays a
user decision.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import desc, text
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.database import SessionLocal
from app.models import Deployment, Task
from app.services.deployment_status import build_resource_views
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


# Distinct from RECONCILER_ADVISORY_LOCK_KEY and the per-user
# hashtext()-derived range.
VM_PRESENCE_ADVISORY_LOCK_KEY = 4242424243

CHECK_INTERVAL_SECONDS = 600

# Effective statuses (``derive_status``) where the deployment should own
# live compute instances. ``failed`` is left out on purpose: a failed
# deploy may never have created a VM, so "none found" says nothing.
RESOURCE_STATUSES: frozenset[str] = frozenset({
    "success",
    "paused",
    "pause_failed",
    "resume_failed",
})


async def run_vm_presence_check() -> None:
    """Top-level coroutine started from the FastAPI lifespan."""
    logger.info(
        "VM presence loop starting (interval=%ss)", CHECK_INTERVAL_SECONDS
    )
    try:
        while True:
            try:
                await asyncio.to_thread(check_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("VM presence pass failed; will retry next tick")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("VM presence loop cancelled — shutting down")
        raise


def check_once() -> None:
    """One pass over all candidate deployments. Sync because
    SessionLocal is sync."""
    db: Session = SessionLocal()
    try:
        if not _try_acquire_lock(db):
            logger.debug("VM presence check skipped: another instance holds the lock")
            return
        try:
            check_deployments(db)
        finally:
            _release_lock(db)
    finally:
        db.close()


def check_deployments(db: Session) -> None:
    """Check every live deployment in a resource-holding status."""
    deployments = (
        db.query(Deployment).filter(Deployment.deleted_at.is_(None)).all()
    )
    summary = crud_deployments.bulk_get_task_summary(
        db, [d.deploymentId for d in deployments]
    )
    for deployment in deployments:
        task_status, task_type, _ = summary.get(
            deployment.deploymentId, (None, None, None)
        )
        if crud_deployments.derive_status(task_status, task_type) not in RESOURCE_STATUSES:
            continue
        try:
            _check_deployment(db, deployment)
        except Exception:
            logger.exception(
                "VM presence check failed for deployment %s — continuing",
                deployment.deploymentId,
            )


def vms_missing(drifts: list[str]) -> bool | None:
    """Decide from the per-instance drift values.

    ``True``  — every instance is definitely gone
    ``False`` — at least one instance was found
    ``None``  — no verdict (no instances, or a lookup failed)
    """
    if not drifts:
        return None
    if any(d == "in_sync" for d in drifts):
        return False
    if all(d == "missing" for d in drifts):
        return True
    return None


def _check_deployment(db: Session, deployment: Deployment) -> None:
    state_json = _latest_tf_state(db, deployment)
    try:
        views = build_resource_views(
            db=db,
            user=deployment.user,
            tf_state_json=state_json,
            refresh=True,
        )
    except HTTPException as exc:
        # 412 (no credentials) / 502 (connect failed): no verdict.
        logger.info(
            "VM presence: no live lookup for deployment %s (%s)",
            deployment.deploymentId,
            exc.status_code,
        )
        return

    verdict = vms_missing([v.drift for v in views if v.category == "instance"])
    if verdict is True and deployment.vm_missing_since is None:
        logger.warning(
            "Deployment %s: all VMs gone from OpenStack — marking vm_missing",
            deployment.deploymentId,
        )
        deployment.vm_missing_since = utcnow()
        db.commit()
    elif verdict is False and deployment.vm_missing_since is not None:
        logger.info(
            "Deployment %s: VM found again — clearing vm_missing",
            deployment.deploymentId,
        )
        deployment.vm_missing_since = None
        db.commit()


def _latest_tf_state(db: Session, deployment: Deployment) -> str | None:
    task = (
        db.query(Task)
        .filter(Task.deploymentId == deployment.deploymentId)
        .filter(Task.tf_state.isnot(None))
        .order_by(desc(Task.created_at))
        .first()
    )
    return task.tf_state if task else None


def _try_acquire_lock(db: Session) -> bool:
    row = db.execute(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": VM_PRESENCE_ADVISORY_LOCK_KEY},
    ).scalar()
    db.commit()
    return bool(row)


def _release_lock(db: Session) -> None:
    db.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": VM_PRESENCE_ADVISORY_LOCK_KEY},
    )
    db.commit()
