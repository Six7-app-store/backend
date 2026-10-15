from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import User, UserRole
from app.schemas import UserCreate, UserUpdate


def get_user(db: Session, user_id: UUID) -> User | None:
    """Get user by ID"""
    return db.query(User).filter(User.userId == user_id).first()


def get_user_by_username(db: Session, username: str) -> User | None:
    """Get user by username"""
    return db.query(User).filter(User.username == username).first()


def get_user_by_email(db: Session, email: str) -> User | None:
    """Get user by email"""
    return db.query(User).filter(User.email == email).first()


def get_users(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    role: UserRole | None = None,
    course_id: UUID | None = None
) -> list[User]:
    """Get users with optional filters"""
    query = db.query(User)

    if role:
        query = query.filter(User.role == role)
    if course_id:
        query = query.filter(User.courseId == course_id)

    return query.order_by(User.created_at, User.userId).offset(skip).limit(limit).all()


def create_user(db: Session, user: UserCreate) -> User:
    """Create a new user"""
    db_user = User(
        email=user.email,
        username=user.username,
        role=user.role,
        courseId=user.courseId
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def update_user(db: Session, user_id: UUID, user_update: UserUpdate) -> User | None:
    """Update user information"""
    db_user = get_user(db, user_id)
    if not db_user:
        return None

    update_data = user_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_user, field, value)

    db.commit()
    db.refresh(db_user)
    return db_user




def delete_user(db: Session, user_id: UUID) -> bool:
    """Delete a user"""
    db_user = get_user(db, user_id)
    if not db_user:
        return False

    db.delete(db_user)
    db.commit()
    return True


def search_users(db: Session, query: str, limit: int = 10) -> list[User]:
    """Search users by username or email"""
    return db.query(User).filter(
        or_(
            User.username.ilike(f"%{query}%"),
            User.email.ilike(f"%{query}%")
        )
    ).limit(limit).all()
