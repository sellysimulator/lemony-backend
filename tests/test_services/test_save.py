"""A finished game is never left behind unsaved: the client hears when a save fails, can
retry it, and a new game is refused while the last one is still unsaved."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.game import Game, UserStats
from app.services import db_service, stats_service
from app.services import game_service as gs
from app.services.state_service import state_svc
from app.sockets.handlers import game

from ..conftest import new_guest
from .test_play import PLAN, config, connect


@pytest.fixture
def db_down(monkeypatch) -> dict[str, bool]:
    """While `down["on"]` is true every save fails, as a lost database connection
    does."""
    down = {"on": True}
    real = db_service.persist_finished_game

    def flaky(document: dict[str, Any], summary: dict[str, Any]) -> str | None:
        return None if down["on"] else real(document, summary)

    monkeypatch.setattr(db_service, "persist_finished_game", flaky)
    return down


async def finish_a_game(emits, sid: str = "s1") -> tuple[str, str]:
    guest = new_guest()
    await connect(sid, {"guestId": guest})
    await game.create_game(sid, {"config": config(1)})
    game_id = emits.of("game_created")[-1]["game_id"]
    await game.submit_day(sid, {"game_id": game_id, "day": 1, **PLAN})
    return guest, game_id


@pytest.mark.asyncio
async def test_a_failed_save_is_reported_and_a_retry_saves_it(emits, db_down):
    _, game_id = await finish_a_game(emits)
    assert emits.of("game_finished") and not emits.of("game_persisted")
    failed = emits.of("game_persist_failed")
    assert len(failed) == 1 and failed[0]["seq"] > emits.of("game_finished")[0]["seq"]

    await game.retry_persist("s1", {"game_id": game_id})  # still down
    assert len(emits.of("game_persist_failed")) == 2 and not emits.of("game_persisted")

    db_down["on"] = False
    await game.retry_persist("s1", {"game_id": game_id})
    assert emits.of("game_persisted")[-1]["public_id"] == game_id
    stored = await state_svc.get_game(game_id)
    assert stored is not None and stored["persisted"] and stored["public_id"] == game_id

    # Asking again once saved just confirms it.
    await game.retry_persist("s1", {"game_id": game_id})
    assert emits.of("game_persisted")[-1]["public_id"] == game_id
    seqs = [d["seq"] for _, e, d in emits.events if e != "plan_preview"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_a_new_game_never_abandons_a_finished_game_that_is_not_saved(
    emits, db_down
):
    guest, game_id = await finish_a_game(emits)

    await game.create_game("s1", {"config": config(2)})
    err = emits.of("error")[-1]
    assert err["code"] == "UNSAVED_GAME" and "not saved yet" in err["message"]
    assert len(emits.of("game_created")) == 1
    assert await state_svc.get_active_game_id(guest) == game_id
    old = await state_svc.get_game(game_id)
    assert old is not None and not old["persisted"]

    # Back up: creating a game saves the last one first, then starts the new one.
    db_down["on"] = False
    await game.create_game("s1", {"config": config(2)})
    assert len(emits.of("game_created")) == 2
    new_id = emits.of("game_created")[-1]["game_id"]
    assert await state_svc.get_active_game_id(guest) == new_id != game_id
    old = await state_svc.get_game(game_id)
    assert old is not None and old["persisted"] and old["public_id"] == game_id


@pytest.mark.asyncio
async def test_a_saved_finished_game_does_not_block_a_new_one(emits):
    _, game_id = await finish_a_game(emits)
    assert emits.of("game_persisted")[-1]["public_id"] == game_id
    await game.create_game("s1", {"config": config(2)})
    assert not emits.of("error") and len(emits.of("game_created")) == 2


@pytest.mark.asyncio
async def test_a_refresh_retries_the_save_and_says_how_it_went(emits, db_down):
    guest, game_id = await finish_a_game(emits)
    await connect("s2", {"guestId": guest})
    await game.resume_game("s2", {"game_id": game_id})
    assert emits.of("game_persist_failed")[-1]["seq"] > 0
    db_down["on"] = False
    await game.resume_game("s2", {"game_id": game_id})
    assert emits.of("game_persisted")[-1]["public_id"] == game_id


@pytest.mark.asyncio
async def test_retry_is_refused_for_an_unfinished_game_or_someone_elses(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config(2)})
    game_id = emits.of("game_created")[0]["game_id"]
    await game.retry_persist("s1", {"game_id": game_id})
    assert emits.of("error")[-1]["code"] == "NOT_FINISHED"

    await connect("s2", {"guestId": new_guest()})
    await game.retry_persist("s2", {"game_id": game_id})
    assert emits.of("error")[-1]["code"] == "NO_GAME"
    assert not emits.of("game_persisted") and not emits.of("game_persist_failed")


@pytest.mark.asyncio
async def test_a_stats_failure_never_fails_the_save(
    emits, firebase_tokens, monkeypatch
):
    def broken(db: Any, user_id: int) -> None:
        raise RuntimeError("stats are broken")

    monkeypatch.setattr(stats_service, "recompute_user_stats", broken)
    firebase_tokens["tok"] = {"uid": "uid-save"}
    await connect("s1", {"idToken": "tok"})
    await game.create_game("s1", {"config": config(1)})
    game_id = emits.of("game_created")[-1]["game_id"]
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})

    assert emits.of("game_persisted")[-1]["public_id"] == game_id
    assert not emits.of("game_persist_failed")
    stored = await state_svc.get_game(game_id)
    assert stored is not None and stored["persisted"] and stored["public_id"] == game_id
    with SessionLocal() as db:  # saved, and the stats really did fail
        saved = db.execute(select(Game).where(Game.public_id == game_id)).scalar_one()
        assert saved.user_id is not None and db.get(UserStats, saved.user_id) is None
    # So "Play again" is open: the next game starts.
    await game.create_game("s1", {"config": config(2)})
    assert not emits.of("error") and len(emits.of("game_created")) == 2


@pytest.mark.asyncio
async def test_persist_if_finished_returns_none_while_the_write_fails(db_down):
    guest = new_guest()
    document = await gs.create_game(guest, config(1))
    await gs.submit_day(guest, {"game_id": document["game_id"], "day": 1, **PLAN})
    finished = await state_svc.get_game(document["game_id"])
    assert finished is not None
    assert await gs.persist_if_finished(finished) is None and not finished["persisted"]


# ------------------------------------------------------- background retries
@pytest.fixture
def retry_gate(monkeypatch) -> asyncio.Event:
    """Background retries wait (no lock held) until the returned event is set."""
    gate = asyncio.Event()

    async def sleep(_delay: float) -> None:
        await gate.wait()

    monkeypatch.setattr(game, "PERSIST_RETRY_DELAYS", (2.0, 10.0, 30.0))
    monkeypatch.setattr(
        game, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep})
    )
    return gate


class FlakyDb:
    """The first `failures` writes fail; `attempts` logs every write ("fail" / "ok")."""

    def __init__(self, monkeypatch, failures: int) -> None:
        self.failures = failures
        self.attempts: list[str] = []
        self._real = db_service.persist_finished_game
        monkeypatch.setattr(db_service, "persist_finished_game", self.write)

    def write(self, document: dict[str, Any], summary: dict[str, Any]) -> str | None:
        if len(self.attempts) < self.failures:
            self.attempts.append("fail")
            return None
        self.attempts.append("ok")
        return self._real(document, summary)


def stored_rows(game_id: str) -> int:
    with SessionLocal() as db:
        return len(db.execute(select(Game).where(Game.public_id == game_id)).all())


def persisted_to(emits) -> list[tuple[str, str]]:
    return [
        (sid, d["public_id"]) for sid, e, d in emits.events if e == "game_persisted"
    ]


@pytest.mark.asyncio
async def test_a_failed_save_is_retried_in_the_background_and_stored_once(
    emits, monkeypatch, retry_gate
):
    db = FlakyDb(monkeypatch, failures=2)
    _, game_id = await finish_a_game(emits)
    assert len(emits.of("game_persist_failed")) == 1 and game.persist_retry_in_flight(
        game_id
    )
    assert db.attempts == ["fail"]

    retry_gate.set()
    await game._persist_retries[game_id]
    assert db.attempts == ["fail", "fail", "ok"]
    assert persisted_to(emits) == [("s1", game_id)]
    assert stored_rows(game_id) == 1
    stored = await state_svc.get_game(game_id)
    assert stored is not None and stored["persisted"]
    assert not game.persist_retry_in_flight(game_id)
    seqs = [d["seq"] for _, e, d in emits.events if e != "plan_preview"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_the_retry_answers_the_sid_that_rejoined(emits, monkeypatch, retry_gate):
    db = FlakyDb(monkeypatch, failures=2)
    guest, game_id = await finish_a_game(emits)
    # A reconnect while the retry waits: no second write, and the answer goes to s2.
    await connect("s2", {"guestId": guest})
    await game.resume_game("s2", {"game_id": game_id})
    await game.request_state("s2", {"game_id": game_id})
    assert db.attempts == ["fail"]

    retry_gate.set()
    await game._persist_retries[game_id]
    assert persisted_to(emits) == [("s2", game_id)]
    assert stored_rows(game_id) == 1


@pytest.mark.asyncio
async def test_when_every_retry_fails_the_client_hears_it_again(
    emits, monkeypatch, retry_gate
):
    db = FlakyDb(monkeypatch, failures=4)
    _, game_id = await finish_a_game(emits)
    retry_gate.set()
    await game._persist_retries[game_id]
    assert db.attempts == ["fail"] * 4  # the first write and three retries
    assert len(emits.of("game_persist_failed")) == 2 and not emits.of("game_persisted")
    assert stored_rows(game_id) == 0 and not game.persist_retry_in_flight(game_id)

    # With no retry in flight, asking for the state tries the save again.
    await game.request_state("s1", {"game_id": game_id})
    assert persisted_to(emits) == [("s1", game_id)]
    assert stored_rows(game_id) == 1


@pytest.mark.asyncio
async def test_a_hand_retry_during_the_background_one_still_stores_once(
    emits, monkeypatch, retry_gate
):
    FlakyDb(monkeypatch, failures=1)
    _, game_id = await finish_a_game(emits)
    await game.retry_persist("s1", {"game_id": game_id})  # saved before the retry wakes
    assert persisted_to(emits) == [("s1", game_id)]
    retry_gate.set()
    await game._persist_retries[game_id]
    # It found the game saved: nothing more
    assert persisted_to(emits) == [("s1", game_id)]
    assert stored_rows(game_id) == 1


@pytest.mark.asyncio
async def test_a_rejoin_during_the_first_save_leaves_it_to_that_save(
    emits, monkeypatch
):
    db = FlakyDb(monkeypatch, failures=0)
    started, release = asyncio.Event(), asyncio.Event()
    real = gs.persist_if_finished

    async def slow(document: dict[str, Any]) -> str | None:
        started.set()
        await release.wait()
        return await real(document)

    monkeypatch.setattr(gs, "persist_if_finished", slow)
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": config(1)})
    game_id = emits.of("game_created")[-1]["game_id"]
    finishing = asyncio.create_task(
        game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    )
    await started.wait()  # the first write is under way
    assert game.persist_retry_in_flight(game_id)

    await connect("s2", {"guestId": guest})
    await game.resume_game("s2", {"game_id": game_id})
    await game.request_state("s2", {"game_id": game_id})
    release.set()
    await finishing
    assert db.attempts == ["ok"]  # no second write
    assert persisted_to(emits) == [("s2", game_id)]  # answered to the latest sid
    assert stored_rows(game_id) == 1 and not game.persist_retry_in_flight(game_id)


def hold_the_last_answer(
    monkeypatch, background: asyncio.Task[None]
) -> tuple[Any, Any]:
    """The background retry pauses just before its final answer: (answering, hold)."""
    answering, hold = asyncio.Event(), asyncio.Event()
    real_say = game._say_persisted

    async def say(sid: str, document: dict[str, Any], public_id: str | None) -> None:
        if asyncio.current_task() is background:
            answering.set()
            await hold.wait()
        await real_say(sid, document, public_id)

    monkeypatch.setattr(game, "_say_persisted", say)
    return answering, hold


@pytest.mark.asyncio
async def test_a_rejoin_while_the_last_retry_answers_saves_again(
    emits, monkeypatch, retry_gate
):
    db = FlakyDb(monkeypatch, failures=4)
    guest, game_id = await finish_a_game(emits)
    background = game._persist_retries[game_id]
    answering, hold = hold_the_last_answer(monkeypatch, background)
    retry_gate.set()
    await answering.wait()
    assert db.attempts == ["fail"] * 4 and not game.persist_retry_in_flight(game_id)

    await connect("s2", {"guestId": guest})
    await game.request_state("s2", {"game_id": game_id})  # a new attempt, and it saves
    assert db.attempts == ["fail"] * 4 + ["ok"]
    assert persisted_to(emits) == [("s2", game_id)]
    hold.set()
    await background
    assert stored_rows(game_id) == 1


@pytest.mark.asyncio
async def test_a_hand_retry_failing_while_the_last_retry_answers_is_retried_too(
    emits, monkeypatch, retry_gate
):
    db = FlakyDb(monkeypatch, failures=5)
    _, game_id = await finish_a_game(emits)
    background = game._persist_retries[game_id]
    answering, hold = hold_the_last_answer(monkeypatch, background)
    retry_gate.set()
    await answering.wait()
    retry_gate.clear()  # the next background retry waits until the end

    # Fails: background retries again
    await game.retry_persist("s1", {"game_id": game_id})
    assert db.attempts == ["fail"] * 5 and game.persist_retry_in_flight(game_id)
    again = game._persist_retries[game_id]
    assert again is not background
    hold.set()
    await background
    assert game.persist_retry_in_flight(game_id)  # the old task's end leaves it be
    retry_gate.set()
    await again
    assert db.attempts == ["fail"] * 5 + ["ok"]
    assert persisted_to(emits) == [("s1", game_id)] and stored_rows(game_id) == 1
