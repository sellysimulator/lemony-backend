from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.defaults import default_config
from app.main import app
from app.models.live_game import LiveGame
from app.services import state_service
from app.services.state_backend import SqlStateBackend
from app.sockets.handlers import connection, game
from app.sockets.manager import socket_manager

from ..conftest import new_guest

PLAN = {
    "purchases": {"ice": {"100": 1}, "sugar": {"100": 1}, "lemons": {"100": 1}, "cups": {"100": 1}},
    "price": 0.6,
    "recipe": {"ice": 2, "sugar": 2, "lemons": 3},
}


async def connect(sid: str, auth) -> str | None:
    accepted = await connection.connect(sid, {}, auth)
    return None if accepted is False else socket_manager.sid_to_identity[sid]


def config(days: int = 2) -> dict:
    c = default_config()
    c["num_days"] = days
    return c


# ----------------------------------------------------------- state backend
@pytest.mark.asyncio
async def test_sql_backend_roundtrip_and_expiry():
    from app.db.session import SessionLocal

    backend = SqlStateBackend()
    await backend.setex("k", 60, "v1")
    await backend.setex("k", 60, "v2")
    assert await backend.get("k") == "v2" and await backend.exists("k") == 1
    with SessionLocal() as db:
        db.get(LiveGame, "k").expires_at = datetime(2000, 1, 1)  # noqa: DTZ001 — column is naive UTC
        db.commit()
    assert await backend.get("k") is None
    await backend.setex("a", 60, "x")
    assert await backend.delete("a", "missing") == 1


@pytest.mark.asyncio
async def test_schema_guard_discards_stale_document():
    await state_service.redis_client.setex("game:old", 60, '{"schema_version": 0, "game_id": "old"}')
    assert await state_service.state_svc.get_game("old") is None
    assert await state_service.redis_client.get("game:old") is None


# ------------------------------------------------------------- handshake
@pytest.mark.asyncio
async def test_handshake_identities(firebase_tokens):
    firebase_tokens["good"] = {"uid": "uid-1"}
    guest = new_guest()
    assert await connect("s1", {"guestId": guest}) == guest
    assert await connect("s2", {"idToken": "good"}) == "uid-1"
    assert await connect("s3", {"idToken": "bad", "guestId": guest}) is None  # never downgrade
    assert (await connect("s4", {"guestId": "uid-1"})).startswith("guest_")  # can't claim a uid


# ------------------------------------------------------------ full game
@pytest.mark.asyncio
async def test_full_guest_game(emits):
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": config(2)})
    created = emits.of("game_created")[0]
    game_id = created["game_id"]
    assert "session_token" in created and "identity" not in emits.of("game_state")[0]["state"]

    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    first = emits.of("day_result")[0]
    assert first["day"] == 1 and not first["duplicate"] and first["state"]["phase"] == "played"

    # duplicate submit returns the stored result, changes nothing
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    dup = emits.of("day_result")[1]
    assert dup["duplicate"] and dup["record"] == first["record"]

    await game.ack_day("s1", {"game_id": game_id, "day": 1})
    assert emits.of("game_state")[-1]["state"]["phase"] == "planning"
    assert emits.of("game_state")[-1]["state"]["day"] == 2

    await game.submit_day("s1", {"game_id": game_id, "day": 2, **PLAN, "purchases": {}})
    assert emits.of("game_finished")[0]["summary"]["days_played"] == 2
    public_id = emits.of("game_persisted")[0]["public_id"]
    assert public_id == game_id

    seqs = [d["seq"] for _, _, d in emits.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_refresh_resumes_same_game_and_other_identity_cannot(emits):
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": config(3)})
    created = emits.of("game_created")[0]

    # page refresh: new sid, same guest id, resume by stored id + token
    await connect("s2", {"guestId": guest})
    await game.resume_game("s2", {"game_id": created["game_id"], "session_token": created["session_token"]})
    assert emits.of("game_state")[-1]["state"]["game_id"] == created["game_id"]

    # resume with nothing stored finds the active game by identity
    await game.resume_game("s2", {})
    assert emits.of("game_state")[-1]["state"]["game_id"] == created["game_id"]

    # a different identity holding the id and token gets nothing
    await connect("s3", {"guestId": new_guest()})
    await game.resume_game("s3", {"game_id": created["game_id"], "session_token": created["session_token"]})
    assert emits.of("no_active_game")
    await game.submit_day("s3", {"game_id": created["game_id"], "day": 1, **PLAN})
    assert emits.of("error")[-1]["code"] == "NO_GAME"


@pytest.mark.asyncio
async def test_active_game_blocks_new_unless_replace(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config()})
    await game.create_game("s1", {"config": config()})
    assert emits.of("error")[-1]["code"] == "ACTIVE_GAME_EXISTS"
    await game.create_game("s1", {"config": config(), "replace": True})
    assert len(emits.of("game_created")) == 2


@pytest.mark.asyncio
async def test_invalid_config_and_plan_refused(emits):
    await connect("s1", {"guestId": new_guest()})
    bad = config()
    bad["people_preferences"]["Adult"]["preferred_hour"] = 18
    await game.create_game("s1", {"config": bad})
    err = emits.of("error")[-1]
    assert err["code"] == "INVALID_CONFIG" and "preferred hour" in err["message"]

    await game.create_game("s1", {"config": config()})
    game_id = emits.of("game_created")[0]["game_id"]
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN, "price": 99})
    assert emits.of("error")[-1]["code"] == "INVALID_PLAN"
    await game.submit_day("s1", {"game_id": game_id, "day": 2, **PLAN})
    assert emits.of("error")[-1]["code"] == "WRONG_DAY"


# ---------------------------------------------------------- profile/REST
@pytest.mark.asyncio
async def test_signed_in_game_reaches_profile_and_guest_claim(emits, firebase_tokens):
    firebase_tokens["tok"] = {"uid": "uid-9", "name": "Lemon Lover"}
    await connect("s1", {"idToken": "tok"})
    await game.create_game("s1", {"config": config(1)})
    gid = emits.of("game_created")[0]["game_id"]
    await game.submit_day("s1", {"game_id": gid, "day": 1, **PLAN})
    assert emits.of("game_persisted")

    guest = new_guest()
    await connect("g1", {"guestId": guest})
    await game.create_game("g1", {"config": config(1)})
    ggid = emits.of("game_created")[-1]["game_id"]
    await game.submit_day("g1", {"game_id": ggid, "day": 1, **PLAN})

    client = TestClient(app)
    auth = {"Authorization": "Bearer tok"}
    assert client.post("/api/v1/users/upsert", json={}, headers=auth).json()["display_name"] == "Lemon Lover"
    assert client.get("/api/v1/users/me/games", headers=auth).json()["total"] == 1
    assert client.post("/api/v1/games/claim", json={"guest_identity": guest}, headers=auth).json()["claimed"] == 1
    listing = client.get("/api/v1/users/me/games", headers=auth).json()
    assert listing["total"] == 2
    stats = client.get("/api/v1/users/me/stats", headers=auth).json()
    assert stats["games_played"] == 2
    detail = client.get(f"/api/v1/users/me/games/{gid}", headers=auth).json()
    assert detail["days"][0]["by_hour"] and detail["config"]["num_days"] == 1
    assert client.get("/api/v1/users/me/games", headers={"Authorization": "Bearer nope"}).status_code in (401, 503)


def test_config_routes():
    client = TestClient(app)
    defaults = client.get("/api/v1/config/defaults").json()
    assert defaults["person_types"] == ["Child", "Teenager", "Adult", "Senior"]
    assert client.post("/api/v1/config/validate", json=defaults["config"]).json() == {"valid": True, "message": None}
    defaults["config"]["weather_temperature_ranges"]["rainy"]["min"] = 8
    res = client.post("/api/v1/config/validate", json=defaults["config"]).json()
    assert not res["valid"] and "gap" in res["message"]
    assert client.get("/api/v1/health").json() == {"status": "ok"}
