"""Game orchestration: the only code that mutates a live game document.

Every read-modify-write runs under the game's lock. Identity is the one
authority: a document is only ever touched by the identity that created it
(compared with `hmac.compare_digest`), and a presented `session_token` must
match too. Handlers never see engine internals — they get the public state.

Game document:
    schema_version, game_id, identity, session_token, engine (payload),
    seq, started_at, finished_at, persisted, public_id
"""

from __future__ import annotations

import hmac
import logging
import random
import uuid
from datetime import UTC, datetime
from typing import Any

import anyio.to_thread
from pydantic import ValidationError

from ..core.config_model import GameConfig
from ..core.engine import DayPlan, LemonadeGame, PlanError
from . import db_service
from .state_service import SCHEMA_VERSION, state_svc

logger = logging.getLogger(__name__)


class GameError(Exception):
    """A refusal with a code the client can branch on and a sentence it can show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validation_message(exc: ValidationError) -> str:
    """The first validation error as one readable sentence."""
    err = exc.errors()[0]
    msg = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
    loc = ".".join(str(part) for part in err.get("loc", ()) if part != "__root__")
    return f"{loc}: {msg}" if loc else msg


def parse_config(data: Any) -> GameConfig:
    try:
        return GameConfig.model_validate(data)
    except ValidationError as exc:
        raise GameError("INVALID_CONFIG", validation_message(exc)) from None


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def engine_of(document: dict[str, Any]) -> LemonadeGame:
    return LemonadeGame.from_payload(document["engine"])


def public_state(document: dict[str, Any]) -> dict[str, Any]:
    """What the owner's client renders. Never carries identity or session_token."""
    engine = engine_of(document)
    state = engine.view()
    state.update(
        game_id=document["game_id"],
        persisted=document.get("persisted", False),
        public_id=document.get("public_id"),
        summary=summary_of(document) if engine.phase == "finished" else None,
    )
    return state


def summary_of(document: dict[str, Any]) -> dict[str, Any]:
    summary = engine_of(document).summary()
    summary["public_id"] = document.get("public_id")
    summary["config"] = document["engine"]["config"]
    return summary


# --------------------------------------------------------------------- create
async def create_game(identity: str, config_data: Any, replace: bool = False) -> dict[str, Any]:
    config = parse_config(config_data)
    async with state_svc.identity_lock(identity):
        active_id = await state_svc.get_active_game_id(identity)
        if active_id:
            active = await state_svc.get_game(active_id)
            unfinished = active is not None and active["engine"]["phase"] != "finished"
            if unfinished and not replace:
                raise GameError(
                    "ACTIVE_GAME_EXISTS",
                    "You already have a game in progress. Resume it or start over.",
                )
            if unfinished:
                await state_svc.delete_game(active_id)
        game = LemonadeGame(config, seed=random.SystemRandom().randrange(2**53))
        document = {
            "schema_version": SCHEMA_VERSION,
            "game_id": uuid.uuid4().hex,
            "identity": identity,
            "session_token": uuid.uuid4().hex,
            "engine": game.to_payload(),
            "seq": 0,
            "started_at": _now(),
            "finished_at": None,
            "persisted": False,
            "public_id": None,
        }
        await state_svc.save_game(document)
        await state_svc.set_active_game(identity, document["game_id"])
    return document


# --------------------------------------------------------------------- lookup
async def load_owned(
    identity: str, game_id: str | None, session_token: str | None = None
) -> dict[str, Any] | None:
    """The identity's game (by id, or its active game), or None.

    A game owned by someone else reads as "no such game" — its existence is not
    disclosed. A presented session_token that does not match is refused the same way.
    """
    if not game_id:
        game_id = await state_svc.get_active_game_id(identity)
        if not game_id:
            return None
    if not isinstance(game_id, str) or len(game_id) > 64:
        return None
    document = await state_svc.get_game(game_id)
    if document is None or not _same(document.get("identity"), identity):
        return None
    if session_token is not None and not _same(document.get("session_token"), session_token):
        return None
    return document


async def require_owned(identity: str, game_id: Any) -> dict[str, Any]:
    document = await load_owned(identity, game_id if isinstance(game_id, str) else None)
    if document is None or not game_id:
        raise GameError("NO_GAME", "That game does not exist or is not yours.")
    return document


# ---------------------------------------------------------------------- play
async def submit_day(identity: str, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Run the day. Returns (document, result, duplicate).

    Idempotent: re-submitting a day that has already been played returns the
    stored result with `duplicate=True` and changes nothing.
    """
    game_id = data.get("game_id")
    day = data.get("day")
    async with state_svc.lock(str(game_id)):
        document = await require_owned(identity, game_id)
        engine = engine_of(document)
        if not isinstance(day, int):
            raise GameError("INVALID_PLAN", "day must be a number")
        if engine.phase != "planning" or day != engine.day:
            if engine.days and engine.days[-1]["day"] == day and engine.phase != "planning":
                stored = {"record": engine.days[-1], "events": engine.last_day_events}
                return document, stored, True
            raise GameError("WRONG_DAY", f"It is day {engine.day}, not day {day}.")
        try:
            plan = DayPlan.model_validate(
                {k: data.get(k) for k in ("purchases", "price", "recipe") if k in data}
            )
        except ValidationError as exc:
            raise GameError("INVALID_PLAN", validation_message(exc)) from None
        try:
            result = engine.run_day(plan)
        except PlanError as exc:
            raise GameError("INVALID_PLAN", str(exc)) from None
        document["engine"] = engine.to_payload()
        if engine.phase == "finished":
            document["finished_at"] = _now()
        await state_svc.save_game(document)
    return document, result, False


async def acknowledge_day(identity: str, game_id: Any, day: Any) -> dict[str, Any]:
    async with state_svc.lock(str(game_id)):
        document = await require_owned(identity, game_id)
        engine = engine_of(document)
        if isinstance(day, int) and engine.acknowledge_day(day):
            document["engine"] = engine.to_payload()
            await state_svc.save_game(document)
    return document


async def abandon_game(identity: str, game_id: Any) -> None:
    async with state_svc.lock(str(game_id)):
        document = await require_owned(identity, game_id)
        await state_svc.delete_game(document["game_id"])
    async with state_svc.identity_lock(identity):
        if await state_svc.get_active_game_id(identity) == document["game_id"]:
            await state_svc.clear_active_game(identity)


async def bump_seq(document: dict[str, Any]) -> int:
    """Allocate the next sequence number for an outgoing event, and save it."""
    async with state_svc.lock(document["game_id"]):
        fresh = await state_svc.get_game(document["game_id"]) or document
        seq = state_svc.next_seq(fresh)
        await state_svc.save_game(fresh)
    document["seq"] = seq
    return seq


# ------------------------------------------------------------------- persist
async def persist_if_finished(document: dict[str, Any]) -> str | None:
    """Write a finished, not-yet-persisted game to MySQL. Safe to call repeatedly."""
    if document["engine"]["phase"] != "finished" or document.get("persisted"):
        return document.get("public_id")
    summary = engine_of(document).summary()
    public_id = await anyio.to_thread.run_sync(db_service.persist_finished_game, document, summary)
    if public_id is None:
        return None
    async with state_svc.lock(document["game_id"]):
        fresh = await state_svc.get_game(document["game_id"])
        if fresh is not None:
            fresh["persisted"] = True
            fresh["public_id"] = public_id
            await state_svc.save_game(fresh)
    document["persisted"] = True
    document["public_id"] = public_id
    return public_id
