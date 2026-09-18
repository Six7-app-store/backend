import ipaddress
import socket
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import AppVersionApprovalStatus, OpenStackAuthType, TaskStatus, TaskType, UserRole


# ----------------------------------------------------------------
# USER SCHEMAS
# ----------------------------------------------------------------
class UserBase(BaseModel):
    email: EmailStr
    username: str


class UserCreate(UserBase):
    role: UserRole = UserRole.STUDENT
    courseId: UUID | None = None


class UserUpdate(BaseModel):
    # Profile fields are managed exclusively through Keycloak; the only
    # update path here is ``role`` and it is admin-only.
    role: UserRole | None = None
    courseId: UUID | None = None


class UserResponse(UserBase):
    userId: UUID
    role: UserRole
    courseId: UUID | None = None
    keycloak_id: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserWithCourse(UserResponse):
    course: "CourseResponse | None" = None

    model_config = ConfigDict(from_attributes=True)


class Token(BaseModel):
    access_token: str
    token_type: str


# ----------------------------------------------------------------
# COURSE SCHEMAS
# ----------------------------------------------------------------
class CourseBase(BaseModel):
    name: str


class CourseCreate(CourseBase):
    pass


class CourseUpdate(BaseModel):
    name: str | None = None


class CourseResponse(CourseBase):
    courseId: UUID

    model_config = ConfigDict(from_attributes=True)


class CourseWithUsers(CourseResponse):
    users: list[UserResponse] = []

    model_config = ConfigDict(from_attributes=True)


class CourseMembersUpdate(BaseModel):
    """Body for bulk-adding members to a course.

    Course membership lives on ``users.courseId``, so a "member" is a
    user-id whose FK is (re-)pointed to this course; an existing
    enrollment on another course is overwritten.
    """
    userIds: list[UUID] = []


# ----------------------------------------------------------------
# APP SCHEMAS
# ----------------------------------------------------------------
# Private IP ranges that must never be reachable via git_link to
# prevent SSRF. Includes RFC-1918, loopback, link-local, and the
# IANA-reserved 100.64/10 (CGN) block.
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_host(hostname: str) -> bool:
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(hostname))
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except (socket.gaierror, ValueError):
        # Cannot resolve or not an IP literal — let the git client
        # handle it; we have blocked the obvious SSRF vectors.
        return False


class AppBase(BaseModel):
    name: str
    description: str | None = None
    git_link: str | None = None
    is_private: bool = False

    @field_validator("git_link")
    @classmethod
    def validate_git_link(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # git@ SSH URLs (e.g. git@github.com:owner/repo.git) are handled
        # specially: urlparse does not recognise them as having a scheme.
        if v.startswith("git@"):
            # Extract hostname from git@<host>:path
            try:
                host_part = v[4:].split(":")[0]
            except IndexError:
                raise ValueError("Invalid git@ URL format")
            if _is_private_host(host_part):
                raise ValueError(
                    "git_link must point to a public host; private/internal "
                    "addresses are not allowed"
                )
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("https",):
            raise ValueError(
                "git_link must use https:// (or git@ SSH) — plain HTTP is not allowed"
            )
        hostname = parsed.hostname or ""
        if _is_private_host(hostname):
            raise ValueError(
                "git_link must point to a public host; private/internal "
                "addresses are not allowed"
            )
        return v


class AppCreate(AppBase):
    # Image is sent as a full data-URL string, e.g.
    # ``"data:image/png;base64,iVBORw0KG..."``. The router decodes the
    # base64 part to bytes and stores mime + bytes in two columns.
    image: str | None = None
    # When True and is_private=False, all existing Git tags are
    # automatically submitted for review right after creation.
    submit_all_versions: bool = False


class AppUpdate(BaseModel):
    # ``git_link`` is intentionally NOT editable — once an app has
    # deployments, changing the repo would make the version history
    # inconsistent. Callers may include it; it is silently ignored.
    name: str | None = None
    description: str | None = None
    is_private: bool | None = None
    # Same data-URL convention as ``AppCreate``. Pass ``""`` to clear
    # the image; ``None`` (the default) leaves it unchanged.
    image: str | None = None

    model_config = ConfigDict(extra="ignore")


class AppResponse(AppBase):
    appId: UUID
    userId: UUID
    created_at: datetime
    is_private: bool
    # Data-URL or null. Populated from ``app.image`` + ``app.image_mime``
    # by ``serialize_app_image``; the raw bytes never leave the backend.
    image: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AppWithUser(AppResponse):
    user: UserResponse

    model_config = ConfigDict(from_attributes=True)


class AppWithVersions(AppWithUser):
    versions: list[dict[str, str]] = []

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# APP VERSION APPROVAL SCHEMAS
# ----------------------------------------------------------------
class AppVersionApprovalSubmit(BaseModel):
    diff_url: str | None = None
    notes: str | None = None


class AppVersionApprovalDecision(BaseModel):
    rejection_reason: str


class AppVersionApprovalResponse(BaseModel):
    approvalId: UUID
    appId: UUID
    version_tag: str
    status: AppVersionApprovalStatus
    diff_url: str | None = None
    notes: str | None = None
    rejection_reason: str | None = None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AppVersionApprovalWithApp(AppVersionApprovalResponse):
    app: AppResponse

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# DEPLOYMENT SCHEMAS
# ----------------------------------------------------------------
class Team(BaseModel):
    name: str
    userIds: list[str] = []


class DeploymentBase(BaseModel):
    name: str
    appId: UUID


class DeploymentFileUpload(BaseModel):
    """Single file uploaded by the teacher in the deploy wizard.

    Persisted verbatim into ``userInputVar.terraform`` keyed by the
    variable name (and recipient key for team/user scope) so the worker
    passes it to terraform and cloud-init decodes it into ``write_files``.

    ``content_b64`` MUST be standard (RFC 4648) base64. The value sent
    to Terraform stays base64 for cloud-init's ``encoding: b64``.
    """
    name: str
    content_b64: str
    size: int
    content_type: str | None = None


class DeploymentCreate(DeploymentBase):
    releaseTag: str | None = None
    userInputVar: dict[str, Any] | None = None
    # Files-map keyed by ``@openstack:file:<scope>``-marked variable
    # name. Inner key:
    #   * scope = all  → exactly one inner key, conventionally "all"
    #   * scope = team → one entry per team name
    #   * scope = user → one entry per ``Team-User`` composite key
    # The backend doesn't auto-route — what the wizard puts in here
    # ends up 1:1 in the Terraform variable.
    files: dict[str, dict[str, DeploymentFileUpload]] | None = None
    teams: list[Team] = []


class DeploymentResponse(DeploymentBase):
    deploymentId: UUID
    userId: UUID
    releaseTag: str | None = None
    userInputVar: dict[str, Any] | None = None
    status: str | None = None  # From latest task
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class DeploymentWithRelations(DeploymentResponse):
    user: UserResponse
    app: AppResponse

    model_config = ConfigDict(from_attributes=True)


# Team response for deployment details (from DB)
class DeploymentTeamMember(BaseModel):
    userId: UUID
    email: str
    username: str

    model_config = ConfigDict(from_attributes=True)


class DeploymentTeamResponse(BaseModel):
    teamId: UUID
    name: str
    members: list[DeploymentTeamMember] = []

    model_config = ConfigDict(from_attributes=True)


# Task summary for deployment details
class TaskSummary(BaseModel):
    taskId: UUID
    type: TaskType
    status: TaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    # Live-progress fields (mirror of Task model), used to render the
    # progress bar on page reload; SSE supplies real-time updates.
    current_phase: str | None = None
    progress_pct: int | None = None

    model_config = ConfigDict(from_attributes=True)


# Terraform outputs parsed
class DeploymentOutputs(BaseModel):
    """Parsed Terraform outputs - structure depends on app"""
    raw: dict[str, Any] | None = None  # Full outputs as dict

    model_config = ConfigDict(from_attributes=True)


# Per-member self-access response
class MyAccessResponse(BaseModel):
    """A single team member's own access credentials.

    Returned by ``GET /deployments/{id}/my-access`` so a member (student)
    can see their OWN credentials without the owner-view that gates the
    full outputs. Only the caller's own account is ever present.

    Both maps mirror the raw terraform ``user_accounts`` / ``team_vms``
    output shape (one key each: the member's account and their team's VM)
    so the frontend's existing account-matching pipeline consumes this
    unchanged. Empty maps mean "no credentials yet" (no successful deploy,
    or the app issued no per-user account) — a valid 200, not an error.
    """
    user_accounts: dict[str, dict[str, Any]] = {}
    team_vms: dict[str, dict[str, Any]] = {}

    model_config = ConfigDict(from_attributes=True)


# Full deployment detail response
class DeploymentDetail(DeploymentWithRelations):
    """Full deployment details with teams, task info, and outputs"""
    teams: list[DeploymentTeamResponse] = []
    latest_task: TaskSummary | None = None
    outputs: DeploymentOutputs | None = None
    logs: str | None = None  # Optional: can be excluded for large logs

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# DEPLOYMENT RESOURCE / INFRASTRUCTURE SCHEMAS
# ----------------------------------------------------------------
# These mirror the dataclasses in ``app/services/deployment_status.py``
# 1:1 so the conversion at the endpoint boundary is a plain
# ``model_validate(asdict(view))``. Defined separately from the
# dataclasses because Pydantic integrates better with FastAPI's OpenAPI
# emitter and the service module stays free of a Pydantic dependency.


class LifecycleStatesSchema(BaseModel):
    """Server lifecycle quad + optional fault message.

    Fields are pass-through from OpenStack's Nova response (stable
    strings like ``"ACTIVE"`` / ``"BUILD"`` / ``"ERROR"``). The frontend
    renders a pill from ``status`` + ``task_state`` and shows
    ``fault_message`` as a banner when ``status == "ERROR"``.
    """
    status: str | None = None
    task_state: str | None = None
    vm_state: str | None = None
    power_state: str | None = None
    fault_message: str | None = None


class HardwareSpecSchema(BaseModel):
    flavor_name: str | None = None
    ram_mb: int | None = None
    vcpus: int | None = None
    disk_gb: int | None = None
    image_id: str | None = None
    image_name: str | None = None
    availability_zone: str | None = None
    launched_at: str | None = None


class NetworkAddressSchema(BaseModel):
    network: str
    fixed_ip: str | None = None
    floating_ip: str | None = None
    mac: str | None = None


class NetworkPortSchema(BaseModel):
    port_id: str
    network_id: str | None = None
    status: str | None = None
    mac: str | None = None
    fixed_ip: str | None = None
    security_group_ids: list[str] = []


class SecurityGroupSummarySchema(BaseModel):
    id: str
    name: str
    description: str | None = None
    ingress_rules: int
    egress_rules: int


class VolumeAttachmentSchema(BaseModel):
    volume_id: str
    device: str | None = None
    size_gb: int | None = None
    bootable: bool | None = None
    status: str | None = None
    name: str | None = None


class DeploymentResourceSchema(BaseModel):
    """One row in the resource list (stage 1) or the response of the
    detail endpoint (stage 2 — same shape, more fields populated).

    Non-instance categories (network, subnet, etc.) only populate
    ``address``, ``type``, ``category``, ``provider_id``,
    ``display_name``; the instance-only fields stay ``None`` /
    empty list.
    """
    address: str
    type: str
    category: str
    team: str | None = None
    provider_id: str
    display_name: str
    drift: Literal["in_sync", "stale", "missing"] = "in_sync"
    # Stage 1 fields (instance-only)
    lifecycle: LifecycleStatesSchema | None = None
    hardware: HardwareSpecSchema | None = None
    addresses: list[NetworkAddressSchema] = []
    # Stage 2 fields (instance-only, only set on the detail endpoint)
    ports: list[NetworkPortSchema] | None = None
    security_groups: list[SecurityGroupSummarySchema] | None = None
    volumes: list[VolumeAttachmentSchema] | None = None
    metadata: dict[str, str] | None = None


class DeploymentResourceListResponse(BaseModel):
    """Wrap the list in an object so we can add cursor/refresh-stamp
    metadata later without a breaking API change."""
    resources: list[DeploymentResourceSchema]
    # True when the response was fetched with live OpenStack join.
    # False means the caller passed ``?refresh=false`` and the
    # response reflects only cached TF state.
    live: bool


# ----------------------------------------------------------------
# Task SCHEMAS
# ----------------------------------------------------------------
class TaskBase(BaseModel):
    deploymentId: UUID
    celeryTaskId: str
    type: TaskType
    status: TaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: str | None = None
    tf_state: str | None = None
    outputs: str | None = None
    current_phase: str | None = None
    progress_pct: int | None = None

class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: str | None = None
    tf_state: str | None = None
    outputs: str | None = None
    current_phase: str | None = None
    progress_pct: int | None = None

class TaskResponse(TaskBase):
    taskId: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# TEAM SCHEMAS
#
# Teams are bound directly to a deployment (FK
# ``deployments.deploymentId`` -> ``Team.deploymentId``). The schemas
# mirror the model 1:1.
# ----------------------------------------------------------------
class TeamBase(BaseModel):
    name: str
    deploymentId: UUID


class TeamCreate(TeamBase):
    userIds: list[UUID] = []


class TeamUpdate(BaseModel):
    name: str | None = None


class TeamResponse(TeamBase):
    teamId: UUID

    model_config = ConfigDict(from_attributes=True)


class TeamWithMembers(TeamResponse):
    users: list[UserResponse] = []

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# STATISTICS SCHEMAS
# ----------------------------------------------------------------
class UserStatistics(BaseModel):
    total_apps: int
    total_deployments: int
    successful_deployments: int
    failed_deployments: int
    pending_deployments: int


# ----------------------------------------------------------------
# OPENSTACK CREDENTIAL SCHEMAS
# ----------------------------------------------------------------
class OpenStackCredentialBase(BaseModel):
    """Non-secret connection metadata. Mirrors the `clouds.yaml` shape."""
    auth_type: OpenStackAuthType
    auth_url: str
    region_name: str | None = None
    interface: str | None = "public"
    identity_api_version: str | None = "3"
    project_id: str | None = None
    project_name: str | None = None
    user_domain_name: str | None = None
    project_domain_name: str | None = None


class OpenStackCredentialUpsert(OpenStackCredentialBase):
    """Body of PUT /me/openstack-credentials.

    `identifier` is either a username (password auth) or an
    application-credential ID. `secret` is the corresponding password or
    application-credential secret. Both are stored encrypted at rest.
    """
    identifier: str = Field(..., min_length=1, description="username OR application-credential ID")
    secret: str = Field(..., min_length=1, description="password OR application-credential secret")

    @model_validator(mode="after")
    def _validate_required_fields(self) -> "OpenStackCredentialUpsert":
        if self.auth_type == OpenStackAuthType.PASSWORD:
            if not self.user_domain_name:
                raise ValueError("user_domain_name is required for password auth")
            if not (self.project_id or self.project_name):
                raise ValueError("project_id or project_name is required for password auth")
        # application-credential auth: project_id is recommended but not required;
        # the credential itself carries the project scope.
        return self


class OpenStackCredentialFromYaml(BaseModel):
    """Convenience body: paste/upload a `clouds.yaml` and let the server pick it apart."""
    clouds_yaml: str = Field(..., min_length=1, description="raw clouds.yaml file contents")
    cloud_name: str | None = Field(
        None, description="Pick a specific cloud from the YAML; required when there is more than one"
    )


class OpenStackCredentialResponse(OpenStackCredentialBase):
    """Masked response — never returns identifier/secret material.

    When `has_credential` is False, base fields are absent/empty and the
    consumer should branch on `has_credential`. `is_locked` and
    `active_deployments` are populated regardless so the frontend can
    render the appropriate guard UI even before any credential exists.
    """
    auth_type: OpenStackAuthType | None = None  # type: ignore[assignment]
    auth_url: str | None = None  # type: ignore[assignment]
    has_credential: bool = True
    last_validated_at: datetime | None = None
    last_validation_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    is_locked: bool = False
    active_deployments: int = 0

    model_config = ConfigDict(from_attributes=True)
