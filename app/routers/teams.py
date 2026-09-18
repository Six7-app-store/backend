from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import teams as crud_teams
from app.database import get_db
from app.models import User
from app.schemas import TeamCreate, TeamResponse, TeamUpdate, TeamWithMembers
from app.utils.auth import get_current_user
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
    Get all teams, optionally filtered to a single deployment.

    All authenticated users may list teams; per-team membership gating
    happens at ``GET /teams/{team_id}``.
    """
    teams = crud_teams.get_teams(db, skip=skip, limit=limit, deployment_id=deployment_id)
    return teams


# ----------------------------------------------------------------
# GET TEAM BY ID
# ----------------------------------------------------------------
@router.get("/{team_id}", response_model=TeamWithMembers)
def get_team(
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get team by ID with all members"""
    team = crud_teams.get_team(db, team_id)
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found"
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
