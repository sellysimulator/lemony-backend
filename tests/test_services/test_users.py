from app.db.session import SessionLocal
from app.models.user import User
from app.services.user_service import UserService


def _lose_the_race(service: UserService) -> None:
    """Make the first uid lookup miss, as if a concurrent request inserted the
    row between this request's SELECT and its INSERT."""
    real = service.get_by_firebase_uid
    calls = {"n": 0}

    def lookup(db, firebase_uid, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db, firebase_uid, **kwargs)

    service.get_by_firebase_uid = lookup  # type: ignore[method-assign]


def _existing(uid: str) -> None:
    with SessionLocal() as db:
        db.add(User(firebase_uid=uid, display_name="Old"))
        db.commit()


def test_upsert_that_loses_the_insert_race_updates_the_winner_row():
    _existing("uid-race")
    service = UserService()
    _lose_the_race(service)
    with SessionLocal() as db:
        user = service.upsert_user(
            db, firebase_uid="uid-race", display_name="New", email=None, photo_url=None
        )
        assert user.display_name == "New"
    with SessionLocal() as db:
        rows = db.query(User).filter(User.firebase_uid == "uid-race").all()
        assert [r.display_name for r in rows] == ["New"]


def test_ensure_user_that_loses_the_insert_race_keeps_the_outer_transaction():
    _existing("uid-ensure")
    service = UserService()
    _lose_the_race(service)
    with SessionLocal() as db:
        db.add(User(firebase_uid="uid-same-txn"))
        user = service.ensure_user(db, "uid-ensure")
        db.commit()
        assert user.firebase_uid == "uid-ensure"
    with SessionLocal() as db:
        assert db.query(User).filter(User.firebase_uid == "uid-ensure").count() == 1
        # Work done earlier in the same transaction survived the duplicate.
        assert db.query(User).filter(User.firebase_uid == "uid-same-txn").count() == 1
