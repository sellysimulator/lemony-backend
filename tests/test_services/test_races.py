"""A game deleted while another call is still working on it stays deleted.

`abandon_game` and `create_game(replace=True)` delete a live game. A call that read the
game before the delete must not save it back (an orphan nobody's active pointer names,
alive until its TTL): `bump_seq` reports the game gone instead of saving a stale copy,
and `create_game` takes the old game's lock before deciding to delete it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.game import Game
from app.services import game_service as gs
from app.services.state_service import state_svc
from app.sockets.handlers import game

from ..conftest import new_guest
from .test_play import PLAN, config, connect


@pytest.mark.asyncio
async def test_bump_seq_never_brings_back_a_deleted_game() -> None:
    guest = new_guest()
    document = await gs.create_game(guest, config())
    stale = await gs.require_owned(guest, document["game_id"])
    await gs.abandon_game(guest, document["game_id"])

    assert await gs.bump_seq(stale) is None
    assert await state_svc.get_game(document["game_id"]) is None
    assert stale["seq"] == document["seq"]  # the caller's copy is left as it was


@pytest.mark.asyncio
async def test_a_day_whose_game_is_abandoned_meanwhile_sends_nothing(
    emits: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last day is played, then the game is abandoned before `day_result` goes out:
    no event for the gone game, no save of it, and it does not come back."""
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": config(1)})
    game_id = emits.of("game_created")[-1]["game_id"]
    real_submit = gs.submit_day

    async def then_abandoned(identity: str, data: dict[str, Any]) -> Any:
        played = await real_submit(identity, data)
        await gs.abandon_game(identity, game_id)
        return played

    monkeypatch.setattr(gs, "submit_day", then_abandoned)
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})

    for event in ("day_result", "game_finished", "game_persisted", "game_persist_failed"):
        assert not emits.of(event), event
    assert await state_svc.get_game(game_id) is None
    with SessionLocal() as db:
        assert db.scalars(select(Game).where(Game.public_id == game_id)).first() is None


async def replace_during_a_submission(
    monkeypatch: pytest.MonkeyPatch, days: int
) -> tuple[str, str, str]:
    """`create_game(replace=True)` arrives while `submit_day` holds the old game's lock,
    between reading the game and saving it. Returns (identity, old id, new id)."""
    guest = new_guest()
    old = await gs.create_game(guest, config(days))
    old_id = old["game_id"]

    holding = asyncio.Event()  # submit_day has read the game and is about to save it
    contended = asyncio.Event()  # someone else asked for the old game's lock meanwhile
    release = asyncio.Event()
    real_save, real_lock = state_svc.save_game, state_svc.lock

    async def gated_save(document: dict[str, Any]) -> None:
        if document["game_id"] == old_id and not release.is_set():
            holding.set()
            await release.wait()  # the forced suspension point between read and write
        await real_save(document)

    def watched_lock(game_id: str) -> Any:
        if game_id == old_id and holding.is_set():
            contended.set()
        return real_lock(game_id)

    monkeypatch.setattr(state_svc, "save_game", gated_save)
    monkeypatch.setattr(state_svc, "lock", watched_lock)

    submit = asyncio.create_task(gs.submit_day(guest, {"game_id": old_id, "day": 1, **PLAN}))
    await holding.wait()
    create = asyncio.create_task(gs.create_game(guest, config(), replace=True))
    waiting = asyncio.create_task(contended.wait())
    # Unblock the submission once the new game is made (no lock taken) or once the
    # replace waits for the old game's lock, whichever comes first.
    await asyncio.wait({create, waiting}, return_when=asyncio.FIRST_COMPLETED)
    waiting.cancel()
    release.set()
    await submit
    new = await create
    return guest, old_id, new["game_id"]


@pytest.mark.asyncio
async def test_replacing_a_game_mid_submission_leaves_no_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest, old_id, new_id = await replace_during_a_submission(monkeypatch, days=2)
    assert await state_svc.get_game(old_id) is None
    assert await state_svc.get_active_game_id(guest) == new_id != old_id


@pytest.mark.asyncio
async def test_a_game_that_finishes_while_being_replaced_is_saved_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The submission plays the last day: by the time the replace looks, the game is
    finished, so it is saved (as any finished active game is) rather than deleted."""
    guest, old_id, new_id = await replace_during_a_submission(monkeypatch, days=1)
    with SessionLocal() as db:
        assert db.scalars(select(Game).where(Game.public_id == old_id)).first() is not None
    old = await state_svc.get_game(old_id)
    assert old is not None and old["persisted"]
    assert await state_svc.get_active_game_id(guest) == new_id != old_id
