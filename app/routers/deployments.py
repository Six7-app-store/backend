import asyncio
import base64
import binascii
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.crud import apps as crud_apps
from app.crud import deployments as crud_deployments
from app.crud import locks as crud_locks
from app.crud import openstack_credentials as crud_openstack_credentials
from app.crud import teams as crud_teams
from app.crud import users as crud_users
from app.database import get_db
from app.models import Deployment, TaskStatus, TaskType, User, UserRole
from app.models import Task as TaskModel  # for ad-hoc state queries
from app.schemas import (
    DeploymentCreate,
    DeploymentDetail,
    DeploymentOutputs,
    DeploymentResourceListResponse,
    DeploymentResourceSchema,
    DeploymentResponse,
    DeploymentTeamMember,
    DeploymentTeamResponse,
    MyAccessResponse,
    TaskSummary,
)
from app.services import deployment_notifier, email_service
from app.services import lifecycle as lifecycle_service
from app.services import task_service as task_service_module
from app.services.deployment_pubsub import pubsub
from app.services.deployment_status import (
    build_resource_detail,
    build_resource_views,
)
from app.services.tf_state_parser import parse_tf_state
from app.utils.capabilities import (
    can_view_deployment_owner,
    ensure_create_deployment,
    ensure_operate_deployment,
    ensure_resend_access,
    ensure_view_app,
    ensure_view_deployment_owner,
    get_my_course_teacher_ids,
)
from app.utils.keycloak_auth import get_current_user_keycloak
from app.utils.permissions import (
    ensure_deployment_access,
    is_deployment_owner_view,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _list_course_scope_deployments(
    db: Session,
    current_user: User,
    skip: int,
    limit: int,
    app_id: UUID | None,
    status_filter: str | None,
    student: UUID | None,
) -> list:
    """Resolve the ``?scope=course`` deployment listing for staff callers.

    Lists every deployment whose owner sits in one of the requestor's
    taught courses. Admins use the same code path for symmetry — usually
    their set is empty. Returns the raw ``Deployment`` rows; the caller
    runs the shared enrichment loop over the result.
    """
    my_courses = get_my_course_teacher_ids(current_user, db)
    if current_user.role == UserRole.ADMIN:
        # Admins always see everything inside the chosen scope.
        # We still need the course-id filter to make ``?scope=course``
        # narrow the listing in some way — otherwise the param is a
        # no-op for admins. The implementation: pull every course
        # the admin is registered as course-teacher for (typically
        # empty), and fall back to the union with the explicit
        # ``student`` filter so the route still does something
        # useful in the common case of an admin requesting a
        # specific student's deployments via the profile page.
        pass
    if not my_courses and current_user.role != UserRole.ADMIN:
        # Teacher with no course-teacher rows — the course scope
        # is empty by definition. Return an empty page rather
        # than the teacher's own owned set, because that would
        # mask the absence of any teacher-course assignment.
        return []

    # Resolve the set of candidate student userIds: every user
    # whose ``courseId`` falls inside ``my_courses``. When the
    # caller also passed ``?student=<id>``, narrow to that single
    # user IFF they actually sit inside one of those courses.
    student_q = db.query(User.userId).filter(
        User.courseId.in_(my_courses)
    )
    if student is not None:
        # ``?student=<id>``: require the student to be inside one
        # of the teacher's courses; otherwise we'd leak that
        # ``student`` exists at all to a teacher who can't see them.
        student_q = student_q.filter(User.userId == student)
    owner_ids = [row[0] for row in student_q.all()]

    if current_user.role == UserRole.ADMIN and not owner_ids:
        # Admin path with empty course set + no student filter →
        # show the admin's own owned set instead of an empty page.
        if student is None:
            return crud_deployments.get_deployments(
                db,
                skip=skip,
                limit=limit,
                user_id=current_user.userId,
                app_id=app_id,
                status=status_filter,
            )
        return []
    if not owner_ids:
        # Teacher in scope mode but their courses are empty, or
        # the named ``student`` isn't in any of their courses —
        # empty page, no leak about that student's existence.
        return []

    # We can't easily widen the existing get_deployments
    # signature to accept an owner_id IN clause without
    # disturbing the other branches, so we build the query
    # inline here. Same soft-delete + app_id + status
    # semantics as the helper.
    q = db.query(Deployment).filter(Deployment.deleted_at.is_(None))
    q = q.filter(Deployment.userId.in_(owner_ids))
    if app_id:
        q = q.filter(Deployment.appId == app_id)
    # Reuse the helper's status-filter implementation by
    # forwarding to ``get_deployments`` with a synthetic
    # ``user_id`` of None and a post-filter — but the helper
    # short-circuits on user_id, so simplest is to inline the
    # ordering/pagination and skip the status filter here. A
    # course-teacher list view rarely needs status filtering
    # in the index; the per-deployment detail page handles
    # status-specific UX.
    q = q.order_by(desc(Deployment.deploymentId))
    return q.offset(skip).limit(limit).all()


# ----------------------------------------------------------------
# GET ALL DEPLOYMENTS
# ----------------------------------------------------------------
@router.get("/", response_model=list[DeploymentResponse])
def list_deployments(
    skip: int = 0,
    limit: int = 100,
    app_id: UUID | None = None,
    status_filter: str | None = None,
    scope: str | None = None,
    student: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """List deployments visible to the caller.

    Visibility rules:
      * **Admins**: their own owned set (default), or — with
        ``?scope=course`` — every deployment whose owner sits in a
        course the admin is a designated teacher of (rare; admins are
        usually not in ``course_teachers`` rows, but the path is
        symmetric with the teacher one for consistency).
      * **Teachers** (default ``scope`` omitted): deployments they
        created (their own owned set). Cross-user listing is
        intentional UX: a teacher opens an individual deployment via
        direct link or via a student's profile page, not from this
        index.
      * **Teachers** (``?scope=course``): every non-deleted deployment
        whose owner sits in a course they teach (course-teacher
        inspect right). Optional ``?student=<userId>`` narrows the
        listing to one course-member's deployments,
        which is what the student-profile page uses to render the
        deployments list of a single student under the teacher's care.
      * **Students** (and any non-staff role): deployments they own
        OR are a member of — either via a team mapping or a direct
        ``UserToDeployment`` row.

    The ``scope`` parameter is accepted only for teachers and admins;
    students passing it get the standard student listing back (the
    parameter is ignored, not rejected, to keep the API forgiving
    against query-string-builder bugs in the frontend).
    """
    is_staff = current_user.role in (UserRole.TEACHER, UserRole.ADMIN)
    use_course_scope = is_staff and scope == "course"

    if use_course_scope:
        deployments = _list_course_scope_deployments(
            db,
            current_user,
            skip=skip,
            limit=limit,
            app_id=app_id,
            status_filter=status_filter,
            student=student,
        )
    elif is_staff:
        deployments = crud_deployments.get_deployments(
            db,
            skip=skip,
            limit=limit,
            user_id=current_user.userId,
            app_id=app_id,
            status=status_filter,
        )
    else:
        deployments = crud_deployments.get_deployments(
            db,
            skip=skip,
            limit=limit,
            member_user_id=current_user.userId,
            app_id=app_id,
            status=status_filter,
        )

    # Enrich with status and created_at from tasks. The summary is
    # bulk-fetched in two queries (latest + first task per deployment via
    # window functions) so the list endpoint stays at a constant query
    # count regardless of page size.
    task_summary = crud_deployments.bulk_get_task_summary(
        db, [d.deploymentId for d in deployments]
    )

    result = []
    for deployment in deployments:
        # Pull the latest-task ``(status, type)`` and the first-task
        # timestamp out of the bulk map. Deployments without any task
        # yet (new row, dispatch in flight) map to ``(None, None, None)``
        # — ``derive_status`` returns None for that, which the schema
        # accepts (``status: str | None``).
        latest_status, latest_type, first_created_at = task_summary.get(
            deployment.deploymentId, (None, None, None)
        )
        status_value = crud_deployments.derive_status(latest_status, latest_type)

        # Parse userInputVar JSON string back to dict if it exists.
        # File uploads are stripped down to metadata here so the list
        # view doesn't ship megabytes of base64 to the browser; the
        # detail endpoint follows the same rule, and the dedicated
        # download route is the only path that returns raw bytes.
        user_input_var_parsed = _parse_and_strip_user_input(deployment.userInputVar)

        result.append(DeploymentResponse(
            deploymentId=deployment.deploymentId,
            name=deployment.name,
            appId=deployment.appId,
            userId=deployment.userId,
            releaseTag=deployment.releaseTag,
            userInputVar=user_input_var_parsed,
            status=status_value,
            created_at=first_created_at,
        ))

    return result


# ----------------------------------------------------------------
# GET DEPLOYMENT BY ID (Full Details)
# ----------------------------------------------------------------
@router.get("/{deployment_id}", response_model=DeploymentDetail)
def get_deployment(
    deployment_id: UUID,
    include_logs: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """
    Get deployment by ID with full details including:
    - User and App relations
    - Teams with members
    - Latest task status
    - Terraform outputs
    - Optionally: full logs (use include_logs=true)
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found"
        )

    # Check access permission
    ensure_deployment_access(deployment, current_user, db)

    # Get latest task
    latest_task = crud_deployments.get_latest_task(db, deployment_id)
    task_summary = None
    logs = None

    if latest_task:
        task_summary = TaskSummary(
            taskId=latest_task.taskId,
            type=latest_task.type,
            status=latest_task.status,
            started_at=latest_task.started_at,
            finished_at=latest_task.finished_at,
            created_at=latest_task.created_at,
            current_phase=getattr(latest_task, "current_phase", None),
            progress_pct=getattr(latest_task, "progress_pct", None),
        )
        if include_logs:
            logs = latest_task.logs

    # Get teams with members. The owner view sees every team and
    # every member. The member view only sees their own team(s) so
    # they can't browse who else has access to the deployment.
    # ``can_view_deployment_owner`` widens "owner view" to course-teachers
    # of the deployment-owner's course, so they get the full roster + logs
    # + outputs alongside owners and admins.
    is_owner_view = can_view_deployment_owner(current_user, deployment, db)
    teams_data = crud_deployments.get_deployment_teams_with_members(db, deployment_id)
    if not is_owner_view:
        teams_data = [
            t for t in teams_data
            if any(str(m["userId"]) == str(current_user.userId) for m in t["members"])
        ]
    teams = [
        DeploymentTeamResponse(
            teamId=team["teamId"],
            name=team["name"],
            members=[
                DeploymentTeamMember(
                    userId=member["userId"],
                    email=member["email"],
                    username=member["username"]
                )
                for member in team["members"]
            ]
        )
        for team in teams_data
    ]

    # Outputs / state / logs are owner-only — members don't get to
    # browse the credentials of teammates or the raw infrastructure
    # state. They have their own resend-access action for their own
    # credentials.
    if is_owner_view:
        outputs_data = crud_deployments.get_deployment_outputs(db, deployment_id)
        outputs = DeploymentOutputs(raw=outputs_data) if outputs_data else None
    else:
        outputs = None
        logs = None

    # Get status and created_at from tasks
    status_value = crud_deployments.get_deployment_status(db, deployment_id)
    created_at = crud_deployments.get_deployment_created_at(db, deployment_id)

    # Parse userInputVar JSON string back to dict if it exists. Same
    # strip-file-bytes treatment as the list endpoint — base64
    # payloads are surfaced via the download route, not the JSON view.
    user_input_var_parsed = _parse_and_strip_user_input(deployment.userInputVar)

    # ``deployment.app`` is the raw ORM relation whose ``image`` column
    # carries bytes. Pydantic's ``DeploymentDetail`` declares
    # ``app.image: Optional[str]`` (the wire shape is a ``data:image/...``
    # URL), so handing it the bytes verbatim throws ``string_unicode``.
    # Run it through ``_serialize_app`` — the same helper the
    # ``/apps``-endpoints already use — to swap the bytes for the
    # data-URL string in place. Apps without an uploaded image are
    # unaffected (``getattr`` returns ``None`` and the helper no-ops).
    from app.routers.apps import _serialize_app  # local: avoid import cycle
    serialised_app = _serialize_app(deployment.app)

    return DeploymentDetail(
        deploymentId=deployment.deploymentId,
        name=deployment.name,
        appId=deployment.appId,
        userId=deployment.userId,
        releaseTag=deployment.releaseTag,
        userInputVar=user_input_var_parsed,
        status=status_value,
        created_at=created_at,
        user=deployment.user,
        app=serialised_app,
        teams=teams,
        latest_task=task_summary,
        outputs=outputs,
        logs=logs,
    )


# ----------------------------------------------------------------
# CREATE DEPLOYMENT
# ----------------------------------------------------------------

# Defense-in-depth limits for inline file uploads. The UX-side warning
# is mirrored on the wizard, but a hand-crafted POST could still try
# to push GBs of payload through ``userInputVar``. We refuse before
# the row hits the DB.
#
# Per-file cap matches the existing app-image cap so users don't have
# to learn a second number; deployment-wide cap is 5× that, leaving
# headroom for (e.g.) one big assignment plus several small starter
# files. Both are enforced post-base64-decode so a malicious base64
# blob of right-shape but wrong-size still fails fast.
_MAX_FILE_BYTES_PER_FILE = 2 * 1024 * 1024
_MAX_FILE_BYTES_PER_DEPLOYMENT = 10 * 1024 * 1024


def _attach_files_to_user_input(
    user_input_var: dict | None,
    files: dict | None,
    variable_definitions: list[dict] | None = None,
) -> dict:
    """Validate and merge wizard-uploaded files into ``userInputVar``.

    The wizard ships files in a parallel ``files`` field instead of
    nesting them straight into ``userInputVar.terraform`` so the
    request payload's shape is obvious to a reader and so we can
    apply size / encoding validation in one place. Result is a fresh
    dict with the files folded into ``terraform[var_name]`` — the
    worker doesn't need to know they originally came from a separate
    field.

    Validation:
      * each top-level key in ``files`` becomes one terraform variable
      * each inner-map entry is one ``DeploymentFileUpload`` record
      * ``content_b64`` decodes cleanly (RFC 4648, padding optional)
      * decoded size matches the declared ``size`` (within rounding —
        client may have set it before encoding so we accept ±1)
      * per-file cap and total deployment cap
      * if ``variable_definitions`` are provided and a file variable
        declares ``fileExtensions``, each uploaded filename's suffix
        (lowercased, after the last dot) must be in the allowed list.
        Defense-in-depth: the wizard's ``accept`` attribute already
        filters in the picker, but a hand-crafted POST could bypass it.

    Raises ``HTTPException(413)`` for size violations and
    ``HTTPException(422)`` for malformed payload — Pydantic already
    rejected the obvious cases (missing fields, wrong types) before
    we get here, so we only catch what gets past it.
    """
    base = dict(user_input_var or {})
    base.setdefault("terraform", {})
    base.setdefault("packer", {})

    if not files:
        return base

    # Build an index var_name → allowed_extensions for the extension
    # check below. Variables without ``fileExtensions`` skip the
    # filter — keeps backward compatibility for any caller that doesn't
    # supply ``variable_definitions``.
    allowed_exts_by_var: dict[str, list[str]] = {}
    scoped_file_vars: set[str] = set()
    if variable_definitions:
        for vdef in variable_definitions:
            exts = vdef.get("fileExtensions")
            if exts:
                allowed_exts_by_var[vdef["name"]] = [e.lower() for e in exts]
            if vdef.get("varScope") in ("team", "user"):
                scoped_file_vars.add(vdef["name"])

    total_bytes = 0
    terraform_block = dict(base.get("terraform") or {})

    for var_name, slot_map in files.items():
        if var_name in terraform_block:
            # Wizard already routed something into this variable — a
            # collision means the frontend filled both the variables
            # picker AND the file uploader for the same name. That's an
            # unrecoverable contract violation; surface it clearly.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "reason": "file_var_collision",
                    "variable": var_name,
                },
            )
        if not isinstance(slot_map, dict) or not slot_map:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "file_var_empty", "variable": var_name},
            )

        encoded_slots: dict[str, dict] = {}
        for slot_key, upload in slot_map.items():
            # ``upload`` arrives here as a Pydantic model instance
            # already (FastAPI deserialised the request body into
            # ``DeploymentCreate``) — pull fields off attributes.
            content_b64 = upload.content_b64

            # Extension-filter check — only when the app author declared
            # an ``@openstack:file:<scope>:<exts>`` filter. We compare
            # the filename suffix (after the last dot, lowercased) to
            # the allowed list. Missing dot or unknown suffix → 422.
            allowed_exts = allowed_exts_by_var.get(var_name)
            if allowed_exts is not None:
                name = upload.name or ""
                dot = name.rfind(".")
                suffix = name[dot + 1 :].lower() if dot >= 0 else ""
                if suffix not in allowed_exts:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "file_extension_rejected",
                            "variable": var_name,
                            "slot": slot_key,
                            "filename": upload.name,
                            "allowed": allowed_exts,
                        },
                    )
            try:
                # ``validate=True`` would reject any non-base64
                # whitespace; the wizard sends compact base64 so this
                # is fine.
                decoded = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "reason": "file_b64_invalid",
                        "variable": var_name,
                        "slot": slot_key,
                        "error": str(e),
                    },
                )

            if abs(len(decoded) - upload.size) > 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "reason": "file_size_mismatch",
                        "variable": var_name,
                        "slot": slot_key,
                        "declared": upload.size,
                        "actual": len(decoded),
                    },
                )

            if len(decoded) > _MAX_FILE_BYTES_PER_FILE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "reason": "file_too_large",
                        "variable": var_name,
                        "slot": slot_key,
                        "limit_bytes": _MAX_FILE_BYTES_PER_FILE,
                        "actual_bytes": len(decoded),
                    },
                )
            total_bytes += len(decoded)
            if total_bytes > _MAX_FILE_BYTES_PER_DEPLOYMENT:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "reason": "deployment_files_too_large",
                        "limit_bytes": _MAX_FILE_BYTES_PER_DEPLOYMENT,
                    },
                )

            encoded_slots[slot_key] = {
                "name": upload.name,
                "content_b64": content_b64,
                "size": upload.size,
                "content_type": upload.content_type or "application/octet-stream",
            }
        # scope=team|user: HCL type is map(map(object({...}))) —
        # outer key is the team/user slot, inner key is the upload slot.
        # scope=all: HCL type is map(object({...})) — flat map.
        if var_name in scoped_file_vars:
            terraform_block[var_name] = {
                slot_key: {"uploaded": file_obj}
                for slot_key, file_obj in encoded_slots.items()
            }
        else:
            terraform_block[var_name] = encoded_slots

    base["terraform"] = terraform_block
    return base


def _validate_scoped_user_input(
    user_input_var: dict | None,
    variable_definitions: list[dict],
    teams_payload: list,
) -> None:
    """Enforce that variables marked with ``varScope = team|user``
    arrive as a map whose keys match the deployment's team / user roster.

    Reasoning: the wizard packs scoped variables as a Map
    (``{slot_key: value, ...}``) and ships them via ``userInputVar``.
    A hand-crafted POST could ship arbitrary keys; we want unknown
    Scope-Targets to fail fast and loud before they hit Terraform,
    where the error would be a confusing "module: invalid for_each
    key" deep in the worker log.

    File variables are NOT skipped here — they share the same scoped
    map shape (``{slot_key: file_obj}``) and a hand-crafted POST could
    just as easily smuggle an unknown team name into a file-scope
    variable. We validate slot identity against the same roster; the
    per-file size / base64 / extension validation stays in
    :func:`_attach_files_to_user_input` because that's the layer that
    actually decodes the bytes.

    Raises ``HTTPException(422)`` with ``reason="unknown_scope_target"``,
    ``reason="scoped_var_not_map"``, or ``reason="required_slot_empty"``
    for shape/identity/completeness problems.
    """
    if not user_input_var:
        return

    # Compose the universe of valid slot keys per scope. ``team``
    # accepts any team name; ``user`` accepts ``TeamName-Username``
    # composites — mirror of ``userSlotKey`` in the wizard.
    team_names: set[str] = set()
    for team in teams_payload or []:
        team_name = getattr(team, "name", None) or (team.get("name") if isinstance(team, dict) else None)
        if not team_name:
            continue
        team_names.add(team_name)
        # ``team.userIds`` contains UUID strings here, not usernames —
        # the deployment endpoint resolves usernames just below us
        # when assembling ``teams_dict``. We accept any non-empty
        # composite key prefix-matching ``f"{team_name}-"`` for
        # user-scoped variables, because the wizard renders one slot
        # per member and labels it with the username (not the UUID).
        # A stricter check would require an extra DB round-trip; the
        # prefix-and-non-empty check is enough to catch typos and
        # cross-team key smuggling.

    # Longest-prefix-match helper for user-scope composite keys:
    # ``TeamName-Username``. A naive ``slot_key.find('-')`` would
    # truncate a team named ``Team-A`` to just ``Team``, so any team
    # name containing a dash would be misclassified as unknown. We
    # iterate the known team names from longest to shortest and pick
    # the first one that either equals ``slot_key`` (empty username,
    # rejected below) or prefixes it as ``f"{team}-"``.
    teams_by_length = sorted(team_names, key=len, reverse=True)

    def _user_slot_team_prefix(slot_key: str) -> str | None:
        for team in teams_by_length:
            if slot_key == team:
                # No trailing ``-Username`` — caller treats this as a
                # missing-user-segment and surfaces ``unknown_scope_target``.
                return team
            if slot_key.startswith(team + "-"):
                return team
        return None

    def _is_empty_slot_value(val) -> bool:
        """Treat None, empty string, empty list, and empty dict as
        "slot not filled". The wizard would otherwise let a required
        team/user-scoped var slip through with one team left blank,
        which Terraform would catch with a much less actionable
        ``Inappropriate value for attribute`` deep in the worker log.
        """
        if val is None:
            return True
        if isinstance(val, str) and val == "":
            return True
        return isinstance(val, (list, dict)) and len(val) == 0

    for source_key in ("terraform", "packer"):
        block = user_input_var.get(source_key)
        if not isinstance(block, dict):
            continue
        for vdef in variable_definitions:
            if vdef.get("source") != source_key:
                continue
            scope = vdef.get("varScope")
            if scope not in ("team", "user"):
                continue
            var_name = vdef["name"]
            value = block.get(var_name)
            is_file = vdef.get("osType") == "file"
            required = bool(vdef.get("required"))
            if value is None:
                # File-scope vars MUST be present — the wizard always
                # ships at least an empty map for them, so a None here
                # is a hand-crafted-POST shape. For non-file required
                # scoped vars, raise on the slot-completeness check
                # below by treating the absent value as an empty map.
                if required:
                    value = {}
                else:
                    continue  # variable left at HCL default — allowed
            if not isinstance(value, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "reason": "scoped_var_not_map",
                        "variable": var_name,
                        "scope": scope,
                    },
                )
            for slot_key in value:
                if scope == "team":
                    if slot_key not in team_names:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={
                                "reason": "unknown_scope_target",
                                "variable": var_name,
                                "scope": scope,
                                "slot": slot_key,
                                "allowed": sorted(team_names),
                            },
                        )
                else:  # user scope
                    # Longest-prefix-match against known team names so
                    # a team named ``Team-A`` parses to prefix
                    # ``Team-A`` and rest ``Username`` instead of
                    # prefix ``Team`` (which wouldn't be a known team).
                    prefix = _user_slot_team_prefix(slot_key)
                    if prefix is None or slot_key == prefix:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={
                                "reason": "unknown_scope_target",
                                "variable": var_name,
                                "scope": scope,
                                "slot": slot_key,
                                "hint": "expected ``TeamName-Username``",
                            },
                        )

            # Required slot-completeness check: for required team /
            # user scoped variables every expected slot key must carry
            # a non-empty value. Without this an empty map (or one
            # team left blank) would silently pass here and only fail
            # downstream with an opaque Terraform error.
            #
            # File vars are skipped from the completeness sweep — the
            # per-file size/decode validation in
            # :func:`_attach_files_to_user_input` raises a more specific
            # error (file_var_empty / file_b64_invalid) for them. We
            # only checked slot identity above; the bytes themselves
            # are validated at that layer.
            if required and not is_file:
                expected_slots: set[str] = set()
                if scope == "team":
                    expected_slots = set(team_names)
                # For ``user`` scope we don't have the per-team member
                # roster here (would need a DB round-trip we already
                # avoid above), so we only enforce that each slot the
                # caller did ship carries a non-empty value. The
                # wizard's frontend check is the primary guard; this
                # is defense-in-depth against hand-crafted POSTs that
                # ship one half-filled team. A POST that omits a team
                # entirely for a required user-scope var is caught by
                # the team-scope branch via team_names because the
                # wizard always emits at least one slot per team.

                missing: list[str] = []
                for slot in expected_slots:
                    if _is_empty_slot_value(value.get(slot)):
                        missing.append(slot)
                # Also flag empty values among slots the caller did
                # provide — covers user-scope and any partial-fill case.
                for slot, val in value.items():
                    if _is_empty_slot_value(val) and slot not in missing:
                        missing.append(slot)
                if missing:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "required_slot_empty",
                            "variable": var_name,
                            "scope": scope,
                            "missing_slots": sorted(missing),
                        },
                    )


def _strip_file_vars_from_user_input(user_input_var: dict | None) -> dict | None:
    """Strip per-file ``content_b64`` payloads from a userInputVar dict.

    Used by the deployment detail responses so the JSON the frontend
    receives only carries metadata (name/size/content_type) — the
    decoded bytes can be many MBs each and shipping them on every
    page render is wasteful. Owners who actually want the file fetch
    it via the dedicated download endpoint.

    Heuristic-based: a variable is a file slot when its value is a
    mapping whose entries each carry a ``content_b64`` field — the
    same shape ``_attach_files_to_user_input`` writes. We match on
    that key because no other user-input kind uses it.
    """
    if not isinstance(user_input_var, dict):
        return user_input_var

    out = {k: v for k, v in user_input_var.items() if k != "terraform"}
    tf_block = user_input_var.get("terraform")
    if not isinstance(tf_block, dict):
        if "terraform" in user_input_var:
            out["terraform"] = tf_block
        return out

    stripped_tf: dict = {}
    for var_name, value in tf_block.items():
        if _looks_like_file_var(value):
            stripped_tf[var_name] = _file_var_metadata_only(value)
        else:
            stripped_tf[var_name] = value
    out["terraform"] = stripped_tf
    return out


def _parse_and_strip_user_input(raw: str | None) -> dict | None:
    """Parse a stored ``userInputVar`` JSON string and strip file bytes.

    Wraps the ``json.loads`` → :func:`_strip_file_vars_from_user_input`
    chain (with a malformed-JSON guard) shared by the list, detail, and
    create responses so all three surface the same file-stripped shape.
    Returns ``None`` for an empty or unparseable value.
    """
    if not raw:
        return None
    try:
        return _strip_file_vars_from_user_input(json.loads(raw))
    except json.JSONDecodeError:
        return None


def _looks_like_file_var(value) -> bool:
    """True if ``value`` matches the file-upload shape produced by
    :func:`_attach_files_to_user_input`: a non-empty mapping whose
    values are objects carrying ``content_b64`` plus the metadata
    triplet. Used at response-shaping time to identify file-typed
    variables without consulting the app's variable schema, AND at
    lifecycle-dispatch time (destroy/pause/resume/redeploy) to drop
    file vars from the worker's var-set so Terraform's schema
    validation doesn't trip on a payload it doesn't need.

    Shape examples it matches (and only these):

    * ``scope=all``   → ``{"all": {name, content_b64, size, content_type}}``
    * ``scope=team``  → ``{"Team-1": {...}, "Team-2": {...}}``
    * ``scope=user``  → ``{"Team-1-luca": {...}, ...}``

    Strict signature: each slot must carry ``content_b64``. Rows
    that survived an earlier response-side-strip-then-persisted
    accident (metadata triplet only, no bytes) are NOT auto-
    detected — clean them up by hand (delete the deployment row +
    its pg-backend tfstate schema). The strictness is intentional:
    a lenient detector would silently swallow legitimate non-file
    map variables that coincidentally share the metadata key names.
    """
    if not isinstance(value, dict) or not value:
        return False
    for slot in value.values():
        if not isinstance(slot, dict):
            return False
        if "content_b64" not in slot:
            return False
    return True


def _file_var_metadata_only(value: dict) -> dict:
    """Return a copy of a file-shape variable with the ``content_b64``
    payload stripped. Metadata fields (name, size, content_type)
    survive so the UI can list "what was uploaded" without shipping
    base64 megabytes on every detail-view render.
    """
    out: dict = {}
    for slot_key, slot in value.items():
        out[slot_key] = {k: v for k, v in slot.items() if k != "content_b64"}
    return out


# ----------------------------------------------------------------
# CREATE DEPLOYMENT
# ----------------------------------------------------------------
@router.post("/", response_model=DeploymentResponse, status_code=status.HTTP_201_CREATED)
def create_deployment(
    deployment: DeploymentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """
    Create a new deployment

    Atomicity: a per-user advisory lock serializes credential mutation
    with deployment dispatch. The deployment row, teams, user mappings,
    and the initial PENDING task row are all inserted in a single
    transaction, so the user can never end up with a deployment row
    that has no matching task. Celery dispatch happens AFTER commit;
    if it fails, the task row is flipped to FAILED so the deployment
    surfaces an honest error instead of hanging in PENDING forever.
    """
    # Role gate first: creating deployments is staff work. A student
    # gets access to an environment a teacher set up, they never create
    # one. Checked before the app lookup so the 403 does not leak
    # whether a given appId exists, and before the advisory lock so a
    # rejected caller never takes it.
    ensure_create_deployment(current_user)

    # Per-user lock — serializes against PUT /me/openstack-credentials
    # and any other concurrent POST /deployments from this user. Held
    # until the next COMMIT/ROLLBACK on this connection.
    crud_locks.acquire_user_xact_lock(db, current_user.userId)

    # Refuse the create if the target app is soft-deleted.
    target_app = crud_apps.get_app(db, deployment.appId)
    if target_app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "app_not_found_or_deleted"},
        )

    # Gate the create on the same visibility rule the list/detail
    # endpoints use. A student cannot deploy a private app they don't
    # own, and a non-owner cannot deploy an app without an approved
    # version. The owner / admin path stays open.
    # ``ensure_view_app`` raises 403 with the structured payload, so
    # the frontend receives the same shape it sees on the detail
    # endpoint when visibility is denied.
    ensure_view_app(current_user, target_app, db=db)

    # Load the app author's variable declarations so we can enforce
    # per-variable contracts (``varScope``, ``fileExtensions``) below.
    # We only do this when the request actually carries variables /
    # files — for a no-input deploy the round-trip into Git would be
    # waste. Same parser as ``GET /apps/{id}/variables`` so client and
    # server agree on which variable is scoped/file/free-text.
    variable_definitions: list[dict] = []
    if deployment.userInputVar or deployment.files:
        from app.routers.apps import load_variable_definitions
        release_tag = deployment.releaseTag or "main"
        try:
            variable_definitions = load_variable_definitions(target_app, release_tag)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
                raise
            # 400 (no git_link) or 500 (git unreachable) — skip validation,
            # proceed without variable definitions.

    # Fold the wizard's parallel ``files`` upload into
    # ``userInputVar.terraform`` before the row gets persisted, so the
    # rest of this handler — and the worker downstream — sees one
    # uniform dict. The helper validates base64 / size / per-file and
    # per-deployment caps; any failure short-circuits with a 4xx and
    # the row never enters the DB. When variable definitions are
    # available, the helper also enforces the app author's declared
    # ``fileExtensions`` filter on each upload name.
    deployment.userInputVar = _attach_files_to_user_input(
        deployment.userInputVar, deployment.files, variable_definitions or None,
    )

    # Enforce ``varScope = team|user`` contracts: each value must be a
    # map whose keys match the deployment's team / user roster. This
    # is defense-in-depth — the wizard already only renders slots the
    # user can fill, but a hand-crafted POST could ship unknown keys.
    if variable_definitions:
        _validate_scoped_user_input(
            deployment.userInputVar,
            variable_definitions,
            deployment.teams or [],
        )

    db_deployment = crud_deployments.create_deployment(
        db, deployment, current_user.userId
    )

    user_ids_in_deployment = set()
    if deployment.teams:
        teams_data = [
            {"name": team.name, "userIds": team.userIds}
            for team in deployment.teams
        ]
        crud_teams.create_teams_for_deployment(
            db=db,
            deployment_id=db_deployment.deploymentId,
            teams_data=teams_data,
        )
        for team in deployment.teams:
            user_ids_in_deployment.update(team.userIds)

    if user_ids_in_deployment:
        crud_deployments.create_user_to_deployments(
            db=db,
            deployment_id=db_deployment.deploymentId,
            user_ids=user_ids_in_deployment,
        )

    # Parse user input variables
    try:
        user_vars = (
            json.loads(db_deployment.userInputVar) if db_deployment.userInputVar else {}
        )
    except Exception:
        user_vars = {}

    # Format teams for Terraform (team_name: [user_emails])
    teams_dict = {}
    if deployment.teams:
        for team in deployment.teams:
            team_users = []
            for user_id in team.userIds:
                user = crud_users.get_user(db, user_id)
                if user:
                    team_users.append({"email": user.email})
            teams_dict[team.name] = team_users

    # Per-user OpenStack credentials are required to deploy. The envelope
    # carries ciphertext only — the worker decrypts in-process. Reading
    # this inside the locked TX guarantees the envelope matches whatever
    # credential row a concurrent PUT might have committed: PUT is
    # serialized behind us by the same advisory lock.
    openstack_envelope = _fetch_dispatch_envelope(
        db, current_user.userId, rollback_on_missing=True
    )

    # Insert PENDING task row in the SAME transaction as the deployment,
    # commit atomically (deployment + teams + user_to_deployments + task),
    # then dispatch to Celery outside the locked TX. On a send failure the
    # task row is flipped to FAILED and we surface 503 — the deployment
    # row stays, but the user sees an obvious failure instead of an
    # eternal PENDING.
    _commit_and_dispatch(
        db,
        deployment_id=db_deployment.deploymentId,
        task_type=TaskType.DEPLOY,
        celery_task_name="tasks.deploy_application",
        celery_args=[
            str(db_deployment.deploymentId),
            str(db_deployment.appId),
            db_deployment.app.git_link,
            db_deployment.releaseTag,
            user_vars,
            teams_dict,
            openstack_envelope,
        ],
        dispatch_error_label="Could not dispatch deployment task — please retry",
        rollback_on_conflict=True,
    )

    db.refresh(db_deployment)

    status_value = crud_deployments.get_deployment_status(db, db_deployment.deploymentId)
    created_at = crud_deployments.get_deployment_created_at(db, db_deployment.deploymentId)

    # Same file-strip rule as the list/detail endpoints: the POST
    # response shape mirrors the read shape so the frontend can reuse
    # the same parsing code-path.
    user_input_var_parsed = _parse_and_strip_user_input(db_deployment.userInputVar)

    return DeploymentResponse(
        deploymentId=db_deployment.deploymentId,
        name=db_deployment.name,
        appId=db_deployment.appId,
        userId=db_deployment.userId,
        releaseTag=db_deployment.releaseTag,
        userInputVar=user_input_var_parsed,
        status=status_value,
        created_at=created_at,
    )


# ----------------------------------------------------------------
# DELETE DEPLOYMENT
# ----------------------------------------------------------------
#
# One endpoint, two outcomes — the backend picks the right one from
# the deployment's status:
#
#   * ``success`` / ``failed`` / ``paused`` → dispatch a Destroy task
#     (terraform destroy + auto-soft-delete on success). ``paused`` is
#     in the destroy set because SHUTOFF instances + volumes/networks
#     are still OpenStack resources that need to be reclaimed.
#     Returns 202 + task_id; the frontend keeps the live stream open
#     and routes back to the list when the task finishes.
#   * ``cancelled``             → soft-delete immediately. Returns 204.
#   * any other status (running / pending / destroying / pausing / resuming)
#                               → 409, the user has to wait.
#
# Frontend doesn't have to know the difference — it just calls DELETE
# and switches into the live-stream view when the response is 202.
@router.delete("/{deployment_id}")
def delete_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Unified delete — destroys OpenStack resources first if needed.

    Restricted to the owner-view (creator, teacher, admin). Members
    can read-access the deployment but never tear it down.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    # Destructive operation — uses the operate gate, which is
    # owner-or-admin only. Course-teachers explicitly do NOT get
    # delete/destroy rights on deployments in their courses; they only
    # get inspect (logs, infra).
    ensure_operate_deployment(current_user, deployment, db)

    # Per-deployment advisory lock — serialises against any concurrent
    # POST /pause, /resume or DELETE on the same deployment so the
    # ``current_status`` read below and the eventual
    # ``prepare_task_in_tx`` insert see a consistent picture. Without
    # this, two concurrent destroys could both pass the in-flight check
    # and one would crash on the partial unique index.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)

    current_status = crud_deployments.get_deployment_status(db, deployment_id)

    # Active task in flight — neither path is safe.
    if current_status in lifecycle_service.IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete a deployment in status '{current_status}'. "
                "Wait for the active task to finish."
            ),
        )

    # Resources may exist — destroy them first; the listener will
    # auto-soft-delete the row when the destroy task succeeds. ``paused``
    # also lands here: SHUTOFF instances + volumes/networks are still
    # OpenStack resources that need to be torn down before the row can
    # be hidden. ``pause_failed`` / ``resume_failed`` likewise still
    # have running OpenStack resources behind them — the deployment
    # itself didn't break, only the lifecycle pass.
    if current_status in ("success", "failed", "paused", "pause_failed", "resume_failed"):
        return _dispatch_destroy(db, deployment, current_user)

    # No resources to clean up (cancelled, or anything else terminal):
    # straight soft-delete.
    success = crud_deployments.soft_delete_deployment(db, deployment_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _fetch_dispatch_envelope(db: Session, user_id, *, rollback_on_missing: bool):
    """Fetch the encrypted OpenStack credential envelope for a dispatch.

    Raises ``HTTPException(412, openstack_credentials_missing)`` when the
    user has no credentials. ``rollback_on_missing`` is used by the
    create path, which holds an uncommitted deployment insert that must
    be rolled back before the 412 escapes; lifecycle callers have no such
    pending insert and pass ``False``.
    """
    try:
        return crud_openstack_credentials.get_dispatch_envelope(db, user_id)
    except crud_openstack_credentials.NoCredentialError:
        if rollback_on_missing:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"reason": "openstack_credentials_missing"},
        )


def _commit_and_dispatch(
    db: Session,
    *,
    deployment_id,
    task_type: TaskType,
    celery_task_name: str,
    celery_args: list,
    dispatch_error_label: str,
    rollback_on_conflict: bool,
):
    """Insert the PENDING task in-TX, commit, then dispatch to Celery.

    Shared tail of the create and lifecycle dispatch paths:

    * ``prepare_task_in_tx`` → ``HTTPException(409)`` on an active task
      (the create path additionally rolls back its pending insert);
    * ``commit`` + ``refresh`` the task row;
    * ``dispatch_to_celery`` → ``HTTPException(503, dispatch_error_label)``
      on a Celery send failure (the task row is flipped to FAILED inside
      ``dispatch_to_celery`` in a fresh TX).

    Returns the committed, dispatched ``Task``.
    """
    try:
        task = task_service_module.prepare_task_in_tx(
            db,
            deployment_id=deployment_id,
            task_type=task_type,
        )
    except task_service_module.ActiveTaskExistsError:
        if rollback_on_conflict:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deployment already has an active task",
        )

    db.commit()
    db.refresh(task)

    try:
        task, _celery_id = task_service_module.dispatch_to_celery(
            db,
            task=task,
            celery_task_name=celery_task_name,
            celery_args=celery_args,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=dispatch_error_label,
        )
    return task


def _dispatch_destroy(db: Session, deployment, current_user: User):
    """Enqueue the destroy worker task for a deployment.

    Thin wrapper around :func:`_dispatch_lifecycle_task` so DELETE can
    keep its existing two-call sites (success-path / failed-path) clear.
    """
    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.DESTROY,
        celery_task_name="tasks.destroy_deployment",
        response_status="destroying",
    )


def _dispatch_lifecycle_task(
    db: Session,
    deployment,
    current_user: User,
    task_type: TaskType,
    celery_task_name: str,
    response_status: str,
    extra_args: list | None = None,
):
    """Enqueue any post-deploy lifecycle worker task for a deployment.

    Used by destroy, pause, resume, and per-VM redeploy — all four
    follow the same pattern: load user inputs, gather team membership,
    fetch the encrypted OpenStack envelope, atomically insert a
    PENDING task row, commit, then ``send_task`` outside the locked
    TX. On a Celery send failure the task row flips to FAILED in a
    fresh TX (handled inside ``dispatch_to_celery``) and we surface a
    503 so the user sees an obvious failure instead of a permanent
    in-flight status.

    Args:
        task_type:           the ``TaskType`` enum value that drives both
                             the task row's ``type`` column and the
                             status the partial-unique index prevents
                             from coexisting.
        celery_task_name:    name registered on the worker side (e.g.
                             ``tasks.pause_deployment``).
        response_status:     synthetic deployment status returned to the
                             frontend in the 202 body — frontend uses
                             this to immediately switch the UI into the
                             live-stream view without re-fetching.
        extra_args:          additional positional args appended to the
                             Celery payload after the standard seven.
                             Used by ``tasks.redeploy_resource`` to pass
                             the targeted resource address.
    """
    try:
        user_vars = json.loads(deployment.userInputVar) if deployment.userInputVar else {}
    except Exception:
        user_vars = {}

    # Belt + braces: for lifecycle tasks that do NOT recreate the VM
    # (destroy, pause, resume), strip any ``@openstack:file:*`` payloads
    # from the user-vars BEFORE they reach the worker. Files are only
    # consumed at apply-time by cloud-init's write_files; everything
    # else just hands the same var-set to Terraform which then
    # validates the entire variable surface against the HCL schema.
    # A row whose ``content_b64`` was stripped by a response-side
    # ``_strip_file_vars_from_user_input`` pass (e.g. after a manual
    # DB edit, an in-place row shrink, or any future code path that
    # rewrites the persisted JSON) would otherwise crash destroy with
    # ``element "all": attributes "content_b64", "content_type",
    # "name", and "size" are required`` because the surviving slot
    # violates the variable's object type. Dropping the var
    # altogether lets Terraform fall back on the HCL default.
    #
    # REDEPLOY is the special case: ``terraform apply -replace`` destroys
    # and recreates the VM, so cloud-init runs fresh and MUST receive the
    # original ``write_files`` payload — otherwise the replaced VM ends
    # up empty even though the user/group/password config is preserved.
    # We therefore keep the file vars on REDEPLOY and let the persisted
    # base64 payload flow through to the worker, mirroring the initial
    # deploy path.
    #
    # DEPLOY/UPDATE legitimately need the file bytes too — they don't
    # enter the worker via this helper.
    if task_type in (TaskType.DESTROY, TaskType.PAUSE, TaskType.RESUME):
        terraform_block = user_vars.get("terraform")
        if isinstance(terraform_block, dict):
            user_vars = {
                **user_vars,
                "terraform": {
                    k: v
                    for k, v in terraform_block.items()
                    if not _looks_like_file_var(v)
                },
            }

    teams_dict: dict = {}
    if deployment.teams:
        # Persisted Team rows expose membership via the ``user_to_teams``
        # association, not a flat ``userIds`` field — that lives on the
        # request-side Pydantic schema in the create endpoint, not on
        # the ORM. ``get_team_members`` does the join for us.
        for team in deployment.teams:
            members = crud_deployments.get_team_members(db, team.teamId)
            teams_dict[team.name] = [{"email": m.email} for m in members]

    openstack_envelope = _fetch_dispatch_envelope(
        db, current_user.userId, rollback_on_missing=False
    )

    task = _commit_and_dispatch(
        db,
        deployment_id=deployment.deploymentId,
        task_type=task_type,
        celery_task_name=celery_task_name,
        celery_args=[
            str(deployment.deploymentId),
            str(deployment.appId),
            deployment.app.git_link,
            deployment.releaseTag,
            user_vars,
            teams_dict,
            openstack_envelope,
            *(extra_args or []),
        ],
        dispatch_error_label=f"Could not dispatch {task_type.value} task — please retry",
        rollback_on_conflict=False,
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"task_id": str(task.taskId), "status": response_status},
    )


# ----------------------------------------------------------------
# INFRASTRUCTURE RESOURCES (per-deployment status + per-VM redeploy)
# ----------------------------------------------------------------
#
# Three sibling endpoints power the Infrastructure tab on the
# deployment detail page:
#
#   * GET /{deployment_id}/resources?refresh=…
#       Stage-1 listing — parses the cached TF state and (default)
#       overlays live OpenStack lifecycle/hardware/addresses per VM.
#       Returns a flat list spanning compute, network, subnet, SG,
#       FIP, and port categories.
#
#   * GET /{deployment_id}/resources/{address}
#       Stage-2 detail — same shape, plus ports/SG-summary/volumes/
#       metadata for ONE compute instance, identified by its TF state
#       address (e.g. ``openstack_compute_instance_v2.team_ide["Team-A"]``).
#       Frontend loads this lazily when the user opens a card's drawer.
#
#   * POST /{deployment_id}/resources/{address}/redeploy
#       Per-VM redeploy — issues ``terraform apply -replace=<addr>
#       -target=<addr>`` in a dedicated Celery task. Strictly
#       address-whitelisted against the cached TF state and the
#       compute-instance category, so a hand-crafted POST can't smuggle
#       a network-resource target (which would tear down all team VMs).
#
# All three are owner-only — the data exposed (live OpenStack status,
# the ability to bounce a VM) is not something a teammate should be
# able to access through the deployment detail page.


# We accept the same Terraform address vocabulary the user would type
# on ``terraform apply -target=``: ``type.name`` with optional
# ``[<int>]`` or ``["<string>"]`` suffix. Multiple address segments
# (modules, nested resources) aren't supported by the current apps,
# so we keep the regex strict to make smuggling impossible. The
# resource-existence whitelist below is the real defense; the regex
# is just a fast no-op rejection for obviously bad inputs (e.g.
# pipes, semicolons, spaces).
_TF_ADDRESS_RE = re.compile(
    r"""^
    [A-Za-z_][A-Za-z0-9_]*       # provider type (e.g. openstack_compute_instance_v2)
    \.[A-Za-z_][A-Za-z0-9_-]*    # resource name (e.g. team_ide)
    (?:
        \[(?:\d+|"[^"\\]+")\]    # optional index ([0] or ["Team-A"])
    )?
    $""",
    re.VERBOSE,
)


def _latest_tf_state_for(deployment_id: UUID, db: Session) -> str | None:
    """Return the JSON blob of the most recent task that captured a
    Terraform state for this deployment, or None when no apply ever
    succeeded yet.

    Note: we deliberately do NOT filter by ``task.type`` — the worker
    captures state on deploy / destroy / pause / resume / redeploy
    alike, and any of those produce a valid snapshot. The "most
    recent" task wins, mirroring the existing ``get_deployment_outputs``
    semantics in ``crud/deployments.py``.
    """
    task = (
        db.query(TaskModel)
        .filter(TaskModel.deploymentId == deployment_id)
        .filter(TaskModel.tf_state.isnot(None))
        .order_by(desc(TaskModel.created_at))
        .first()
    )
    return task.tf_state if task else None


@router.get(
    "/{deployment_id}/resources",
    response_model=DeploymentResourceListResponse,
)
def list_deployment_resources(
    deployment_id: UUID,
    refresh: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Stage-1 resource listing for the Infrastructure tab.

    Owner-only. ``refresh=false`` skips the live OpenStack join — use
    that when polling rapidly to avoid hammering Keystone, or when
    OpenStack is known unavailable and the cached state is good enough.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    # Inspect-only view — owner, admin, or a course-teacher of the
    # deployment-owner's course. Course-teachers explicitly do NOT have
    # operate rights; this endpoint is read-only.
    ensure_view_deployment_owner(current_user, deployment, db)

    state_json = _latest_tf_state_for(deployment_id, db)
    views = build_resource_views(
        db=db,
        user=current_user,
        tf_state_json=state_json,
        refresh=refresh,
    )
    # Convert dataclasses → Pydantic models via dict-roundtrip. The
    # fields line up 1:1 by name, so ``model_validate`` works directly
    # on the dataclass dict.
    payload = [
        DeploymentResourceSchema.model_validate(_view_asdict(v))
        for v in views
    ]
    return DeploymentResourceListResponse(resources=payload, live=refresh)


@router.get(
    "/{deployment_id}/resources/{address:path}",
    response_model=DeploymentResourceSchema,
)
def get_deployment_resource_detail(
    deployment_id: UUID,
    address: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Stage-2 detail for one compute instance.

    Uses a ``path``-converter on the address so the for_each-key
    quoting (``team_ide["Team-A"]``) survives URL routing without
    aggressive encoding gymnastics on the client side. The address
    MUST exist in the cached state and MUST be a compute instance;
    other categories get 422.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    # Inspect-only view — course-teachers may read the per-resource
    # detail; the per-VM redeploy below is operate-gated.
    ensure_view_deployment_owner(current_user, deployment, db)

    if not _TF_ADDRESS_RE.match(address):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_resource_address"},
        )

    state_json = _latest_tf_state_for(deployment_id, db)
    view = build_resource_detail(
        db=db,
        user=current_user,
        tf_state_json=state_json,
        address=address,
    )
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "resource_not_in_state", "address": address},
        )
    return DeploymentResourceSchema.model_validate(_view_asdict(view))


@router.post(
    "/{deployment_id}/resources/{address:path}/redeploy",
    status_code=status.HTTP_202_ACCEPTED,
)
def redeploy_deployment_resource(
    deployment_id: UUID,
    address: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Replace one compute instance via ``terraform apply -replace=…``.

    Address-whitelisted: we re-parse the cached TF state and only
    accept addresses that resolve to a compute instance. Anything else
    fails with 422 — the redeploy of a network resource would tear
    down all the team VMs, which is not what a one-VM "fix it" action
    should do.

    Concurrency: same per-user advisory lock as create/destroy/pause
    so a parallel redeploy can't race a destroy. The lock is held
    only for the row insert; Celery dispatch happens after commit.
    """
    crud_locks.acquire_user_xact_lock(db, current_user.userId)

    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    # Per-VM redeploy is a mutating operation — operate gate
    # (owner-or-admin). Course-teachers may inspect the resource via
    # the GET endpoints above but not bounce it.
    ensure_operate_deployment(current_user, deployment, db)

    if not _TF_ADDRESS_RE.match(address):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_resource_address"},
        )

    # Whitelist check: the address MUST point to a compute instance in
    # the current state. We re-parse here instead of trusting the
    # output of the list endpoint — a hand-crafted POST would skip
    # the list call entirely.
    state_json = _latest_tf_state_for(deployment_id, db)
    parsed = parse_tf_state(state_json)
    match = next((r for r in parsed if r.address == address), None)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "resource_not_in_state", "address": address},
        )
    if match.category != "instance":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "non_redeployable_resource_type",
                "address": address,
                "category": match.category,
            },
        )

    return _dispatch_lifecycle_task(
        db=db,
        deployment=deployment,
        current_user=current_user,
        task_type=TaskType.REDEPLOY,
        celery_task_name="tasks.redeploy_resource",
        response_status="redeploying",
        extra_args=[address],
    )


def _view_asdict(view) -> dict:
    """Recursive dataclass → dict converter, used to bridge
    ``deployment_status.DeploymentResourceView`` to Pydantic.

    ``dataclasses.asdict`` already recurses into nested dataclasses,
    so we just delegate. Kept as a thin wrapper so the call sites
    above read symmetrically and we can swap in custom handling later
    if needed (e.g. enum serialisation).
    """
    return asdict(view)



#
# Halts the OpenStack compute instances of a deployment without
# tearing them down. The worker task pulls the terraform state, lists
# every ``openstack_compute_instance_v2`` resource, and runs
# ``openstack server stop`` against each. Volumes and networks stay,
# so RESUME restores the same instances byte-for-byte.
#
# Allowed only on ``status='success'`` — the lifecycle service is the
# single source of truth, the partial-unique index on active tasks is
# the DB-level backstop.
@router.post("/{deployment_id}/pause", status_code=status.HTTP_202_ACCEPTED)
def pause_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Pause a running deployment by stopping its compute instances.

    Owner-only — same gate as Destroy, because pausing a teammate's
    deployment is in practice a denial-of-service against the team.

    Returns ``202 + {task_id, status: "pausing"}`` on dispatch. The
    frontend reads ``status`` to switch to the live SSE view; the
    deployment's effective status is recomputed from the new task
    row by ``crud_deployments.get_deployment_status``.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    ensure_operate_deployment(current_user, deployment, db)
    # Hold the per-deployment advisory lock across the status check
    # AND the task insert so a parallel POST /pause can't sneak past
    # ``ensure_action_allowed`` between our read and the
    # ``prepare_task_in_tx`` flush.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    lifecycle_service.ensure_action_allowed(
        db, deployment, lifecycle_service.DeploymentAction.PAUSE,
    )

    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.PAUSE,
        celery_task_name="tasks.pause_deployment",
        response_status="pausing",
    )


# ----------------------------------------------------------------
# RESUME DEPLOYMENT
# ----------------------------------------------------------------
#
# Reverses Pause. Allowed only on ``status='paused'`` — a deployment
# that was never paused has nothing to resume, so the lifecycle
# matrix gates this strictly. Returns 202 with the same shape as
# Pause/Destroy so the frontend handles all three the same way.
@router.post("/{deployment_id}/resume", status_code=status.HTTP_202_ACCEPTED)
def resume_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Resume a paused deployment by starting its compute instances."""
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    ensure_operate_deployment(current_user, deployment, db)
    # Per-deployment advisory lock — same justification as in
    # ``pause_deployment`` above: keep the status check and the task
    # insert atomic against concurrent /resume / /pause / DELETE
    # requests on this deployment.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    lifecycle_service.ensure_action_allowed(
        db, deployment, lifecycle_service.DeploymentAction.RESUME,
    )

    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.RESUME,
        celery_task_name="tasks.resume_deployment",
        response_status="resuming",
    )


# ----------------------------------------------------------------
# DOWNLOAD UPLOADED FILE
# ----------------------------------------------------------------
#
# Lets the deployment owner re-fetch a file they uploaded at deploy
# time. The list / detail endpoints strip the base64 payload so they
# don't ship megabytes per page render; this endpoint is the only
# path that returns the actual bytes. Owner-only — members can see
# that a file was uploaded (metadata survives the strip), but the
# bytes themselves stay restricted to whoever created the deployment.
@router.get(
    "/{deployment_id}/files/{var_name}/{slot_key}",
    response_class=Response,
)
def download_deployment_file(
    deployment_id: UUID,
    var_name: str,
    slot_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Stream the raw bytes of one wizard-uploaded file back to the owner.

    Path components mirror how the upload was indexed:
      * ``var_name`` — the ``@openstack:file:*`` variable name
      * ``slot_key`` — the inner-map key (``"all"`` for scope=all,
        team name for scope=team, ``Team-User`` composite for scope=user)

    Returns 404 if any layer of the lookup misses; the frontend can
    therefore probe a slot's existence via this endpoint without
    needing a separate metadata response.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    # Inspect-only view, gated through capabilities so course-teachers
    # can download the same wizard-uploaded files they can already see
    # referenced in the inspect view (logs / detail). Owners + admins
    # keep their access. The list/detail strip-pass already hid the
    # base64 payload from plain members, so this endpoint stays
    # restricted to the owner-view set.
    ensure_view_deployment_owner(current_user, deployment, db)

    if not deployment.userInputVar:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No files")
    try:
        user_input = json.loads(deployment.userInputVar)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No files")

    tf_block = user_input.get("terraform") if isinstance(user_input, dict) else None
    var_value = (tf_block or {}).get(var_name)
    if not _looks_like_file_var(var_value):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No uploaded file under variable '{var_name}'",
        )
    # ``var_value`` is ``{slot_key: {name, content_b64, size, content_type}}``;
    # the slot-level entry IS the file metadata, no extra wrapper.
    entry = var_value.get(slot_key)
    if not isinstance(entry, dict) or "content_b64" not in entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No file in slot '{slot_key}'",
        )

    try:
        payload = base64.b64decode(entry["content_b64"], validate=True)
    except (binascii.Error, ValueError):
        # Persisted bytes are corrupt — surface as 500 because there's
        # nothing the caller can do; this is a server-side data bug.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored file payload is not valid base64",
        )

    filename = str(entry.get("name") or f"{var_name}-{slot_key}")
    content_type = str(entry.get("content_type") or "application/octet-stream")
    return Response(
        content=payload,
        media_type=content_type,
        headers={
            # ``filename*`` is the RFC 5987 form for non-ASCII names;
            # we always emit it alongside the legacy ``filename`` so
            # clients without UTF-8 support still see something.
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{filename}"
            ),
            "Content-Length": str(len(payload)),
        },
    )


# ----------------------------------------------------------------
# GET OWN ACCESS CREDENTIALS (member self-service)
# ----------------------------------------------------------------
@router.get("/{deployment_id}/my-access", response_model=MyAccessResponse)
def get_my_access(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Return the calling member's OWN access credentials for a deployment.

    The full terraform ``outputs`` are owner-view-only (they carry every
    teammate's credentials in one object). This endpoint is the member
    counterpart: a team member — typically a student — retrieves only
    THEIR OWN account, extracted server-side from the latest successful
    deploy. Teammates' credentials are never included in the response.

    Authorisation reuses :func:`ensure_resend_access` with the target set
    to the caller themself, which collapses to the member-view gate
    (owner, staff, team-member, or direct mapping). A caller with no
    access to the deployment gets 403; the resend endpoint uses the same
    gate for the self-resend button, so the two stay consistent.

    Returns 200 with empty maps when there's no successful deploy yet or
    the app issued no per-user credential for this user — the UI renders
    a "no credentials yet" state rather than treating it as an error.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    # Self-target → member-view gate. Non-members get 403 here.
    ensure_resend_access(current_user, deployment, current_user.userId, db)

    access = deployment_notifier.get_user_access(
        db, deployment_id, current_user.userId
    )
    if access is None:
        # No successful deploy yet, user not in any team, or no per-user
        # credential in the outputs — surface an empty (but valid) payload.
        return MyAccessResponse()
    return MyAccessResponse(
        user_accounts=access.get("user_accounts", {}),
        team_vms=access.get("team_vms", {}),
    )


# ----------------------------------------------------------------
# RESEND ACCESS MAIL FOR ONE TEAM MEMBER
# ----------------------------------------------------------------
@router.post(
    "/{deployment_id}/teams/{team_id}/users/{user_id}/resend-access",
    status_code=status.HTTP_202_ACCEPTED,
)
def resend_access_credentials(
    deployment_id: UUID,
    team_id: UUID,
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Re-send the per-user access mail for one team member.

    Reuses the original notify pipeline — same template, same
    credential extraction from the latest successful DEPLOY task's
    ``terraform_outputs``. Useful when the user lost their first mail
    or the deploy ran before the user's email got fixed.

    Access control: caller must have access to the deployment (owner
    or teacher/admin). The endpoint is intentionally idempotent —
    each call sends one mail to the targeted user. There's no rate
    limit at the API level; SMTP and Gmail's per-account quota are
    the natural backstops.

    Mapping ResendError to HTTP:
      * ``deployment_not_found`` → 404
      * ``team_not_in_deployment`` / ``user_not_in_team`` → 404
      * ``no_successful_deploy`` → 409 (nothing to resend yet)
      * ``no_credentials_for_user`` → 409 (template didn't issue
        per-user creds, or matcher missed despite the fuzzy logic)
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    ensure_deployment_access(deployment, current_user, db)

    # Members may only re-send their own access mail. Owner-view
    # callers (creator, teacher, admin) can resend for anyone in
    # any team. Without this check a student in team A could trigger
    # a mail to anyone else's address, which is both privacy-leaky
    # and a tiny SMTP-amplification vector.
    #
    # This 403 is decided BEFORE the SMTP-disabled 503 below: a
    # not-authorised caller must not learn the SMTP-state of the
    # platform — that would be a (small) information disclosure.
    if not is_deployment_owner_view(deployment, current_user) and str(user_id) != str(current_user.userId):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Members may only resend their own access mail",
        )

    # SMTP kill-switch check: refuse cleanly with 503 BEFORE running the
    # notifier so the response carries the correct semantic ("we chose
    # not to send" — operator configuration) rather than the existing
    # 502 ("we tried and SMTP rejected" — infrastructure failure).
    # Frontend distinguishes the two reasons in the toast text so the
    # user understands whether to ask an admin to enable mail or to
    # simply retry. The 503 + Retry-After header signals "service
    # temporarily unavailable; come back later" semantics.
    #
    # Order vs. 409 in-flight: a 503 here is non-recoverable until an
    # operator flips the env-flag, whereas 409 is a transient state
    # the caller can simply wait out. Returning the 503 first matches
    # the "service-level concern beats per-request concern" pattern
    # used by every other 5xx vs 4xx ordering in this router.
    if not email_service.is_smtp_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "smtp_disabled"},
            headers={"Retry-After": "3600"},
        )

    # Refuse while another lifecycle action is in flight. Resending
    # the access mail relies on the latest successful DEPLOY task's
    # ``terraform_outputs``; during pending/running/destroying/
    # pausing/resuming the deployment is in transition and the
    # credentials might no longer be reachable on the VM (paused →
    # SHUTOFF, destroying → tearing down). Returning 409 here keeps
    # the user's mental model consistent with the rest of the
    # lifecycle gates.
    #
    # Per-deployment advisory lock is acquired BEFORE the status
    # read so a concurrent /pause / /resume / DELETE can't slip a
    # transition past us between the check and the mail send. The
    # lock is the same one those endpoints take, so the four
    # mutators serialise against each other on the same deployment.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    current_status = crud_deployments.get_deployment_status(db, deployment_id)
    if current_status in lifecycle_service.IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot resend access mail while deployment is '{current_status}'. "
                "Wait for the active task to finish."
            ),
        )

    try:
        sent = deployment_notifier.resend_user_access(
            db, deployment_id, team_id, user_id,
        )
    except deployment_notifier.ResendError as e:
        reason = str(e)
        # 404 for "this user/team isn't in this deployment", 409 for
        # "the deployment hasn't reached the state where it could
        # have emitted credentials yet".
        if reason in ("deployment_not_found", "team_not_in_deployment", "user_not_in_team"):
            http_status = status.HTTP_404_NOT_FOUND
        else:
            http_status = status.HTTP_409_CONFLICT
        raise HTTPException(status_code=http_status, detail={"reason": reason})

    if not sent:
        # Template render + payload were fine, only SMTP rejected.
        # 502 makes the "upstream service failed" semantics clear so
        # the frontend can surface a retry rather than a 4xx that
        # implies the request itself was wrong.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "smtp_send_failed"},
        )
    return {"status": "sent"}


# ----------------------------------------------------------------
# LIVE STREAM — Server-Sent Events for progress + log tail
# ----------------------------------------------------------------
#
# Several kinds of events flow through the stream:
#
# * ``event: snapshot`` — fired once at connect with the latest task's
#   current_phase / progress_pct / status. Lets a freshly-loaded page
#   render the bar at the right position before the worker emits its
#   next progress update.
# * ``event: progress`` — every ``task-progress`` from the worker. The
#   payload includes ``phase``, ``phase_index``, ``total_phases``,
#   ``progress_pct``, ``message``.
# * ``event: log`` — every ``task-log`` from the worker. The payload
#   is the LogEntry dict (timestamp, level, category, message, plus
#   tool/streaming flags for streaming subprocess lines).
# * ``event: overflow`` — emitted by the in-process pubsub when a
#   slow consumer overran its bounded queue.
# * comment lines starting with ``:`` are SSE keepalive pings.
#
# The stream stays open until the deployment reaches a terminal state
# (success/failed/cancelled), the client disconnects, or the backend
# shuts down. There's no client-driven close — EventSource handles
# reconnect automatically.
@router.get("/{deployment_id}/stream")
async def stream_deployment_events(
    deployment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Live progress + log stream for one deployment as Server-Sent Events.

    The connection is authenticated with the same Keycloak dependency
    used elsewhere; the standard auth middleware also vets the token
    before this handler runs. After auth we attach to the in-process
    pubsub for this deployment and forward every event to the client.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    # Inspect-only view via capabilities. The live stream surfaces
    # task-log lines (raw worker stdout incl. terraform output, packer
    # build chatter, etc.); course-teachers of the deployment-owner's
    # course are in the inspect set, owners and admins keep their access,
    # and plain members still see metadata only.
    ensure_view_deployment_owner(current_user, deployment, db)

    # Snapshot the latest task once before subscribing so the client
    # gets a meaningful initial state. Reading happens before the
    # generator yields its first chunk to avoid the "subscribed but
    # nothing buffered yet" gap.
    latest_task = crud_deployments.get_latest_task(db, deployment_id)
    snapshot_payload = {
        "task_id": str(latest_task.taskId) if latest_task else None,
        "status": latest_task.status.value if latest_task else None,
        "current_phase": getattr(latest_task, "current_phase", None),
        "progress_pct": getattr(latest_task, "progress_pct", None),
        "type": latest_task.type.value if latest_task else None,
    }
    initial_status = latest_task.status if latest_task else None

    deployment_id_str = str(deployment_id)

    async def event_stream() -> AsyncIterator[bytes]:
        queue = pubsub.subscribe(deployment_id_str)
        try:
            yield _sse_frame("snapshot", snapshot_payload)

            # Backfill what's been happening lately. The pubsub keeps a
            # bounded ring buffer of recent events per deployment so a
            # client connecting mid-stream sees the last few minutes of
            # progress / log output instead of an empty tail until the
            # next worker line lands. Replays the buffer in order so
            # ``streamCurrentPhaseIndex``/``streamProgress`` end up at
            # their latest values before the live loop starts.
            for past_event in pubsub.recent(deployment_id_str):
                event_name = _event_name_for(past_event.get("type"))
                yield _sse_frame(event_name, past_event)

            # If the task is already in a terminal state we still yield
            # the snapshot but close the stream right away — no live
            # events will ever arrive for this deployment.
            if initial_status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return

            # Heartbeat / event-pump loop. Wait up to 15s for an event;
            # if nothing arrives, send a ``: keepalive`` comment so
            # proxies and the EventSource client don't time out.
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue

                event_name = _event_name_for(event.get("type"))
                yield _sse_frame(event_name, event)

                # Stop streaming once the parent task reaches a
                # terminal state. The lifecycle events (succeeded /
                # failed / revoked) flow through the same pubsub key,
                # so we look for them right here. Without this break
                # the connection would dangle until the client closes
                # it.
                if event.get("type") in ("task-succeeded", "task-failed", "task-revoked"):
                    return
        except asyncio.CancelledError:
            # FastAPI cancels the generator on client disconnect.
            raise
        finally:
            pubsub.unsubscribe(deployment_id_str, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
            "Connection": "keep-alive",
        },
    )


_EVENT_NAME_MAP: dict[str, str] = {
    "task-progress": "progress",
    "task-log": "log",
    "task-overflow": "overflow",
    "task-started": "started",
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-revoked": "revoked",
}


def _event_name_for(celery_event_type: str | None) -> str:
    """Map Celery event type names onto short SSE event names.

    Frontend code attaches listeners by these short names rather than
    the verbose celery-internal ones; ``_EVENT_NAME_MAP`` is the
    single source of truth on both sides of the wire.
    """
    return _EVENT_NAME_MAP.get(celery_event_type or "", "message")


def _sse_frame(event_name: str, payload: dict) -> bytes:
    """Serialise one SSE frame.

    SSE format:

    ```
    event: <name>\\n
    data: <json>\\n
    \\n
    ```

    Embedded newlines in the JSON would split the frame into multiple
    ``data:`` lines per the SSE spec; we use ``json.dumps`` defaults
    which keep everything on one line.
    """
    body = json.dumps(payload, default=str)
    return f"event: {event_name}\ndata: {body}\n\n".encode()
