import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, asc, desc, exists, func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    Team,
    User,
    UserToDeployment,
    UserToTeam,
)
from app.schemas import DeploymentCreate
from app.utils.time import utcnow


def _is_destroyed_subq():
    """Correlated EXISTS: deployment has a successful DESTROY task."""
    return exists().where(
        (Task.deploymentId == Deployment.deploymentId)
        & (Task.type == TaskType.DESTROY)
        & (Task.status == TaskStatus.SUCCESS)
    )


def count_active_user_deployments(db: Session, user_id: UUID) -> int:
    """Number of *active* deployments owned by ``user_id``.

    "Active" here matches what the user sees on the Deployments page:

      * Owned by them (``Deployment.userId == user_id``).
      * Not soft-deleted (``deleted_at IS NULL``).
      * Has not been fully destroyed (no successful DESTROY task) —
        soft-delete happens after destroy completes, so the check
        covers the destroy-in-flight window too.
    """
    return (
        db.query(Deployment)
        .filter(Deployment.userId == user_id)
        .filter(Deployment.deleted_at.is_(None))
        .filter(~_is_destroyed_subq())
        .count()
    )


def get_deployment(
    db: Session,
    deployment_id: UUID,
    include_deleted: bool = False,
) -> Deployment | None:
    """Get deployment by ID. Hides soft-deleted rows by default.

    ``include_deleted=True`` is for the rare audit/restore lookup; the
    HTTP API never sets it.
    """
    q = db.query(Deployment).filter(Deployment.deploymentId == deployment_id)
    if not include_deleted:
        q = q.filter(Deployment.deleted_at.is_(None))
    return q.first()


def get_deployment_with_details(
    db: Session,
    deployment_id: UUID,
    include_deleted: bool = False,
) -> Deployment | None:
    """Get deployment by ID with all relations loaded. Hides soft-deleted by default."""
    q = (
        db.query(Deployment)
        .options(
            joinedload(Deployment.user),
            joinedload(Deployment.app),
            joinedload(Deployment.teams),
        )
        .filter(Deployment.deploymentId == deployment_id)
    )
    if not include_deleted:
        q = q.filter(Deployment.deleted_at.is_(None))
    return q.first()


def get_latest_task(db: Session, deployment_id: UUID) -> Task | None:
    """Get the most recent task for a deployment"""
    return (
        db.query(Task)
        .filter(Task.deploymentId == deployment_id)
        .order_by(desc(Task.created_at))
        .first()
    )


def get_first_task(db: Session, deployment_id: UUID) -> Task | None:
    """Get the first task for a deployment (when deployment was created)"""
    return (
        db.query(Task)
        .filter(Task.deploymentId == deployment_id)
        .order_by(asc(Task.created_at))
        .first()
    )


def derive_status(
    task_status: TaskStatus | None,
    task_type: TaskType | None,
) -> str | None:
    """Synthesize the effective deployment status from the latest task's
    ``(status, type)`` pair.

    ``task.status`` alone isn't enough: a destroy in flight surfaces as
    ``destroying`` and a finished destroy as ``destroyed`` (neither is a
    stored enum value). Pause/resume follow the same pattern:

    * ``(PAUSE,  pending|running)`` → ``pausing``
    * ``(PAUSE,  success)``         → ``paused``
    * ``(RESUME, pending|running)`` → ``resuming``
    * ``(RESUME, success)``         → falls through to ``success``

    PAUSE/RESUME ``failed``/``cancelled`` bleed through unchanged so the
    user sees the pause/resume itself broke. Returns ``None`` when the
    deployment has no tasks yet.
    """
    if task_status is None:
        return None

    raw_status = task_status.value
    raw_type = task_type.value if task_type else None

    if raw_type == "destroy":
        if raw_status in ("pending", "running"):
            return "destroying"
        if raw_status == "success":
            return "destroyed"
        # failed/cancelled bleed through unchanged so the user sees that
        # the destroy itself broke (vs. the original deploy succeeded).
    elif raw_type == "pause":
        if raw_status in ("pending", "running"):
            return "pausing"
        if raw_status == "success":
            return "paused"
        if raw_status == "failed":
            # Distinguish a pause failure from a deploy failure — the
            # resources are still running, only the stop pass broke.
            return "pause_failed"
        # cancelled bleeds through unchanged.
    elif raw_type == "resume":
        if raw_status in ("pending", "running"):
            return "resuming"
        if raw_status == "failed":
            # Same as pause_failed: instances are still SHUTOFF, only
            # the start pass tripped.
            return "resume_failed"
        # On resume success the deployment is running again; let
        # "success" pass through so the lifecycle matrix treats it like
        # a fresh successful deploy.
    return raw_status


def get_deployment_status(db: Session, deployment_id: UUID) -> str | None:
    """Effective deployment status for a single deployment.

    Thin wrapper around ``derive_status`` for the per-deployment path
    (detail endpoint, single-row callers). The list endpoint goes
    through ``bulk_get_task_summary`` instead so it doesn't fan out
    one query per row.

    Returns ``None`` if the deployment has no tasks yet.
    """
    task = get_latest_task(db, deployment_id)
    if task is None:
        return None
    return derive_status(task.status, task.type)


def get_deployment_created_at(db: Session, deployment_id: UUID):
    """Get deployment creation time from first task"""
    task = get_first_task(db, deployment_id)
    return task.created_at if task else None


def bulk_get_task_summary(
    db: Session, deployment_ids: list[UUID]
) -> dict[UUID, tuple[TaskStatus | None, TaskType | None, datetime | None]]:
    """Fetch the latest-task ``(status, type)`` and the first-task
    ``created_at`` for every deployment in ``deployment_ids`` — in two
    queries regardless of how many deployments are passed.

    Returns a dict keyed by ``deploymentId``. Deployments with no tasks
    are absent from the map; callers use
    ``.get(deployment_id, (None, None, None))``.
    """

    if not deployment_ids:
        return {}

    # Latest task per deployment via row_number() over (PARTITION BY ...
    # ORDER BY created_at DESC).
    latest_rn = (
        func.row_number()
        .over(partition_by=Task.deploymentId, order_by=desc(Task.created_at))
        .label("rn")
    )
    latest_subq = (
        db.query(
            Task.deploymentId.label("did"),
            Task.status.label("status"),
            Task.type.label("type"),
            latest_rn,
        )
        .filter(Task.deploymentId.in_(deployment_ids))
        .subquery()
    )
    latest_rows = (
        db.query(latest_subq.c.did, latest_subq.c.status, latest_subq.c.type)
        .filter(latest_subq.c.rn == 1)
        .all()
    )

    # First task per deployment (ascending) for created_at.
    first_rn = (
        func.row_number()
        .over(partition_by=Task.deploymentId, order_by=asc(Task.created_at))
        .label("rn")
    )
    first_subq = (
        db.query(
            Task.deploymentId.label("did"),
            Task.created_at.label("created_at"),
            first_rn,
        )
        .filter(Task.deploymentId.in_(deployment_ids))
        .subquery()
    )
    first_rows = (
        db.query(first_subq.c.did, first_subq.c.created_at)
        .filter(first_subq.c.rn == 1)
        .all()
    )

    first_map = {row.did: row.created_at for row in first_rows}
    return {
        row.did: (row.status, row.type, first_map.get(row.did))
        for row in latest_rows
    }


def get_team_members(db: Session, team_id: UUID) -> list[User]:
    """Get all users in a team"""
    user_ids = (
        db.query(UserToTeam.userId)
        .filter(UserToTeam.teamId == team_id)
        .all()
    )
    user_ids = [uid[0] for uid in user_ids]

    if not user_ids:
        return []

    return db.query(User).filter(User.userId.in_(user_ids)).all()


def get_deployment_teams_with_members(db: Session, deployment_id: UUID) -> list[dict[str, Any]]:
    """Get all teams for a deployment with their members — single query."""
    rows = (
        db.query(Team, User)
        .join(UserToTeam, UserToTeam.teamId == Team.teamId)
        .join(User, User.userId == UserToTeam.userId)
        .filter(Team.deploymentId == deployment_id)
        .all()
    )

    teams_map: dict = {}
    for team, user in rows:
        if team.teamId not in teams_map:
            teams_map[team.teamId] = {
                "teamId": team.teamId,
                "name": team.name,
                "members": [],
            }
        teams_map[team.teamId]["members"].append(
            {"userId": user.userId, "email": user.email, "username": user.username}
        )

    # Teams with no members at all won't appear in the join — add them.
    all_teams = db.query(Team).filter(Team.deploymentId == deployment_id).all()
    for team in all_teams:
        if team.teamId not in teams_map:
            teams_map[team.teamId] = {"teamId": team.teamId, "name": team.name, "members": []}

    return list(teams_map.values())


def get_team_emails_bulk(db: Session, team_ids: list) -> dict[str, list[str]]:
    """Return {team_name: [email, ...]} for all given team IDs in one query."""
    if not team_ids:
        return {}
    rows = (
        db.query(Team.name, User.email)
        .join(UserToTeam, UserToTeam.teamId == Team.teamId)
        .join(User, User.userId == UserToTeam.userId)
        .filter(Team.teamId.in_(team_ids))
        .all()
    )
    result: dict[str, list[str]] = {}
    for team_name, email in rows:
        result.setdefault(team_name, []).append(email)
    return result


def get_deployment_outputs(db: Session, deployment_id: UUID) -> dict[str, Any] | None:
    """Get parsed Terraform outputs from the latest successful task"""
    task = (
        db.query(Task)
        .filter(Task.deploymentId == deployment_id)
        .filter(Task.outputs.isnot(None))
        .order_by(desc(Task.created_at))
        .first()
    )

    if task and task.outputs:
        try:
            return json.loads(task.outputs)
        except json.JSONDecodeError:
            return None
    return None


def get_latest_successful_deploy_outputs(
    db: Session, deployment_id: UUID
) -> dict[str, Any] | None:
    """Parsed Terraform outputs of the most recent *successful DEPLOY* task.

    Stricter than :func:`get_deployment_outputs`, which returns the latest
    task carrying any non-null ``outputs`` regardless of type or status — a
    DESTROY task's (empty) outputs could win there. Credential extraction
    must read the outputs of an actual successful deploy, so this helper
    filters to ``type == DEPLOY`` and ``status == SUCCESS``.

    Returns ``None`` when no such task exists or its ``outputs`` are absent
    / unparseable. Shared by ``resend_user_access`` and the per-member
    ``/my-access`` endpoint so both agree on which outputs are authoritative.
    """
    task = (
        db.query(Task)
        .filter(
            Task.deploymentId == deployment_id,
            Task.type == TaskType.DEPLOY,
            Task.status == TaskStatus.SUCCESS,
        )
        .order_by(desc(Task.created_at))
        .first()
    )

    if task and task.outputs:
        try:
            return json.loads(task.outputs) if isinstance(task.outputs, str) else task.outputs
        except json.JSONDecodeError:
            return None
    return None


def get_deployments(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    user_id: UUID | None = None,
    member_user_id: UUID | None = None,
    app_id: UUID | None = None,
    status: str | None = None,
    include_deleted: bool = False,
) -> list[Deployment]:
    """Get deployments with optional filters. Hides soft-deleted by default.

    ``user_id`` filters by deployment owner (``Deployment.userId``).

    ``member_user_id`` filters by owner or membership: a deployment
    matches when the user is the creator OR appears in any team's
    ``UserToTeam`` row OR has a direct ``UserToDeployment`` mapping.
    Mutually exclusive with ``user_id``; if both are set ``user_id``
    wins.
    """
    query = db.query(Deployment)

    if not include_deleted:
        # Backed by the partial index ix_deployments_live so this stays
        # cheap even with many rows.
        query = query.filter(Deployment.deleted_at.is_(None))
    if user_id:
        query = query.filter(Deployment.userId == user_id)
    elif member_user_id:
        # Owner OR team member OR direct mapping, via a subquery on the
        # user's teamIds so the OR doesn't explode into a cartesian.
        member_team_ids = (
            db.query(UserToTeam.teamId).filter(UserToTeam.userId == member_user_id)
        )
        member_deployment_ids_via_teams = (
            db.query(Team.deploymentId).filter(Team.teamId.in_(member_team_ids))
        )
        member_deployment_ids_direct = (
            db.query(UserToDeployment.deploymentId)
            .filter(UserToDeployment.userId == member_user_id)
        )
        query = query.filter(
            (Deployment.userId == member_user_id)
            | (Deployment.deploymentId.in_(member_deployment_ids_via_teams))
            | (Deployment.deploymentId.in_(member_deployment_ids_direct))
        )
    if app_id:
        query = query.filter(Deployment.appId == app_id)

    # Filter by effective status. The exposed status is derived from the
    # LATEST task per deployment (see ``derive_status``), so we join a
    # window-function subquery pinning the latest task and apply the
    # equivalent predicate here — before offset/limit — so the page size
    # stays correct.
    if status:
        latest_rn = (
            func.row_number()
            .over(partition_by=Task.deploymentId, order_by=desc(Task.created_at))
            .label("rn")
        )
        latest_subq = (
            db.query(
                Task.deploymentId.label("did"),
                Task.status.label("status"),
                Task.type.label("type"),
                latest_rn,
            ).subquery()
        )
        query = query.join(
            latest_subq,
            and_(
                latest_subq.c.did == Deployment.deploymentId,
                latest_subq.c.rn == 1,
            ),
        )

        if status == "destroying":
            query = query.filter(
                latest_subq.c.type == TaskType.DESTROY,
                latest_subq.c.status.in_((TaskStatus.PENDING, TaskStatus.RUNNING)),
            )
        elif status == "destroyed":
            query = query.filter(
                latest_subq.c.type == TaskType.DESTROY,
                latest_subq.c.status == TaskStatus.SUCCESS,
            )
        else:
            # Plain task statuses, mirroring ``derive_status``:
            # - ``pending``/``running``/``success`` match deploy-typed
            #   tasks only (destroy surfaces as destroying/destroyed).
            # - ``failed``/``cancelled`` bleed through both task types.
            try:
                status_enum = TaskStatus(status)
            except ValueError:
                # Unknown status string → empty result.
                return []
            query = query.filter(latest_subq.c.status == status_enum)
            if status_enum in (
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.SUCCESS,
            ):
                query = query.filter(latest_subq.c.type != TaskType.DESTROY)

    # Order by deploymentId (UUID)
    query = query.order_by(desc(Deployment.deploymentId))

    return query.offset(skip).limit(limit).all()


def create_deployment(db: Session, deployment: DeploymentCreate, user_id: UUID) -> Deployment:
    """Insert a deployment row in the current transaction.

    Does NOT commit — the caller is expected to also insert teams/tasks
    in the same TX and commit once at the end. This is necessary so the
    advisory lock acquired at the start of the request stays held across
    all related inserts.
    """
    # Convert userInputVar dict to JSON string for database storage
    user_input_var_json = None
    if deployment.userInputVar is not None:
        user_input_var_json = json.dumps(deployment.userInputVar)

    db_deployment = Deployment(
        name=deployment.name,
        appId=deployment.appId,
        userId=user_id,
        releaseTag=deployment.releaseTag,
        userInputVar=user_input_var_json,
    )
    db.add(db_deployment)
    db.flush()
    db.refresh(db_deployment)
    return db_deployment

def soft_delete_deployment(db: Session, deployment_id: UUID) -> bool:
    """Mark a deployment as deleted without removing the row.

    Sets ``deleted_at = utcnow()`` so default queries skip it. The
    related tasks/teams/user-mappings are intentionally untouched —
    they're useful for audit and the partial-unique index on active
    tasks already prevents the deployment from accepting new work.

    Returns ``False`` if the deployment doesn't exist (or was already
    deleted), ``True`` on a successful soft-delete.
    """
    db_deployment = get_deployment(db, deployment_id)
    if not db_deployment:
        return False
    db_deployment.deleted_at = utcnow()
    db.commit()
    return True


def create_user_to_deployments(
    db: Session,
    deployment_id: UUID,
    user_ids: set[UUID],
) -> list[UserToDeployment]:
    """
    Create UserToDeployment entries for multiple users
    """
    user_to_deployments = []

    for user_id in user_ids:
        user_to_deployment = UserToDeployment(
            userId=user_id,
            deploymentId=deployment_id
        )
        db.add(user_to_deployment)
        user_to_deployments.append(user_to_deployment)

    return user_to_deployments
