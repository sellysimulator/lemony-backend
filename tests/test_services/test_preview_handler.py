"""LE-4: the `preview_plan` socket handler and its `plan_preview` reply.

Refusals must look exactly like `submit_day`'s (same code, same message) but travel
inside the reply, never as an `error` event. The reply goes to the asking sid only,
carries no `seq`, and changes nothing. The limiter is module-global, so every test
uses its own sids.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from app.core.defaults import default_config
from app.core.engine import DayPlan
from app.core.preview import plan_preview
from app.services import game_service as gs
from app.services import state_service
from app.sockets.handlers import connection, game
from app.sockets.handlers.game import PREVIEW_LIMIT, PREVIEW_WINDOW, preview_plan
from app.sockets.manager import sio

from ..conftest import Recorder, new_guest

PURCHASES = {"ice": {"100": 1}, "sugar": {"100": 1}, "lemons": {"100": 1}, "cups": {"100": 1}}
RECIPE = {"ice": 2, "sugar": 2, "lemons": 3}
NO_GAME = "That game does not exist or is not yours."


def new_sid() -> str:
    return f"sid-{uuid.uuid4().hex}"


async def connect(sid: str, guest: str | None = None) -> None:
    await connection.connect(sid, {}, {"guestId": guest or new_guest()})


def config(days: int = 2) -> dict[str, Any]:
    c = default_config()
    c["num_days"] = days
    return c


async def start(emits: Recorder, days: int = 2) -> tuple[str, str, str]:
    """(sid, guest, game_id) for a fresh game."""
    sid, guest = new_sid(), new_guest()
    await connect(sid, guest)
    await game.create_game(sid, {"config": config(days)})
    return sid, guest, emits.of("game_created")[-1]["game_id"]


def request(game_id: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": "r1",
        "game_id": game_id,
        "day": 1,
        "purchases": copy.deepcopy(PURCHASES),
        "recipe": dict(RECIPE),
    }
    body.update(over)
    return body


def previews(emits: Recorder) -> list[Any]:
    return emits.of("plan_preview")


async def submit_error(emits: Recorder, sid: str, body: dict[str, Any]) -> dict[str, Any]:
    """What `submit_day` answers to the same plan (price added, request_id dropped)."""
    before = len(emits.of("error"))
    payload = {k: v for k, v in body.items() if k != "request_id"}
    payload.setdefault("price", 0.5)
    await game.submit_day(sid, payload)
    errors = emits.of("error")
    assert len(errors) == before + 1, "submit_day was expected to refuse this plan"
    return errors[-1]


# ------------------------------------------------------------------- success
@pytest.mark.asyncio
async def test_preview_replies_to_the_asker_only_and_changes_nothing(emits):
    sid, guest, game_id = await start(emits)
    other = new_sid()
    await connect(other, guest)  # same player, second tab
    emits.events.clear()
    before = await state_service.state_svc.get_game(game_id)

    await preview_plan(sid, request(game_id))

    assert len(emits.events) == 1  # no other event at all
    to, event, reply = emits.events[0]
    assert (to, event) == (sid, "plan_preview")
    assert "seq" not in reply and not emits.of("error")
    assert set(reply) == {
        "request_id",
        "day",
        "prices",
        "chance",
        "expected_visitors",
        "expected_cups",
        "expected_margin",
        "cups_possible",
        "cost_per_cup",
    }
    assert reply["request_id"] == "r1" and reply["day"] == 1
    engine = gs.engine_of(before)
    price_min = engine.config.min_max_values.price.min
    plan = DayPlan(purchases=PURCHASES, recipe=RECIPE, price=price_min)
    assert reply == {"request_id": "r1", "day": 1, **plan_preview(engine, plan)}
    assert await state_service.state_svc.get_game(game_id) == before  # seq included


@pytest.mark.asyncio
async def test_previews_do_not_change_the_day_that_is_then_played(emits):
    sid, _, game_id = await start(emits)
    document = await state_service.state_svc.get_game(game_id)
    assert document is not None
    plan = DayPlan(purchases=PURCHASES, recipe=RECIPE, price=0.6)
    expected = gs.engine_of(document).run_day(plan)["record"]

    for i in range(3):
        await preview_plan(sid, request(game_id, request_id=f"p{i}", recipe={**RECIPE, "ice": i}))
    await game.submit_day(sid, {"game_id": game_id, "day": 1, "price": 0.6, **request(game_id)})
    assert emits.of("day_result")[0]["record"] == expected


@pytest.mark.asyncio
async def test_preview_follows_the_game_to_the_next_day(emits):
    sid, _, game_id = await start(emits)
    await game.submit_day(
        sid, {"game_id": game_id, "day": 1, "price": 0.5, "purchases": PURCHASES, "recipe": RECIPE}
    )
    await game.ack_day(sid, {"game_id": game_id, "day": 1})
    emits.events.clear()
    await preview_plan(sid, request(game_id, day=2))
    reply = previews(emits)[0]
    assert reply["day"] == 2 and "error" not in reply and reply["prices"]


# -------------------------------------------------------------- request_id
@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["a", "x" * 64])
async def test_request_ids_of_1_to_64_characters_are_answered(emits, request_id):
    sid, _, game_id = await start(emits)
    await preview_plan(sid, request(game_id, request_id=request_id))
    assert previews(emits)[0]["request_id"] == request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, 7, "", "x" * 65, ["a"], {"a": 1}])
async def test_a_bad_request_id_is_dropped_silently(emits, request_id):
    sid, _, game_id = await start(emits)
    emits.events.clear()
    await preview_plan(sid, request(game_id, request_id=request_id))
    assert emits.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, "nope", [], {}])
async def test_a_payload_without_a_request_id_is_dropped(emits, data):
    sid, _, _ = await start(emits)
    emits.events.clear()
    await preview_plan(sid, data)
    assert emits.events == []


# ----------------------------------------------------------------- refusals
def bad_recipe(body: dict[str, Any]) -> None:
    body["recipe"] = {"ice": 1, "sugar": 1, "lemons": 99}


def missing_lemons(body: dict[str, Any]) -> None:
    body["recipe"] = {"ice": 1, "sugar": 1}


def unknown_ingredient(body: dict[str, Any]) -> None:
    body["purchases"] = {"gold": {"100": 1}}


def not_sold_in_that_size(body: dict[str, Any]) -> None:
    body["purchases"] = {"ice": {"123": 1}}


def over_cash(body: dict[str, Any]) -> None:
    body["purchases"] = {"lemons": {"700": 100}}


def purchases_wrong_type(body: dict[str, Any]) -> None:
    body["purchases"] = "lots"


def recipe_wrong_type(body: dict[str, Any]) -> None:
    body["recipe"] = "tasty"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate, message_part",
    [
        (bad_recipe, "lemons per cup must be between"),
        (missing_lemons, "Recipe must set ice, sugar and lemons"),
        (unknown_ingredient, "Unknown ingredient: gold"),
        (not_sold_in_that_size, "ice is not sold in packs of 123"),
        (over_cash, "you only have"),
        (purchases_wrong_type, "purchases"),
        (recipe_wrong_type, "recipe"),
    ],
)
async def test_invalid_plans_are_refused_like_submit_day_inside_the_reply(
    emits, mutate, message_part
):
    sid, _, game_id = await start(emits)
    body = request(game_id)
    mutate(body)
    emits.events.clear()

    await preview_plan(sid, body)

    assert not emits.of("error")  # never an error event
    assert len(emits.events) == 1 and emits.events[0][0] == sid
    reply = previews(emits)[0]
    assert "seq" not in reply and set(reply) == {"request_id", "day", "error"}
    assert reply["request_id"] == "r1" and reply["day"] == 1
    assert reply["error"]["code"] == "INVALID_PLAN" and message_part in reply["error"]["message"]
    assert reply["error"] == await submit_error(emits, sid, body)


@pytest.mark.asyncio
async def test_other_identitys_game_reads_as_no_game(emits):
    _, _, game_id = await start(emits)
    thief = new_sid()
    await connect(thief)
    emits.events.clear()

    await preview_plan(thief, request(game_id))

    assert not emits.of("error") and len(emits.events) == 1 and emits.events[0][0] == thief
    reply = previews(emits)[0]
    assert reply == {
        "request_id": "r1",
        "day": 1,
        "error": {"code": "NO_GAME", "message": NO_GAME},
    }
    assert reply["error"] == await submit_error(emits, thief, request(game_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("game_id", ["nope", None])
async def test_a_missing_game_is_no_game(emits, game_id):
    sid, _, _ = await start(emits)
    emits.events.clear()
    await preview_plan(sid, request(game_id))
    reply = previews(emits)[0]
    assert reply["error"] == {"code": "NO_GAME", "message": NO_GAME} and not emits.of("error")


@pytest.mark.asyncio
async def test_day_that_is_not_a_number(emits):
    sid, _, game_id = await start(emits)
    emits.events.clear()
    await preview_plan(sid, request(game_id, day="1"))
    reply = previews(emits)[0]
    assert reply == {
        "request_id": "r1",
        "day": None,
        "error": {"code": "INVALID_PLAN", "message": "day must be a number"},
    }
    assert not emits.of("error")
    assert reply["error"] == await submit_error(emits, sid, request(game_id, day="1"))


@pytest.mark.asyncio
async def test_checks_run_in_submit_days_order(emits):
    sid, _, game_id = await start(emits)
    emits.events.clear()
    # ownership before day, day before the plan
    await preview_plan(sid, request("nope", day="x", recipe="bad"))
    await preview_plan(sid, request(game_id, request_id="r2", day=2, recipe="bad"))
    await preview_plan(sid, request(game_id, request_id="r3", day="x", recipe="bad"))
    assert [p["error"]["code"] for p in previews(emits)] == ["NO_GAME", "WRONG_DAY", "INVALID_PLAN"]
    assert previews(emits)[2]["error"]["message"] == "day must be a number"


@pytest.mark.asyncio
async def test_another_day_is_wrong_day(emits):
    sid, _, game_id = await start(emits)
    emits.events.clear()
    for day in (2, 0):
        await preview_plan(sid, request(game_id, request_id=f"d{day}", day=day))
    first, second = previews(emits)
    assert first["day"] == 2 and second["day"] == 0
    assert first["error"] == {"code": "WRONG_DAY", "message": "It is day 1, not day 2."}
    assert second["error"] == {"code": "WRONG_DAY", "message": "It is day 1, not day 0."}
    assert not emits.of("error")
    assert first["error"] == await submit_error(emits, sid, request(game_id, day=2))


@pytest.mark.asyncio
async def test_a_played_day_is_wrong_day(emits):
    sid, _, game_id = await start(emits, days=2)
    await game.submit_day(
        sid, {"game_id": game_id, "day": 1, "price": 0.5, "purchases": PURCHASES, "recipe": RECIPE}
    )
    emits.events.clear()
    before = await state_service.state_svc.get_game(game_id)

    await preview_plan(sid, request(game_id, request_id="a"))
    await preview_plan(sid, request(game_id, request_id="b", day=2))

    played, next_day = previews(emits)
    assert played["error"] == {"code": "WRONG_DAY", "message": "It is day 1, not day 1."}
    assert next_day["error"] == {"code": "WRONG_DAY", "message": "It is day 1, not day 2."}
    assert not emits.of("error") and len(emits.events) == 2
    assert await state_service.state_svc.get_game(game_id) == before


@pytest.mark.asyncio
async def test_a_finished_game_is_wrong_day(emits):
    sid, _, game_id = await start(emits, days=1)
    await game.submit_day(
        sid, {"game_id": game_id, "day": 1, "price": 0.5, "purchases": PURCHASES, "recipe": RECIPE}
    )
    assert emits.of("game_finished")
    emits.events.clear()
    await preview_plan(sid, request(game_id))
    assert previews(emits)[0]["error"]["code"] == "WRONG_DAY" and not emits.of("error")
    assert len(emits.events) == 1


# --------------------------------------------------------------- rate limit
@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [100.0]
    monkeypatch.setattr(game, "PREVIEW_CLOCK", lambda: now[0])
    return now


def test_limit_constants():
    assert PREVIEW_LIMIT == 5 and PREVIEW_WINDOW == 1.0


@pytest.mark.asyncio
async def test_the_sixth_request_in_a_window_gets_no_reply_until_the_window_passes(emits, clock):
    sid, _, game_id = await start(emits)
    emits.events.clear()
    for i in range(PREVIEW_LIMIT):
        await preview_plan(sid, request(game_id, request_id=f"r{i}"))
        clock[0] += 0.1
    assert len(previews(emits)) == PREVIEW_LIMIT

    await preview_plan(sid, request(game_id, request_id="sixth"))
    assert len(previews(emits)) == PREVIEW_LIMIT  # no reply, not even a refusal
    assert not emits.of("error")

    clock[0] += PREVIEW_WINDOW + 0.5  # every earlier request is out of the window
    await preview_plan(sid, request(game_id, request_id="again"))
    assert previews(emits)[-1]["request_id"] == "again" and "error" not in previews(emits)[-1]


@pytest.mark.asyncio
async def test_refused_requests_count_toward_the_limit(emits, clock):
    sid, _, game_id = await start(emits)
    emits.events.clear()
    for i in range(PREVIEW_LIMIT):
        await preview_plan(sid, request("nope", request_id=f"r{i}"))  # NO_GAME replies
    assert len(previews(emits)) == PREVIEW_LIMIT
    await preview_plan(sid, request(game_id, request_id="valid"))
    assert len(previews(emits)) == PREVIEW_LIMIT


@pytest.mark.asyncio
async def test_the_limit_is_per_sid(emits, clock):
    sid, guest, game_id = await start(emits)
    other = new_sid()
    await connect(other, guest)
    emits.events.clear()
    for i in range(PREVIEW_LIMIT + 1):
        await preview_plan(sid, request(game_id, request_id=f"a{i}"))
    await preview_plan(other, request(game_id, request_id="b0"))
    replies = [(to, d["request_id"]) for to, e, d in emits.events if e == "plan_preview"]
    assert (other, "b0") in replies
    assert sum(1 for to, _ in replies if to == sid) == PREVIEW_LIMIT


@pytest.mark.asyncio
async def test_other_events_are_not_limited(emits, clock):
    sid, _, game_id = await start(emits)
    for i in range(PREVIEW_LIMIT + 2):
        await preview_plan(sid, request(game_id, request_id=f"r{i}"))
    emits.events.clear()
    await game.request_state(sid, {"game_id": game_id})
    assert emits.of("game_state")


def test_the_handler_is_registered_as_a_socket_event():
    assert "preview_plan" in sio.handlers["/"]
