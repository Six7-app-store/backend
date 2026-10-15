from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Deployment, Team, User, UserRole, UserToTeam
from app.schemas import TeamCreate, TeamUpdate


def get_team(db: Session, team_id: UUID) -> Team | None:
    """Get team by ID"""
    return db.query(Team).filter(Team.teamId == team_id).first()


def get_teams(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    deployment_id: UUID | None = None
) -> list[Team]:
    """Get teams with optional deployment filter."""
    query = db.query(Team)

    if deployment_id:
        query = query.filter(Team.deploymentId == deployment_id)

    return query.order_by(Team.name, Team.teamId).offset(skip).limit(limit).all()


def is_team_member(db: Session, team_id: UUID, user_id: UUID) -> bool:
    """Whether ``user_id`` belongs to ``team_id`` (a ``UserToTeam`` row exists)."""
    return (
        db.query(UserToTeam.userToTeamId)
        .filter(UserToTeam.teamId == team_id, UserToTeam.userId == user_id)
        .first()
        is not None
    )


def get_teams_for_member(
    db: Session,
    user_id: UUID,
    deployment_id: UUID,
    skip: int = 0,
    limit: int = 100,
) -> list[Team]:
    """Teams of one deployment that ``user_id`` is a member of."""
    return (
        db.query(Team)
        .join(UserToTeam, UserToTeam.teamId == Team.teamId)
        .filter(Team.deploymentId == deployment_id, UserToTeam.userId == user_id)
        .order_by(Team.name, Team.teamId)
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_teams_visible_to(
    db: Session, user: User, skip: int = 0, limit: int = 100
) -> list[Team]:
    """Teams a caller may list without naming a deployment.

    Admins see every team of every live deployment. Everybody else sees the
    teams they belong to plus all teams of the deployments they own — the same
    split ``GET /deployments/{id}`` makes between owner view and member view.
    """
    query = (
        db.query(Team)
        .join(Deployment, Deployment.deploymentId == Team.deploymentId)
        .filter(Deployment.deleted_at.is_(None))
    )
    if user.role != UserRole.ADMIN:
        member_team_ids = db.query(UserToTeam.teamId).filter(UserToTeam.userId == user.userId)
        query = query.filter(
            or_(Deployment.userId == user.userId, Team.teamId.in_(member_team_ids))
        )
    return query.order_by(Team.name, Team.teamId).offset(skip).limit(limit).all()


def _add_team_members(db: Session, team: Team, user_ids: list[UUID]) -> None:
    """Stage ``UserToTeam`` membership rows for ``team``.

    Adds one association row per user id to the session without
    committing or flushing — the caller controls transaction
    boundaries. ``team.teamId`` must already be populated (via a prior
    commit or flush) so the foreign key can be set.
    """
    for user_id in user_ids:
        user_to_team = UserToTeam(
            userId=user_id,
            teamId=team.teamId
        )
        db.add(user_to_team)


def create_team(db: Session, team: TeamCreate) -> Team:
    """Create a new team.

    ``Team`` has a NOT NULL ``deploymentId`` FK, so the request payload
    must carry the deployment to attach to.
    """
    db_team = Team(
        name=team.name,
        deploymentId=team.deploymentId
    )
    db.add(db_team)
    # Commit the team row first so ``teamId`` is populated before staging
    # memberships.
    db.commit()
    db.refresh(db_team)

    _add_team_members(db, db_team, team.userIds)

    db.commit()
    db.refresh(db_team)
    return db_team


def update_team(db: Session, team_id: UUID, team_update: TeamUpdate) -> Team | None:
    """Update team information"""
    db_team = get_team(db, team_id)
    if not db_team:
        return None

    update_data = team_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_team, field, value)

    db.commit()
    db.refresh(db_team)
    return db_team


def delete_team(db: Session, team_id: UUID) -> bool:
    """Delete a team"""
    db_team = get_team(db, team_id)
    if not db_team:
        return False

    db.delete(db_team)
    db.commit()
    return True


def add_user_to_team(db: Session, team_id: UUID, user_id: UUID) -> bool:
    """Add a user to a team"""
    # Check if already exists
    existing = db.query(UserToTeam).filter(
        UserToTeam.teamId == team_id,
        UserToTeam.userId == user_id
    ).first()

    if existing:
        return False

    user_to_team = UserToTeam(
        userId=user_id,
        teamId=team_id
    )
    db.add(user_to_team)
    db.commit()
    return True


def remove_user_from_team(db: Session, team_id: UUID, user_id: UUID) -> bool:
    """Remove a user from a team"""
    user_to_team = db.query(UserToTeam).filter(
        UserToTeam.teamId == team_id,
        UserToTeam.userId == user_id
    ).first()

    if not user_to_team:
        return False

    db.delete(user_to_team)
    db.commit()
    return True


def create_teams_for_deployment(
    db: Session,
    deployment_id: UUID,
    teams_data: list[dict]
) -> list[Team]:
    """
    Create multiple teams for a deployment
    teams_data format: [{"name": "team1", "userIds": [uuid1, uuid2]}, ...]
    """
    created_teams = []

    for team_data in teams_data:
        # Create team
        db_team = Team(
            name=team_data["name"],
            deploymentId=deployment_id
        )
        db.add(db_team)
        db.flush()  # Get team ID

        _add_team_members(db, db_team, team_data.get("userIds", []))

        created_teams.append(db_team)

    return created_teams
