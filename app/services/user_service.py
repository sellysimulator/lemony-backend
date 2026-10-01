"""The `users` table: upsert and lookup by verified Firebase uid."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models.user import User


def _clip(value: str | None, length: int) -> str | None:
    return value[:length] if isinstance(value, str) else None


class UserService:
    def get_by_firebase_uid(
        self, db: Session, firebase_uid: str, *, for_update: bool = False
    ) -> User | None:
        query = select(User).where(User.firebase_uid == firebase_uid)
        if for_update:
            query = query.with_for_update()
        return db.execute(query).scalar_one_or_none()

    def _get_or_create(self, db: Session, firebase_uid: str) -> User:
        """The row for a uid, inserting it if missing, safe against a concurrent insert.

        Two requests for the same uid (a doubled `/users/upsert` at sign-in, or an
        upsert racing a game persist or guest claim) can both miss the lookup and
        both INSERT. The
        INSERT runs in a savepoint: the loser rolls back only that savepoint,
        keeping the caller's transaction, and re-reads the winner's row. The
        re-read is a locking read because MySQL's REPEATABLE READ snapshot would
        otherwise still show no row.
        """
        user = self.get_by_firebase_uid(db, firebase_uid)
        if user is not None:
            return user
        try:
            with db.begin_nested():
                user = User(firebase_uid=firebase_uid)
                db.add(user)
        except IntegrityError:
            user = self.get_by_firebase_uid(db, firebase_uid, for_update=True)
            if user is None:
                raise
        return user

    def upsert_user(
        self,
        db: Session,
        *,
        firebase_uid: str,
        display_name: str | None,
        email: str | None,
        photo_url: str | None,
    ) -> User:
        user = self._get_or_create(db, firebase_uid)
        user.display_name = _clip(display_name, settings.MAX_DISPLAY_NAME_LENGTH)
        user.email = _clip(email, 200)
        user.photo_url = _clip(photo_url, 500)
        db.commit()
        db.refresh(user)
        return user

    def ensure_user(self, db: Session, firebase_uid: str) -> User:
        """The user row for a uid, created bare if the client never upserted it."""
        return self._get_or_create(db, firebase_uid)
