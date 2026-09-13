"""Per-user OpenStack credential management.

All endpoints scope to the caller (`current_user`) — there is no
`{user_id}` path parameter. This makes IDOR impossible by construction.
The masked response never returns identifier or secret material.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import locks as crud_locks
from app.crud import openstack_credentials as crud_creds
from app.database import get_db
from app.models import User
from app.schemas import (
    OpenStackCredentialFromYaml,
    OpenStackCredentialResponse,
    OpenStackCredentialUpsert,
)
from app.services import clouds_yaml_parser, openstack_validator
from app.utils.auth import get_current_user

router = APIRouter()


def _lock_state(db: Session, user_id) -> tuple[bool, int]:
    n = crud_deployments.count_active_user_deployments(db, user_id)
    return (n > 0, n)


def _assert_unlocked(db: Session, user: User) -> None:
    """Refuse credential mutation while the user has non-destroyed deployments."""
    is_locked, n = _lock_state(db, user.userId)
    if is_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "openstack_credentials_locked",
                "active_deployments": n,
            },
        )


def _to_response(
    row,
    *,
    has_credential: bool,
    is_locked: bool,
    active_deployments: int,
) -> OpenStackCredentialResponse:
    if row is None:
        return OpenStackCredentialResponse(
            has_credential=False,
            is_locked=is_locked,
            active_deployments=active_deployments,
        )
    return OpenStackCredentialResponse(
        auth_type=row.auth_type,
        auth_url=row.auth_url,
        region_name=row.region_name,
        interface=row.interface,
        identity_api_version=row.identity_api_version,
        project_id=row.project_id,
        project_name=row.project_name,
        user_domain_name=row.user_domain_name,
        project_domain_name=row.project_domain_name,
        has_credential=has_credential,
        last_validated_at=row.last_validated_at,
        last_validation_error=row.last_validation_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_locked=is_locked,
        active_deployments=active_deployments,
    )


@router.get(
    "/me/openstack-credentials",
    response_model=OpenStackCredentialResponse,
)
def get_my_credentials(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Always 200 — returns has_credential=False when none configured.

    Lock state is included regardless so the frontend can render the
    correct guard UI without a second request.
    """
    row = crud_creds.get_for_user(db, current_user.userId)
    is_locked, n = _lock_state(db, current_user.userId)
    return _to_response(
        row,
        has_credential=row is not None,
        is_locked=is_locked,
        active_deployments=n,
    )


@router.put(
    "/me/openstack-credentials",
    response_model=OpenStackCredentialResponse,
)
def upsert_my_credentials(
    payload: OpenStackCredentialUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Auto-validates against Keystone before persisting.

    On a successful authorize we record `last_validated_at`. On failure
    we still persist the row (so the user can fix it via the UI) and
    record the human-readable error message.
    """
    crud_locks.acquire_user_xact_lock(db, current_user.userId)
    _assert_unlocked(db, current_user)
    result = openstack_validator.validate(payload)
    row = crud_creds.upsert(db, current_user.userId, payload, result)
    is_locked, n = _lock_state(db, current_user.userId)
    return _to_response(row, has_credential=True, is_locked=is_locked, active_deployments=n)


@router.put(
    "/me/openstack-credentials/from-yaml",
    response_model=OpenStackCredentialResponse,
)
def upsert_my_credentials_from_yaml(
    body: OpenStackCredentialFromYaml,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    crud_locks.acquire_user_xact_lock(db, current_user.userId)
    _assert_unlocked(db, current_user)
    payload = clouds_yaml_parser.parse(body.clouds_yaml, body.cloud_name)
    result = openstack_validator.validate(payload)
    row = crud_creds.upsert(db, current_user.userId, payload, result)
    is_locked, n = _lock_state(db, current_user.userId)
    return _to_response(row, has_credential=True, is_locked=is_locked, active_deployments=n)


@router.post(
    "/me/openstack-credentials/test",
    response_model=OpenStackCredentialResponse,
)
def test_my_credentials(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-authorize the stored credential and refresh validation metadata.

    Allowed even while locked — purely read-only against Keystone.
    """
    row = crud_creds.get_for_user(db, current_user.userId)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No OpenStack credentials configured",
        )
    plaintext = crud_creds.get_decrypted_for_backend(db, current_user.userId)
    payload = OpenStackCredentialUpsert(
        auth_type=row.auth_type,
        auth_url=row.auth_url,
        region_name=row.region_name,
        interface=row.interface,
        identity_api_version=row.identity_api_version,
        project_id=row.project_id,
        project_name=row.project_name,
        user_domain_name=row.user_domain_name,
        project_domain_name=row.project_domain_name,
        identifier=plaintext["identifier"],
        secret=plaintext["secret"],
    )
    result = openstack_validator.validate(payload)
    row = crud_creds.stamp_validation(db, row, result)
    is_locked, n = _lock_state(db, current_user.userId)
    return _to_response(row, has_credential=True, is_locked=is_locked, active_deployments=n)


@router.delete(
    "/me/openstack-credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_my_credentials(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    crud_locks.acquire_user_xact_lock(db, current_user.userId)
    _assert_unlocked(db, current_user)
    deleted = crud_creds.delete(db, current_user.userId)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No OpenStack credentials configured",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
