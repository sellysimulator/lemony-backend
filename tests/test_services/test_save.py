"""A finished game is never left behind unsaved: the client hears when a save fails, can
retry it, and a new game is refused while the last one is still unsaved."""

from typing import Any

import pytest

from app.services import db_service
from app.services import game_service as gs
from app.services.state_service import state_svc
from app.sockets.handlers import game

from ..conftest import new_guest
from .test_play import PLAN, config, connect


@pytest.fixture
def db_down(monkeypatch) -> dict[str, bool]:
    """While `down["on"]` is true every save fails, as a lost database connection does."""
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
async def test_a_new_game_never_abandons_a_finished_game_that_is_not_saved(emits, db_down):
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
async def test_persist_if_finished_returns_none_while_the_write_fails(db_down):
    guest = new_guest()
    document = await gs.create_game(guest, config(1))
    await gs.submit_day(guest, {"game_id": document["game_id"], "day": 1, **PLAN})
    finished = await state_svc.get_game(document["game_id"])
    assert finished is not None
    assert await gs.persist_if_finished(finished) is None and not finished["persisted"]
