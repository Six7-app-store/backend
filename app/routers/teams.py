from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import teams as crud_teams
from app.database import get_db
from app.models import User
from app.schemas import TeamCreate, TeamResponse, TeamUpdate, TeamWithMembers
from app.utils.auth import get_current_user
from app.utils.capabilities import (
    can_view_deployment_owner,
    ensure_view_deployment_member,
)
from app.utils.permissions import require_staff

router = APIRouter()


# ----------------------------------------------------------------
# GET ALL TEAMS
# ----------------------------------------------------------------
@router.get("/", response_model=list[TeamResponse])
def list_teams(
    skip: int = 0,
    limit: int = 100,
    deployment_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List the teams the caller is allowed to see, optionally for one deployment.

    Same split as ``GET /deployments/{id}``: the owner view (owner, admin,
    course-teacher of the owner's course) sees every team of a deployment, every
    other caller only the teams they belong to. Naming a deployment the caller
    has no access to at all is a 403. Without ``deployment_id`` the caller gets
    their own teams plus the teams of deployments they own; admins get all.
    """
    if deployment_id is None:
        return crud_teams.get_teams_visible_to(db, current_user, skip=skip, limit=limit)

    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found"
        )
    ensure_view_deployment_member(current_user, deployment, db)
    if can_view_deployment_owner(current_user, deployment, db):
        return crud_teams.get_teams(db, skip=skip, limit=limit, deployment_id=deployment_id)
    return crud_teams.get_teams_for_member(
        db, current_user.userId, deployment_id, skip=skip, limit=limit
    )


# ----------------------------------------------------------------
# GET TEAM BY ID
# ----------------------------------------------------------------
@router.get("/{team_id}", response_model=TeamWithMembers)
def get_team(
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get team by ID with all members.

    Owner view of the team's deployment (owner, admin, course-teacher) or a
    member of this very team; everyone else gets a 403.
    """
    team = crud_teams.get_team(db, team_id)
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )
    deployment = crud_deployments.get_deployment(db, team.deploymentId)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )
    if not can_view_deployment_owner(current_user, deployment, db) and not (
        crud_teams.is_team_member(db, team.teamId, current_user.userId)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "team_view_forbidden"},
        )
    return team


# ----------------------------------------------------------------
# CREATE TEAM (TEACHER/ADMIN ONLY)
# ----------------------------------------------------------------
@router.post("/", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def create_team(
    team: TeamCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Create a new team
    - **Requires**: TEACHER or ADMIN role
    """
    return crud_teams.create_team(db, team)


# ----------------------------------------------------------------
# UPDATE TEAM (TEACHER/ADMIN ONLY)
# ----------------------------------------------------------------
@router.put("/{team_id}", response_model=TeamResponse)
def update_team(
    team_id: UUID,
    team_update: TeamUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Update a team
    - **Requires**: TEACHER or ADMIN role
    """
    team = crud_teams.update_team(db, team_id, team_update)
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )
    return team


# ----------------------------------------------------------------
# DELETE TEAM (TEACHER/ADMIN ONLY)
# ----------------------------------------------------------------
@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Delete a team
    - **Requires**: TEACHER or ADMIN role
    """
    success = crud_teams.delete_team(db, team_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
        )
    return None


# ----------------------------------------------------------------
# ADD USER TO TEAM (TEACHER/ADMIN ONLY)
# ----------------------------------------------------------------
@router.post("/{team_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def add_user_to_team(
    team_id: UUID,
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Add a user to a team
    - **Requires**: TEACHER or ADMIN role
    """
    success = crud_teams.add_user_to_team(db, team_id, user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already in team or team not found"
        )
    return None


# ----------------------------------------------------------------
# REMOVE USER FROM TEAM (TEACHER/ADMIN ONLY)
# ----------------------------------------------------------------
@router.delete("/{team_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user_from_team(
    team_id: UUID,
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff)
):
    """
    Remove a user from a team
    - **Requires**: TEACHER or ADMIN role
    """
    success = crud_teams.remove_user_from_team(db, team_id, user_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not in team or team not found"
        )
    return None
