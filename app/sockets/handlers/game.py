"""The play loop over Socket.IO.

| client -> server                               | server -> client                          |
|------------------------------------------------|-------------------------------------------|
| create_game {config, replace?}                 | game_created {game_id, session_token} + game_state |
| resume_game {game_id?, session_token?}         | game_state, or no_active_game             |
| request_state {game_id}                        | game_state                                |
| submit_day {game_id, day, purchases, price, recipe} | day_result (+ game_finished, game_persisted on the last day) |
| ack_day {game_id, day}                         | game_state                                |
| abandon_game {game_id}                         | game_abandoned                            |
| (any refusal)                                  | error {code, message}                     |

Every server event carries a monotonic per-game `seq`. `session_token` is sent
in exactly one event, `game_created`, to the creating sid.
"""

from __future__ import annotations

import logging
from typing import Any

from ...services import game_service as gs
from ..errors import guarded
from ..manager import sio, socket_manager

logger = logging.getLogger(__name__)


def _identity(sid: str) -> str | None:
    return socket_manager.sid_to_identity.get(sid)


async def _error(sid: str, code: str, message: str) -> None:
    await socket_manager.emit_to_sid(sid, "error", {"code": code, "message": message})


async def _send_state(sid: str, document: dict[str, Any]) -> None:
    seq = await gs.bump_seq(document)
    await socket_manager.emit_to_sid(sid, "game_state", {"seq": seq, "state": gs.public_state(document)})


async def _finish(sid: str, document: dict[str, Any]) -> None:
    seq = await gs.bump_seq(document)
    await socket_manager.emit_to_sid(
        sid, "game_finished", {"seq": seq, "summary": gs.summary_of(document)}
    )
    public_id = await gs.persist_if_finished(document)
    if public_id:
        seq = await gs.bump_seq(document)
        await socket_manager.emit_to_sid(sid, "game_persisted", {"seq": seq, "public_id": public_id})


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
        document = await gs.create_game(identity, payload.get("config"), bool(payload.get("replace")))
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
            "state": gs.public_state(document),
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
    await _send_state(sid, document)


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
