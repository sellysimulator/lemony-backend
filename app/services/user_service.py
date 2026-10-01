"""The `users` table: upsert and lookup by verified Firebase uid."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.user import User


def _clip(value: str | None, length: int) -> str | None:
    return value[:length] if isinstance(value, str) else None


class UserService:
    def get_by_firebase_uid(self, db: Session, firebase_uid: str) -> User | None:
        return db.execute(
            select(User).where(User.firebase_uid == firebase_uid)
        ).scalar_one_or_none()

    def upsert_user(
        self,
        db: Session,
        *,
        firebase_uid: str,
        display_name: str | None,
        email: str | None,
        photo_url: str | None,
    ) -> User:
        user = self.get_by_firebase_uid(db, firebase_uid)
        if user is None:
            user = User(firebase_uid=firebase_uid)
            db.add(user)
        user.display_name = _clip(display_name, settings.MAX_DISPLAY_NAME_LENGTH)
        user.email = _clip(email, 200)
        user.photo_url = _clip(photo_url, 500)
        db.commit()
        db.refresh(user)
        return user

    def ensure_user(self, db: Session, firebase_uid: str) -> User:
        """The user row for a uid, created bare if the client never upserted it."""
        user = self.get_by_firebase_uid(db, firebase_uid)
        if user is None:
            user = User(firebase_uid=firebase_uid)
            db.add(user)
            db.flush()
        return user
