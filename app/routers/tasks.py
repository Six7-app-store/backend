"""
Tasks Router

Read-only access to task information for a deployment. Tasks are created by the
deployment flow itself; this router exposes status and details so the frontend
can render progress.

Every endpoint enforces ``ensure_deployment_access`` (to prevent IDOR) plus
``ensure_deployment_owner_view``, so only the deployment creator, teachers, and
admins can read task logs — which contain Terraform outputs, IPs, and worker
stack traces. Members get a 403 even though they can read deployment metadata.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import tasks as crud_tasks
from app.database import get_db
from app.models import User
from app.schemas import TaskResponse
from app.utils.auth import get_current_user
from app.utils.permissions import ensure_deployment_access, ensure_deployment_owner_view

router = APIRouter()


@router.get("/deployment/{deployment_id}", response_model=list[TaskResponse])
def get_deployment_tasks(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all tasks for a deployment the caller has owner-access to."""
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    ensure_deployment_access(deployment, current_user, db)
    ensure_deployment_owner_view(deployment, current_user)
    return crud_tasks.get_tasks(db, deployment_id=deployment_id)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a single task; only the deployment owner-view sees it."""
    task = crud_tasks.get_task(db, task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    deployment = crud_deployments.get_deployment(db, task.deploymentId)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment for task not found",
        )
    ensure_deployment_access(deployment, current_user, db)
    ensure_deployment_owner_view(deployment, current_user)
    return task
