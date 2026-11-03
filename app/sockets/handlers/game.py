"""The play loop over Socket.IO.

| client -> server                               | server -> client                          |
|------------------------------------------------|-------------------------------------------|
| create_game {config, replace?}                 | game_created {game_id, session_token} + game_state (full) |
| resume_game {game_id?, session_token?}         | game_state (full), or no_active_game      |
| request_state {game_id}                        | game_state (full)                         |
| submit_day {game_id, day, purchases, price, recipe} | day_result {day, record, events, duplicate, state (daily)} (+ game_finished, then game_persisted or game_persist_failed, on the last day) |
| ack_day {game_id, day}                         | game_state (daily)                        |
| retry_persist {game_id}                        | game_persisted {public_id}, or game_persist_failed |
| abandon_game {game_id}                         | game_abandoned                            |
| preview_plan {request_id, game_id, day, purchases, recipe, price?} | plan_preview {request_id, day, prices, chance, expected_visitors, expected_cups, expected_margin, cups_possible, cost_per_cup, spend, cash_after, units, can_add, and with a price: price, price_error, at_price}, or {request_id, day, error: {code, message}}; no `seq`, changes no state; at most 5 per second per sid, the excess gets no reply |
| (any refusal)                                  | error {code, message}                     |

`game_state` is `{seq, full, state}`. A full state carries the history (`days`,
`last_day_events`, and `summary` once finished); a daily state leaves those out
so a day's events do not grow with the game. The client appends
`day_result.record` to the days it holds (only when its day is later than the
last one held) and takes the summary from `game_finished`. Every resync is full.

Every server event carries a monotonic per-game `seq`, except `plan_preview`, a
request-scoped reply that is not a state event. `session_token` is sent
in exactly one event, `game_created`, to the creating sid.

`plan_preview` carries every figure the plan screen shows (see `core/preview.py`): the
spend, the cash after it, the units bought, which packs can still be added (`can_add`,
per ingredient and size), and, for the price sent, the server's verdict on it
(`price_error`, null when it is fine) and the curve at exactly that price with the margin
per cup (`at_price`, null when the price is refused). Without a `price` those three keys
are absent.

Saving a finished game: `game_persisted` when it is stored, `game_persist_failed`
when the write failed (the client offers a retry, `retry_persist`; `resume_game`
retries too). `create_game` retries the save of a finished, unsaved active game
first and refuses with `UNSAVED_GAME` if it still fails, so no finished game is
left behind unsaved.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ...core.engine import DayPlan, PlanError
from ...core.preview import plan_preview
from ...services import game_service as gs
from ..errors import guarded
from ..manager import sio, socket_manager

logger = logging.getLogger(__name__)

# At most PREVIEW_LIMIT preview_plan requests per PREVIEW_WINDOW seconds per sid, counted on
# arrival (refused ones too). The excess gets no reply. Tests patch PREVIEW_CLOCK.
PREVIEW_LIMIT = 5
PREVIEW_WINDOW = 1.0
PREVIEW_CLOCK: Callable[[], float] = time.monotonic
_preview_times: dict[str, deque[float]] = {}


def _identity(sid: str) -> str | None:
    return socket_manager.sid_to_identity.get(sid)


async def _error(sid: str, code: str, message: str) -> None:
    await socket_manager.emit_to_sid(sid, "error", {"code": code, "message": message})


async def _send_state(sid: str, document: dict[str, Any], full: bool = True) -> None:
    seq = await gs.bump_seq(document)
    await socket_manager.emit_to_sid(
        sid, "game_state", {"seq": seq, "full": full, "state": gs.public_state(document, full)}
    )


async def _persist(sid: str, document: dict[str, Any]) -> None:
    """Save a finished game and say how it went: `game_persisted` or `game_persist_failed`."""
    public_id = await gs.persist_if_finished(document)
    seq = await gs.bump_seq(document)
    if public_id:
        await socket_manager.emit_to_sid(
            sid, "game_persisted", {"seq": seq, "public_id": public_id}
        )
    else:
        await socket_manager.emit_to_sid(sid, "game_persist_failed", {"seq": seq})


async def _finish(sid: str, document: dict[str, Any]) -> None:
    seq = await gs.bump_seq(document)
    await socket_manager.emit_to_sid(
        sid, "game_finished", {"seq": seq, "summary": gs.summary_of(document)}
    )
    await _persist(sid, document)


def _payload(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


@sio.event
@guarded()
async def create_game(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    payload = _payload(data)
    try:
        document = await gs.create_game(
            identity, payload.get("config"), bool(payload.get("replace"))
        )
    except gs.GameError as exc:
        await _error(sid, exc.code, exc.message)
        return
    seq = await gs.bump_seq(document)
    await socket_manager.emit_to_sid(
        sid,
        "game_created",
        {"seq": seq, "game_id": document["game_id"], "session_token": document["session_token"]},
    )
    await _send_state(sid, document)


@sio.event
@guarded()
async def resume_game(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    payload = _payload(data)
    token = payload.get("session_token")
    document = await gs.load_owned(
        identity, payload.get("game_id"), token if isinstance(token, str) else None
    )
    if document is None and payload.get("game_id"):
        # A stale local pointer: fall back to whatever this identity is playing.
        document = await gs.load_owned(identity, None)
    if document is None:
        await socket_manager.emit_to_sid(sid, "no_active_game", {"seq": 0})
        return
    await _send_state(sid, document)
    if document["engine"]["phase"] == "finished" and not document.get("persisted"):
        await _finish(sid, document)  # retry a persistence that failed earlier


@sio.event
@guarded()
async def request_state(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    try:
        document = await gs.require_owned(identity, _payload(data).get("game_id"))
    except gs.GameError as exc:
        await _error(sid, exc.code, exc.message)
        return
    await _send_state(sid, document)


@sio.event
@guarded()
async def submit_day(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    try:
        document, result, duplicate = await gs.submit_day(identity, _payload(data))
    except gs.GameError as exc:
        await _error(sid, exc.code, exc.message)
        return
    seq = await gs.bump_seq(document)
    await socket_manager.emit_to_sid(
        sid,
        "day_result",
        {
            "seq": seq,
            "day": result["record"]["day"],
            "record": result["record"],
            "events": result["events"],
            "duplicate": duplicate,
            "state": gs.public_state(document, full=False),
        },
    )
    if document["engine"]["phase"] == "finished" and not duplicate:
        await _finish(sid, document)


@sio.event
@guarded()
async def ack_day(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    payload = _payload(data)
    try:
        document = await gs.acknowledge_day(identity, payload.get("game_id"), payload.get("day"))
    except gs.GameError as exc:
        await _error(sid, exc.code, exc.message)
        return
    await _send_state(sid, document, full=False)


@sio.event
@guarded()
async def retry_persist(sid: str, data: Any = None) -> None:
    """Retry saving a finished game whose save failed (or answer that it is saved)."""
    identity = _identity(sid)
    if identity is None:
        return
    try:
        document = await gs.require_owned(identity, _payload(data).get("game_id"))
        if document["engine"]["phase"] != "finished":
            raise gs.GameError("NOT_FINISHED", "This game is not finished yet.")
    except gs.GameError as exc:
        await _error(sid, exc.code, exc.message)
        return
    await _persist(sid, document)


@sio.event
@guarded()
async def abandon_game(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    game_id = _payload(data).get("game_id")
    try:
        await gs.abandon_game(identity, game_id)
    except gs.GameError as exc:
        await _error(sid, exc.code, exc.message)
        return
    await socket_manager.emit_to_sid(sid, "game_abandoned", {"seq": 0, "game_id": game_id})


def _preview_allowed(sid: str) -> bool:
    now = PREVIEW_CLOCK()
    for key in list(_preview_times):
        stamps = _preview_times[key]
        while stamps and now - stamps[0] >= PREVIEW_WINDOW:
            stamps.popleft()
        if not stamps:
            del _preview_times[key]
    stamps = _preview_times.setdefault(sid, deque())
    stamps.append(now)
    return len(stamps) <= PREVIEW_LIMIT


@sio.event
@guarded()
async def preview_plan(sid: str, data: Any = None) -> None:
    identity = _identity(sid)
    if identity is None:
        return
    if not _preview_allowed(sid):
        return
    payload = _payload(data)
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        return
    day = payload.get("day")
    reply: dict[str, Any] = {"request_id": request_id, "day": day if isinstance(day, int) else None}
    try:
        document = await gs.require_owned(identity, payload.get("game_id"))
        engine = gs.engine_of(document)
        if not isinstance(day, int):
            raise gs.GameError("INVALID_PLAN", "day must be a number")
        if engine.phase != "planning" or day != engine.day:
            raise gs.GameError("WRONG_DAY", f"It is day {engine.day}, not day {day}.")
        # The price never refuses the preview: it is judged inside the reply (price_error).
        priced = payload.get("price") is not None
        try:
            plan = DayPlan.model_validate(
                {
                    **{k: payload.get(k) for k in ("purchases", "recipe") if k in payload},
                    "price": payload["price"] if priced else engine.config.min_max_values.price.min,
                }
            )
        except ValidationError as exc:
            raise gs.GameError("INVALID_PLAN", gs.validation_message(exc)) from None
        try:
            reply.update(plan_preview(engine, plan, plan.price if priced else None))
        except PlanError as exc:
            raise gs.GameError("INVALID_PLAN", str(exc)) from None
    except gs.GameError as exc:
        reply = {
            "request_id": request_id,
            "day": reply["day"],
            "error": {"code": exc.code, "message": exc.message},
        }
    await socket_manager.emit_to_sid(sid, "plan_preview", reply)
