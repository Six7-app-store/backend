"""Operator CLI for a durable, fail-closed platform maintenance gate.

Runs in a backend container against its configured application database. No
credentials, task IDs, logs or deployment details are printed.
"""
from __future__ import annotations

import argparse
import sys
import time

from sqlalchemy import text

from app.celery_app import celery_app
from app.database import SessionLocal
from app.services.maintenance_gate import acquire_maintenance, release_maintenance


def begin_and_drain(owner: str, timeout: float, poll: float = 2.0) -> bool:
    """Block new tasks, wait for existing ones, and release only on timeout.

    A DB/process error after acquisition deliberately leaves the durable gate
    owned: the operator must investigate and explicitly release it.
    """
    if timeout < 0 or poll <= 0:
        raise ValueError("invalid wait duration")
    with SessionLocal() as db:
        if not acquire_maintenance(db, owner):
            db.rollback()
            return False
        db.commit()

    deadline = time.monotonic() + timeout
    while True:
        with SessionLocal() as db:
            status = db.execute(
                text("SELECT owner FROM platform_maintenance WHERE id = 1")
            ).scalar_one()
            if status != owner:
                raise RuntimeError("maintenance ownership lost")
            count = db.execute(
                text("SELECT count(*) FROM tasks WHERE status IN ('PENDING', 'RUNNING')")
            ).scalar_one()
        if count == 0:
            return True
        if time.monotonic() >= deadline:
            with SessionLocal() as db:
                if not release_maintenance(db, owner):
                    raise RuntimeError("maintenance ownership lost")
                db.commit()
            return False
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


def end(owner: str) -> bool:
    with SessionLocal() as db:
        released = release_maintenance(db, owner)
        db.commit()
        return released


def workers_idle() -> bool:
    """Require responsive workers and empty active, reserved and scheduled sets.

    Payloads may contain credentials. Only this boolean leaves the process.
    The broker's ready/unacknowledged counts are checked separately by the
    deployment coordinator; neither check alone is sufficient.
    """
    inspector = celery_app.control.inspect(timeout=3)
    reports = (inspector.active(), inspector.reserved(), inspector.scheduled())
    if any(not isinstance(report, dict) or not report for report in reports):
        return False
    nodes = set(reports[0])
    if any(set(report) != nodes for report in reports):
        return False
    return all(isinstance(tasks, list) and not tasks for report in reports for tasks in report.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("begin", "end", "verify-workers"))
    parser.add_argument("--owner")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    try:
        if args.action == "verify-workers":
            result = workers_idle()
        elif not args.owner:
            parser.error("--owner is required for begin/end")
        else:
            result = begin_and_drain(args.owner, args.timeout) if args.action == "begin" else end(args.owner)
    except Exception:
        # Avoid logging DSNs or provider errors to publicly visible workflow logs.
        print("Maintenance operation failed; check the gate before continuing.", file=sys.stderr)
        return 1
    if args.action == "verify-workers":
        print("Workers are idle." if result else "Workers busy or unreachable.")
    else:
        print("Maintenance gate acquired and drained." if args.action == "begin" and result else
              "Maintenance gate released." if result else "Maintenance unavailable or tasks still active.")
    return 0 if result else 2


if __name__ == "__main__":
    raise SystemExit(main())
