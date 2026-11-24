"""The play loop over Socket.IO.

| client -> server                               | server -> client                          |
|------------------------------------------------|-------------------------------------------|
| create_game {config, replace?}                 | game_created {game_id, session_token} + game_state (full) |
| resume_game {game_id?, session_token?}         | game_state (full), or no_active_game      |
| request_state {game_id}                        | game_state (full)                         |
| submit_day {game_id, day, purchases, price, recipe} | day_result {day, record, events, duplicate, state (daily)} (+ game_finished, then game_persisted or game_persist_failed, on the last day); `events` is null on a duplicate |
| ack_day {game_id, day}                         | game_state (daily)                        |
| retry_persist {game_id}                        | game_persisted {public_id}, or game_persist_failed |
| abandon_game {game_id}                         | game_abandoned                            |
| preview_plan {request_id, game_id, day, purchases, recipe, price?} | plan_preview {request_id, day, prices, chance, expected_visitors, expected_cups, expected_margin, cups_possible, cost_per_cup, spend, cash_after, units, can_add, and with a price: price, price_error, at_price}, or {request_id, day, error: {code, message}}; no `seq`, changes no state; at most 5 per second per sid, the excess gets no reply |
| (any refusal)                                  | error {code, message}                     |

`game_state` is `{seq, full, state}`. A full state carries the history (`days`,
and `summary` once finished); a daily state leaves those out so a day's events do
not grow with the game. The client appends `day_result.record` to the days it holds
(only when its day is later than the last one held) and takes the summary from
`game_finished`. Every resync is full.

The day's customers (`events`, one per visitor, in arrival order) are sent once, in
the `day_result` of the submission that played the day, and never stored: no state
carries them (`last_day_events` is gone), and a duplicate `day_result` (a day sent
again) has `events: null`, meaning "not available". The client then skips the replay
and opens that day's report; a resume while a day is `played` but not acknowledged
does the same.

Every server event carries a monotonic per-game `seq`, except `plan_preview`, a
request-scoped reply that is not a state event. A game deleted while a request was
being answered (abandoned, or replaced by a new game, from another tab) gets no more
events: `bump_seq` returns None (and never saves it back) and the handler stops there.
So a game abandoned between its last day and `game_finished` is not saved either.
`session_token` is sent in exactly one event, `game_created`, to the creating sid.

`plan_preview` carries every figure the plan screen shows (see `core/preview.py`): the
spend, the cash after it, the units bought, which packs can still be added (`can_add`,
per ingredient and size), and, for the price sent, the server's verdict on it
(`price_error`, null when it is fine) and the curve at exactly that price with the
margin per cup (`at_price`, null when the price is refused). Without a `price` those
three keys are absent.

Saving a finished game: `game_persisted` when it is stored, `game_persist_failed`
when the write failed (the client offers a retry, `retry_persist`). A failed write is
also retried in the background, `PERSIST_RETRY_DELAYS` (2 s, 10 s, 30 s) apart, with
no lock held while it waits or writes; the first success sends `game_persisted`, and
if all fail `game_persist_failed` is sent again. Both go to the sid that last asked
about the game (a reconnect's `resume_game` or `request_state` takes them over). One
save is in flight per game, from before its first write until its last retry is done:
meanwhile `resume_game` and `request_state` leave the save to it (only `retry_persist`
writes again); otherwise they retry a finished, unsaved game's save themselves. The
game leaves the in-flight set before the final answer, so a rejoin during that answer
starts a new save, and a retry by hand that fails then gets background retries too.
Every write is idempotent on the game's `public_id`, so the game is stored once.
`create_game` retries the save of a finished, unsaved active game first and refuses
with `UNSAVED_GAME` if it still fails, so no finished game is left behind unsaved.

Rate limits (`core/abuse.py`, applied by `guarded()`): past 20 events a second a
sid's events are dropped unanswered, and so are its `preview_plan` requests past 5 a
second; past 60 `create_game` a minute from one address, `create_game` is answered
with `error {code: "RATE_LIMITED", message}`.
"""  # noqa: E501 — the event table's rows cannot wrap

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import ValidationError

from ...core.abuse import create_game_limiter, preview_limiter
from ...core.engine import DayPlan, PlanError
from ...core.preview import plan_preview
from ...services import game_service as gs
from ..errors import guarded
from ..manager import sio, socket_manager

logger = logging.getLogger(__name__)


def _identity(sid: str) -> str | None:
    return socket_manager.sid_to_identity.get(sid)


async def _error(sid: str, code: str, message: str) -> None:
    await socket_manager.emit_to_sid(sid, "error", {"code": code, "message": message})


async def _send_state(sid: str, document: dict[str, Any], full: bool = True) -> None:
    seq = await gs.bump_seq(document)
    if seq is None:
        return
    await socket_manager.emit_to_sid(
        sid,
        "game_state",
        {"seq": seq, "full": full, "state": gs.public_state(document, full)},
    )


# Seconds before each background retry of a failed save (after the first attempt).
PERSIST_RETRY_DELAYS: tuple[float, ...] = (2.0, 10.0, 30.0)
# Games with a save in flight: from before the first write until the last retry is done,
# and no longer during the final answer (so a rejoin meanwhile can start a new save).
_persisting: set[str] = set()
# The background retry of each game whose save failed (a strong reference, so the task
# is not collected mid-flight), and the sid its answer goes to: the latest one to ask.
_persist_retries: dict[str, asyncio.Task[None]] = {}
_persist_sids: dict[str, str] = {}


def persist_retry_in_flight(game_id: str) -> bool:
    return game_id in _persisting


async def _say_persisted(
    sid: str, document: dict[str, Any], public_id: str | None
) -> None:
    """`game_persisted` or `game_persist_failed`; nothing for a game that is gone."""
    seq = await gs.bump_seq(document)
    if seq is None:
        return
    if public_id:
        await socket_manager.emit_to_sid(
            sid, "game_persisted", {"seq": seq, "public_id": public_id}
        )
    else:
        await socket_manager.emit_to_sid(sid, "game_persist_failed", {"seq": seq})


async def _persist(sid: str, document: dict[str, Any], by_hand: bool = False) -> None:
    """Save a finished game and say how it went: `game_persisted` or
    `game_persist_failed`.

    While a save is in flight only a retry by hand writes again; anyone else just takes
    over its answer. A failed write starts the background retry unless one is running.
    """
    game_id = document["game_id"]
    _persist_sids[game_id] = sid
    owner = game_id not in _persisting
    if not owner and not by_hand:
        return
    _persisting.add(game_id)
    public_id: str | None = None
    try:
        public_id = await gs.persist_if_finished(document)
    finally:
        if owner:
            _persisting.discard(game_id)
        if public_id is None and game_id not in _persisting:
            _retry_persist_later(document)
    if game_id in _persisting:
        await _say_persisted(_persist_sids.get(game_id, sid), document, public_id)
    else:
        await _say_persisted(_persist_sids.pop(game_id, sid), document, public_id)


def _retry_persist_later(document: dict[str, Any]) -> None:
    game_id = document["game_id"]
    if not PERSIST_RETRY_DELAYS:
        return
    _persisting.add(game_id)
    identity = document["identity"]
    task = asyncio.get_running_loop().create_task(
        _persist_in_background(identity, game_id)
    )
    _persist_retries[game_id] = task


async def _persist_in_background(identity: str, game_id: str) -> None:
    """Retry a failed save after each of `PERSIST_RETRY_DELAYS`, holding no lock
    meanwhile.

    The game leaves the in-flight set before the final answer, so a rejoin (or a retry
    by hand that fails) while it is being sent starts a new save.
    """
    try:
        document: dict[str, Any] | None = None
        public_id: str | None = None
        try:
            for delay in PERSIST_RETRY_DELAYS:
                await asyncio.sleep(delay)
                document = await gs.load_owned(identity, game_id)
                if document is None or document.get("persisted"):
                    return  # gone, or saved meanwhile (and that save was answered)
                public_id = await gs.persist_if_finished(document)
                if public_id:
                    break
        finally:
            _persisting.discard(game_id)
            _persist_retries.pop(game_id, None)
            sid = _persist_sids.pop(game_id, "")
        if document is not None:
            await _say_persisted(sid, document, public_id)
    except Exception:  # pragma: no cover - logged; the client can still retry by hand
        logger.exception("Background save of a finished game failed.")


async def _resave_if_unsaved(sid: str, document: dict[str, Any], finish: bool) -> None:
    """Retry the save of a finished, unsaved game someone (re)joined, unless a save is
    in flight: then its answer goes to this sid."""
    if document["engine"]["phase"] != "finished" or document.get("persisted"):
        return
    if persist_retry_in_flight(document["game_id"]):
        _persist_sids[document["game_id"]] = sid
    elif finish:
        await _finish(sid, document)
    else:
        await _persist(sid, document)


async def _finish(sid: str, document: dict[str, Any]) -> None:
    seq = await gs.bump_seq(document)
    if seq is None:
        return
    await socket_manager.emit_to_sid(
        sid, "game_finished", {"seq": seq, "summary": gs.summary_of(document)}
    )
    await _persist(sid, document)


def _payload(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


@sio.event
@guarded(per_ip=create_game_limiter)
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
    if seq is None:
        return
    await socket_manager.emit_to_sid(
        sid,
        "game_created",
        {
            "seq": seq,
            "game_id": document["game_id"],
            "session_token": document["session_token"],
        },
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
    # Retry a save that failed earlier
    await _resave_if_unsaved(sid, document, finish=True)


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
    await _resave_if_unsaved(sid, document, finish=False)


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
    if seq is None:
        return
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
        document = await gs.acknowledge_day(
            identity, payload.get("game_id"), payload.get("day")
        )
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
    await _persist(sid, document, by_hand=True)


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
    await socket_manager.emit_to_sid(
        sid, "game_abandoned", {"seq": 0, "game_id": game_id}
    )


@sio.event
@guarded(per_sid=preview_limiter)
async def preview_plan(sid: str, data: Any = None) -> None:
    # At most PREVIEW_LIMIT (5) a second per sid, counted on arrival (refused ones too);
    # `guarded` drops the excess unanswered.
    identity = _identity(sid)
    if identity is None:
        return
    payload = _payload(data)
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        return
    day = payload.get("day")
    reply: dict[str, Any] = {
        "request_id": request_id,
        "day": day if isinstance(day, int) else None,
    }
    try:
        document = await gs.require_owned(identity, payload.get("game_id"))
        engine = gs.engine_of(document)
        if not isinstance(day, int):
            raise gs.GameError("INVALID_PLAN", "day must be a number")
        if engine.phase != "planning" or day != engine.day:
            raise gs.GameError("WRONG_DAY", f"It is day {engine.day}, not day {day}.")
        # The price never refuses the preview: it is judged inside the reply
        # (price_error).
        priced = payload.get("price") is not None
        try:
            plan = DayPlan.model_validate(
                {
                    **{
                        k: payload.get(k)
                        for k in ("purchases", "recipe")
                        if k in payload
                    },
                    "price": (
                        payload["price"]
                        if priced
                        else engine.config.min_max_values.price.min
                    ),
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
