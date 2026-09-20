import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base
from app.utils.time import utcnow


# ----------------------------------------------------------------
# ENUMS
# ----------------------------------------------------------------
class UserRole(str, enum.Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class AppVersionApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskType(str, enum.Enum):
    DEPLOY = "deploy"
    UPDATE = "update"
    DESTROY = "destroy"
    PAUSE = "pause"
    RESUME = "resume"
    # Single-resource redeploy of ONE Compute-Instance via
    # ``terraform apply -replace=<addr> -target=<addr>``, without
    # touching the other team VMs of the same deployment.
    REDEPLOY = "redeploy"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpenStackAuthType(str, enum.Enum):
    APPLICATION_CREDENTIAL = "v3applicationcredential"
    PASSWORD = "password"


class IdentityProvider(str, enum.Enum):
    KEYCLOAK = "keycloak"
    LTI = "lti"


# ----------------------------------------------------------------
# COURSE MODEL
# ----------------------------------------------------------------
class Course(Base):
    __tablename__ = "courses"

    courseId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)

    # Relationships
    users = relationship("User", back_populates="course")
    # Moodle course contexts that were mapped onto this course. Empty
    # for every course until somebody maps one — see ``LtiContext``.
    lti_contexts = relationship("LtiContext", back_populates="course")
    # Many-to-many to User via the ``course_teachers`` join table:
    # a course can have several teachers and a teacher several courses.
    # Backs the ``is_course_teacher`` capability check.
    course_teachers = relationship(
        "CourseTeacher",
        back_populates="course",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def teacherIds(self) -> list[uuid.UUID]:
        """User-ids of this course's designated teachers.

        Exposed on ``CourseResponse`` so a client can tell apart the
        courses it may edit or delete (``ensure_edit_course``: admin or
        course-teacher of *this* course) from the ones it may only read.
        Without it the UI could only guess from the role and would offer
        a delete button that answers 403.
        """
        return [ct.userId for ct in self.course_teachers]


# ----------------------------------------------------------------
# USER MODEL
# ----------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    userId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    keycloak_id = Column(String, unique=True, index=True, nullable=True)  # Keycloak User ID (sub)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=False)
    firstName = Column(String, nullable=True)
    lastName = Column(String, nullable=True)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.STUDENT)
    courseId = Column(UUID(as_uuid=True), ForeignKey("courses.courseId"), nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)

    # Relationships
    course = relationship("Course", back_populates="users")
    apps = relationship("App", back_populates="user")
    deployments = relationship("Deployment", back_populates="user")
    user_to_deployments = relationship("UserToDeployment", back_populates="user")
    user_to_teams = relationship("UserToTeam", back_populates="user")
    course_teacher_links = relationship(
        "CourseTeacher",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    openstack_credential = relationship(
        "UserOpenStackCredential",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    identities = relationship(
        "UserIdentity",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# APP MODEL
# ----------------------------------------------------------------
class App(Base):
    __tablename__ = "apps"

    appId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    image = Column(LargeBinary, nullable=True)  # raw bytes of the uploaded logo
    image_mime = Column(String(64), nullable=True)  # e.g. "image/png" — needed to build a data-URL on read
    git_link = Column(String, nullable=True)
    is_private = Column(Boolean, nullable=False, default=False)
    userId = Column(UUID(as_uuid=True), ForeignKey("users.userId"), nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow)
    # Soft-delete marker. When set, the app is hidden from default
    # queries but the row stays so existing deployments keep their FK
    # valid. Soft-delete is refused while the app has live deployments.
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="apps")
    deployments = relationship("Deployment", back_populates="app")
    version_approvals = relationship(
        "AppVersionApproval",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# DEPLOYMENT MODEL
# ----------------------------------------------------------------
class Deployment(Base):
    __tablename__ = "deployments"

    deploymentId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    releaseTag = Column(String, nullable=True)
    userInputVar = Column(Text, nullable=True)  # could also be JSON
    userId = Column(UUID(as_uuid=True), ForeignKey("users.userId"), nullable=False, index=True)
    appId = Column(UUID(as_uuid=True), ForeignKey("apps.appId"), nullable=False, index=True)
    # Soft-delete marker. Set to hide the deployment from default
    # queries while keeping the row for audit/restore. DELETE is only
    # allowed in terminal states, so OpenStack resources are already
    # gone by the time this is set.
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="deployments")
    app = relationship("App", back_populates="deployments")
    user_to_deployments = relationship(
        "UserToDeployment",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    teams = relationship(
        "Team",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tasks = relationship(
        "Task",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# TASK MODEL
# ----------------------------------------------------------------
class Task(Base):
    __tablename__ = "tasks"

    taskId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    celeryTaskId = Column(String, nullable=True)
    type = Column(Enum(TaskType), nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    logs = Column(Text, nullable=True)  # JSON or text
    tf_state = Column(Text, nullable=True)  # Terraform state as JSON/text
    outputs = Column(Text, nullable=True)  # Terraform outputs as JSON/text
    # Live-progress columns, updated by the celery event listener on
    # each ``task-progress`` event. Advisory only — the SSE stream is
    # canonical; these let a page reload show the last known phase/pct.
    current_phase = Column(String(50), nullable=True)
    progress_pct = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    # Relationships
    deployment = relationship("Deployment", back_populates="tasks")


# ----------------------------------------------------------------
# USERTODEPLOYMENT MODEL
# ----------------------------------------------------------------
class UserToDeployment(Base):
    __tablename__ = "user_to_deployments"

    userToDeploymentId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    user = relationship("User", back_populates="user_to_deployments")
    deployment = relationship("Deployment", back_populates="user_to_deployments")


# ----------------------------------------------------------------
# TEAM MODEL
# ----------------------------------------------------------------
class Team(Base):
    __tablename__ = "teams"

    teamId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    deployment = relationship("Deployment", back_populates="teams")
    user_to_teams = relationship(
        "UserToTeam",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# USERTOTEAM MODEL
# ----------------------------------------------------------------
class UserToTeam(Base):
    __tablename__ = "user_to_teams"

    userToTeamId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    teamId = Column(
        UUID(as_uuid=True),
        ForeignKey("teams.teamId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    user = relationship("User", back_populates="user_to_teams")
    team = relationship("Team", back_populates="user_to_teams")


# ----------------------------------------------------------------
# USER OPENSTACK CREDENTIAL MODEL
# ----------------------------------------------------------------
class UserOpenStackCredential(Base):
    __tablename__ = "user_openstack_credentials"

    credentialId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    auth_type = Column(
        Enum(
            OpenStackAuthType,
            name="openstackauthtype",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )

    # Non-secret display fields (plaintext)
    auth_url = Column(String, nullable=False)
    region_name = Column(String, nullable=True)
    interface = Column(String, nullable=True, default="public")
    identity_api_version = Column(String, nullable=True, default="3")
    project_id = Column(String, nullable=True)
    project_name = Column(String, nullable=True)
    user_domain_name = Column(String, nullable=True)
    project_domain_name = Column(String, nullable=True)

    # Encrypted (Fernet ciphertext) — never logged, never returned via API
    encrypted_identifier = Column(LargeBinary, nullable=False)
    encrypted_secret = Column(LargeBinary, nullable=False)

    last_validated_at = Column(DateTime, nullable=True)
    last_validation_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    user = relationship("User", back_populates="openstack_credential")


# ----------------------------------------------------------------
# APP VERSION APPROVAL MODEL
# ----------------------------------------------------------------
class AppVersionApproval(Base):
    __tablename__ = "app_version_approvals"

    approvalId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appId = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.appId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_tag = Column(String, nullable=False)
    status = Column(
        Enum(AppVersionApprovalStatus),
        nullable=False,
        default=AppVersionApprovalStatus.PENDING,
    )
    diff_url = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    reviewed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("appId", "version_tag", name="uq_app_version_approval"),
    )

    # Relationships
    app = relationship("App", back_populates="version_approvals")
    reviewer = relationship("User", foreign_keys=[reviewed_by])


# ----------------------------------------------------------------
# COURSE TEACHER MODEL
# ----------------------------------------------------------------
# Many-to-many join between courses and users. A row ``(course_id,
# user_id)`` declares that ``user`` is one of the teachers responsible
# for ``course``. Backs the per-course "course-teacher" capability.
# Both FKs together form the composite primary key.
class CourseTeacher(Base):
    __tablename__ = "course_teachers"

    courseId = Column(
        "course_id",
        UUID(as_uuid=True),
        ForeignKey("courses.courseId", ondelete="CASCADE"),
        primary_key=True,
    )
    userId = Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )

    # Relationships
    course = relationship("Course", back_populates="course_teachers")
    user = relationship("User", back_populates="course_teacher_links")


# ----------------------------------------------------------------
# USER IDENTITY MODEL
# ----------------------------------------------------------------
# One row per external account a user can sign in with. An LTI
# ``sub`` is only unique *within one platform*, so the identity is the
# triple (provider, issuer, subject) — never the subject alone.
#
# A column per provider on ``User`` would have been the shorter route,
# but it stops working at the second Moodle instance, and the project
# is required to support two sign-in paths already. ``User.keycloak_id``
# stays where it is for now; migrating it into this table is a separate,
# self-contained change.
class UserIdentity(Base):
    __tablename__ = "user_identities"

    identityId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(Enum(IdentityProvider), nullable=False)
    # The issuing system. For LTI this is the platform's ``iss``, i.e.
    # Moodle's wwwroot.
    issuer = Column(String, nullable=False)
    # The provider's own identifier for the user (``sub``).
    subject = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider", "issuer", "subject", name="uq_user_identity_provider_subject"
        ),
    )

    # Relationships
    user = relationship("User", back_populates="identities")


# ----------------------------------------------------------------
# LTI CONTEXT MODEL
# ----------------------------------------------------------------
# A Moodle course, recorded as its own entity.
#
# Deliberately NOT folded into ``Course``: a ``Course`` here is a
# Studiengruppe, which is not the same thing as a Moodle course, and
# equating them automatically would assert a mapping nobody has
# confirmed. ``courseId`` is therefore nullable and stays empty until
# somebody maps it on purpose.
class LtiContext(Base):
    __tablename__ = "lti_contexts"

    ltiContextId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Platform ``iss`` — the same value as in ``UserIdentity.issuer``.
    issuer = Column(String, nullable=False)
    # The platform's own course id (the ``context`` claim's ``id``).
    context_id = Column(String, nullable=False)
    title = Column(String, nullable=True)
    label = Column(String, nullable=True)
    # Where the platform serves this course's member list (NRPS). The
    # URL arrives in the launch and is stored because the import runs
    # later, on a request of its own, when no launch is in hand. Null
    # when the platform does not offer the service — the tool then has
    # no way to read the roster and the import says so instead of
    # guessing.
    memberships_url = Column(String, nullable=True)
    courseId = Column(
        UUID(as_uuid=True),
        ForeignKey("courses.courseId", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("issuer", "context_id", name="uq_lti_context_issuer_context"),
    )

    # Relationships
    course = relationship("Course", back_populates="lti_contexts")
