"""A day's events do not grow with the game: only resyncs carry the history."""

import pytest

from app.sockets.handlers import game

from ..conftest import new_guest
from .test_play import PLAN, config, connect

HISTORY = ("days", "last_day_events", "summary")


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
