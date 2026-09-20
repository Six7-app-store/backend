from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.models import Course, User
from app.schemas import CourseCreate, CourseUpdate


def get_course(db: Session, course_id: UUID) -> Course | None:
    """Get course by ID"""
    return (
        db.query(Course)
        .options(selectinload(Course.course_teachers))
        .filter(Course.courseId == course_id)
        .first()
    )


def get_courses(db: Session, skip: int = 0, limit: int = 100) -> list[Course]:
    """Get all courses.

    ``course_teachers`` is eager-loaded because ``CourseResponse``
    serialises ``teacherIds`` for every row — lazy-loading would fire one
    query per course on a list of up to ``limit`` entries.
    """
    return (
        db.query(Course)
        .options(selectinload(Course.course_teachers))
        .offset(skip)
        .limit(limit)
        .all()
    )


def create_course(db: Session, course: CourseCreate) -> Course:
    """Create a new course"""
    db_course = Course(name=course.name)
    db.add(db_course)
    db.commit()
    db.refresh(db_course)
    return db_course


def update_course(db: Session, course_id: UUID, course_update: CourseUpdate) -> Course | None:
    """Update course information"""
    db_course = get_course(db, course_id)
    if not db_course:
        return None

    update_data = course_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_course, field, value)

    db.commit()
    db.refresh(db_course)
    return db_course


def delete_course(db: Session, course_id: UUID) -> bool:
    """Delete a course.

    The user-course link is a nullable FK on ``users.courseId`` and
    deleting the course leaves enrolled users dangling. We unlink them
    first so the deletion is idempotent and never trips the FK
    constraint, regardless of how the schema is configured.
    """
    db_course = get_course(db, course_id)
    if not db_course:
        return False

    # Detach members so the row deletion is allowed even if the FK
    # is configured as ``ON DELETE RESTRICT``.
    db.query(User).filter(User.courseId == course_id).update(
        {User.courseId: None}, synchronize_session=False
    )

    db.delete(db_course)
    db.commit()
    return True


# ----------------------------------------------------------------
# MEMBER MANAGEMENT
# ----------------------------------------------------------------
# Members live as ``users.courseId`` on the ``User`` row, not in a
# join table — a user is in at most one course at a time. The helpers
# below treat that single FK as a small membership API so the router
# stays slim.

def get_course_members(db: Session, course_id: UUID) -> list[User]:
    """Return users currently enrolled in ``course_id``."""
    return db.query(User).filter(User.courseId == course_id).order_by(User.username).all()


def add_users_to_course(
    db: Session,
    course_id: UUID,
    user_ids: Iterable[UUID],
) -> list[User]:
    """Add a batch of users to a course.

    Sets ``users.courseId`` for every existing user in ``user_ids``.
    Missing user-ids are skipped silently — the caller already
    validated the picker contents and there's no useful error to
    surface for "this user vanished between picker and submit".
    Returns the affected user rows post-update.
    """
    ids = [uid for uid in user_ids if uid is not None]
    if not ids:
        return []

    db.query(User).filter(User.userId.in_(ids)).update(
        {User.courseId: course_id}, synchronize_session=False
    )
    db.commit()
    return db.query(User).filter(User.userId.in_(ids)).all()


def remove_user_from_course(
    db: Session,
    course_id: UUID,
    user_id: UUID,
) -> bool:
    """Remove ``user_id`` from ``course_id``.

    Returns ``True`` if the user was actually a member of that course
    and got detached, ``False`` if they weren't (so the router can
    answer 404 cleanly). Detaching a user from a different course is
    NOT done — that would silently mutate unrelated state.
    """
    user = db.query(User).filter(
        User.userId == user_id,
        User.courseId == course_id,
    ).first()
    if not user:
        return False
    user.courseId = None
    db.commit()
    return True
