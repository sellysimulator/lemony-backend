"""A day's events do not grow with the game: only resyncs carry the history."""

import json

import pytest

from app.services import state_service
from app.sockets.handlers import game

from ..conftest import new_guest
from .test_play import PLAN, config, connect

HISTORY = ("days", "summary")


@pytest.mark.asyncio
async def test_round_events_leave_the_history_out_and_resyncs_carry_it(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config(3)})
    game_id = emits.of("game_created")[0]["game_id"]
    start = emits.of("game_state")[-1]
    assert start["full"] is True and start["state"]["days"] == []

    for day in (1, 2):
        await game.submit_day("s1", {"game_id": game_id, "day": day, **PLAN})
        result = emits.of("day_result")[-1]
        assert result["record"]["day"] == day and result["events"]
        assert not set(HISTORY) & set(result["state"])
        assert result["state"]["phase"] == "played" and result["state"]["day"] == day

        await game.ack_day("s1", {"game_id": game_id, "day": day})
        acked = emits.of("game_state")[-1]
        assert acked["full"] is False and not set(HISTORY) & set(acked["state"])
        assert acked["state"]["phase"] == "planning" and acked["state"]["day"] == day + 1

    await game.request_state("s1", {"game_id": game_id})
    asked = emits.of("game_state")[-1]
    assert asked["full"] is True and [d["day"] for d in asked["state"]["days"]] == [1, 2]

    await game.resume_game("s1", {"game_id": game_id})
    resumed = emits.of("game_state")[-1]
    assert resumed["full"] is True and [d["day"] for d in resumed["state"]["days"]] == [1, 2]


@pytest.mark.asyncio
async def test_the_customers_go_out_once_and_are_never_stored(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config(3)})
    created = emits.of("game_created")[0]
    game_id = created["game_id"]
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    live = emits.of("day_result")[-1]
    assert live["duplicate"] is False and len(live["events"]) == live["record"]["visitors"] > 0

    stored = await state_service.state_svc.get_game(game_id)
    assert stored is not None and "last_day_events" not in stored["engine"]
    assert '"outcome"' not in json.dumps(stored)

    # The day sent again: the record, and `events: null` (not available), never an empty list.
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    dup = emits.of("day_result")[-1]
    assert dup["duplicate"] is True and dup["events"] is None
    assert dup["record"] == live["record"]

    # A refresh while the day is played but not acknowledged: the full state has no list.
    await game.resume_game("s1", {"game_id": game_id, "session_token": created["session_token"]})
    resumed = emits.of("game_state")[-1]
    assert resumed["full"] is True and resumed["state"]["phase"] == "played"
    assert "last_day_events" not in resumed["state"]
    assert resumed["state"]["days"][-1] == live["record"]


@pytest.mark.asyncio
async def test_a_document_saved_with_the_customer_list_still_plays_and_drops_it(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config(2)})
    game_id = emits.of("game_created")[0]["game_id"]
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    live = emits.of("day_result")[-1]
    # As saved before the change: the engine payload still holds the day's customers.
    document = await state_service.state_svc.get_game(game_id)
    assert document is not None
    document["engine"]["last_day_events"] = live["events"]
    await state_service.state_svc.save_game(document)

    await game.resume_game("s1", {"game_id": game_id})
    assert "last_day_events" not in emits.of("game_state")[-1]["state"]
    await game.ack_day("s1", {"game_id": game_id, "day": 1})
    await game.submit_day("s1", {"game_id": game_id, "day": 2, **PLAN})
    assert not emits.of("error") and emits.of("day_result")[-1]["events"]
    saved = await state_service.state_svc.get_game(game_id)
    assert saved is not None and "last_day_events" not in saved["engine"]


@pytest.mark.asyncio
async def test_the_last_day_leaves_the_summary_to_game_finished(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config(1)})
    game_id = emits.of("game_created")[0]["game_id"]
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    result = emits.of("day_result")[-1]
    assert result["state"]["phase"] == "finished" and "summary" not in result["state"]
    assert [d["day"] for d in emits.of("game_finished")[0]["summary"]["days"]] == [1]

    # A refresh of a finished game is a resync: the full state carries the summary.
    await game.resume_game("s1", {"game_id": game_id})
    resumed = emits.of("game_state")[-1]
    assert resumed["full"] is True and resumed["state"]["summary"]["days_played"] == 1


@pytest.mark.asyncio
async def test_the_final_submission_is_reported_to_the_player_before_the_game_ends(emits):
    # Single-player has no host view to go stale: the one client that submitted always gets
    # the final day's record and the finished state in the same reply, then the summary.
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config(2)})
    game_id = emits.of("game_created")[0]["game_id"]
    for day in (1, 2):
        await game.submit_day("s1", {"game_id": game_id, "day": day, **PLAN})
        if day == 1:
            await game.ack_day("s1", {"game_id": game_id, "day": 1})
    last = emits.of("day_result")[-1]
    assert last["record"]["day"] == 2 and last["state"]["phase"] == "finished"
    assert emits.of("game_finished")[-1]["summary"]["days_played"] == 2
