import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config_model import GameConfig
from app.core.config_preview import config_help, ingredient_preview
from app.core.defaults import INGREDIENT_NAMES, WEATHER_TYPES, default_config
from app.core.demand import max_expected_customers
from app.core.limits import MAX_CUSTOMERS_PER_DAY, public_limits
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


def crowded(days: int = 2) -> dict:
    """Valid in every rule but the cap on customers a day: ~5,800 on the busiest day."""
    c = config(days)
    c["min_max_values"]["hour"] = {"min": 0, "max": 23}
    for prefs in c["people_preferences"].values():
        prefs["spawn_per_hour"] = 40
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
        expired = datetime(2000, 1, 1)  # noqa: DTZ001 — column is naive UTC
        db.get(LiveGame, "k").expires_at = expired
        db.commit()
    assert await backend.get("k") is None
    await backend.setex("a", 60, "x")
    assert await backend.delete("a", "missing") == 1


@pytest.mark.asyncio
async def test_schema_guard_discards_stale_document():
    await state_service.redis_client.setex(
        "game:old", 60, '{"schema_version": 0, "game_id": "old"}'
    )
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

    # duplicate submit returns the stored record, changes nothing; its customers are not kept
    await game.submit_day("s1", {"game_id": game_id, "day": 1, **PLAN})
    dup = emits.of("day_result")[1]
    assert dup["duplicate"] and dup["record"] == first["record"] and dup["events"] is None

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
    await game.resume_game(
        "s2", {"game_id": created["game_id"], "session_token": created["session_token"]}
    )
    assert emits.of("game_state")[-1]["state"]["game_id"] == created["game_id"]

    # resume with nothing stored finds the active game by identity
    await game.resume_game("s2", {})
    assert emits.of("game_state")[-1]["state"]["game_id"] == created["game_id"]

    # a different identity holding the id and token gets nothing
    await connect("s3", {"guestId": new_guest()})
    await game.resume_game(
        "s3", {"game_id": created["game_id"], "session_token": created["session_token"]}
    )
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, value, message",
    [
        (("starting_cash",), None, "starting_cash: Input should be a valid number"),
        (("ingredients", "lemons", "unit_cost"), None, "ingredients.lemons.unit_cost"),
        (("starting_cash",), 1_000_001, "starting_cash: Input should be less than or equal to"),
        (("weather_temperature_ranges", "sunny", "max"), 61, "sunny temperature range"),
    ],
)
async def test_a_config_with_a_cleared_or_out_of_range_value_is_refused(
    emits, path, value, message
):
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    bad = config()
    node = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    await game.create_game("s1", {"config": bad})
    err = emits.of("error")[-1]
    assert err["code"] == "INVALID_CONFIG" and message in err["message"]
    assert not emits.of("game_created") and not emits.of("game_state")
    assert await state_service.state_svc.get_active_game_id(guest) is None


@pytest.mark.asyncio
async def test_create_game_refuses_a_config_over_the_customers_cap(emits):
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": crowded()})
    err = emits.of("error")[-1]
    assert err["code"] == "INVALID_CONFIG" and "over the limit of 5,000 a day" in err["message"]
    assert not emits.of("game_created")
    assert await state_service.state_svc.get_active_game_id(guest) is None


@pytest.mark.asyncio
async def test_a_live_game_stored_over_the_customers_cap_still_loads_and_plays(emits):
    """The cap applies when a config is created or changed, never to a stored game."""
    guest = new_guest()
    await connect("s1", {"guestId": guest})
    await game.create_game("s1", {"config": config(2)})
    created = emits.of("game_created")[0]
    game_id = created["game_id"]
    # A game created before the cap: its stored config is over it.
    document = await state_service.state_svc.get_game(game_id)
    assert document is not None
    document["engine"]["config"] = crowded(2)
    document["engine"]["config"]["weather_multipliers"] = {w: 3.0 for w in WEATHER_TYPES}
    await state_service.state_svc.save_game(document)

    await connect("s2", {"guestId": guest})
    await game.resume_game("s2", {"game_id": game_id, "session_token": created["session_token"]})
    state = emits.of("game_state")[-1]["state"]
    assert state["game_id"] == game_id and state["config"]["weather_multipliers"]["snowy"] == 3
    await game.submit_day("s2", {"game_id": game_id, "day": 1, **PLAN})
    result = emits.of("day_result")[-1]
    assert not emits.of("error") and result["record"]["visitors"] > 0
    await game.ack_day("s2", {"game_id": game_id, "day": 1})
    await game.submit_day("s2", {"game_id": game_id, "day": 2, **PLAN, "purchases": {}})
    assert emits.of("game_finished")[0]["summary"]["days_played"] == 2
    assert emits.of("game_persisted")[0]["public_id"] == game_id


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
    assert (
        client.post("/api/v1/users/upsert", json={}, headers=auth).json()["display_name"]
        == "Lemon Lover"
    )
    assert client.get("/api/v1/users/me/games", headers=auth).json()["total"] == 1
    assert (
        client.post("/api/v1/games/claim", json={"guest_identity": guest}, headers=auth).json()[
            "claimed"
        ]
        == 1
    )
    listing = client.get("/api/v1/users/me/games", headers=auth).json()
    assert listing["total"] == 2
    for item in listing["items"]:
        assert item["conversion"] == round(item["total_buyers"] / item["total_visitors"], 4)
    stats = client.get("/api/v1/users/me/stats", headers=auth).json()
    assert stats["games_played"] == 2
    assert stats["conversion"] == round(stats["total_cups_sold"] / stats["total_visitors"], 4)
    detail = client.get(f"/api/v1/users/me/games/{gid}", headers=auth).json()
    assert detail["days"][0]["by_hour"] and detail["config"]["num_days"] == 1
    day = detail["days"][0]
    assert day["popularity"] == 0.5
    assert detail["final_popularity"] == pytest.approx(round(day["buyers"] / day["visitors"], 4))
    assert detail["final_popularity"] in {item["final_popularity"] for item in listing["items"]}
    assert client.get(
        "/api/v1/users/me/games", headers={"Authorization": "Bearer nope"}
    ).status_code in (401, 503)


def test_config_routes():
    client = TestClient(app)
    defaults = client.get("/api/v1/config/defaults").json()
    assert defaults["person_types"] == ["Child", "Teenager", "Adult", "Senior"]
    assert defaults["limits"] == public_limits() and "max_num_days" not in defaults
    assert defaults["limits"]["num_days"] == {"min": 1, "max": 30}
    assert defaults["limits"]["packs_per_size"] == {"min": 0, "max": 100}
    assert client.post("/api/v1/config/validate", json=defaults["config"]).json() == {
        "valid": True,
        "message": None,
    }
    defaults["config"]["weather_temperature_ranges"]["rainy"]["min"] = 8
    res = client.post("/api/v1/config/validate", json=defaults["config"]).json()
    assert not res["valid"] and "gap" in res["message"]
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_config_preview_route_gives_the_verdict_and_every_figure():
    client = TestClient(app)
    data = default_config()
    res = client.post("/api/v1/config/preview", json=data).json()
    assert res["valid"] is True and res["message"] is None
    parsed = GameConfig.model_validate(data)
    assert res["customers_per_day"] == max_expected_customers(parsed) == 228
    assert res["help"] == config_help(parsed)
    assert res["ingredients"] == {
        name: ingredient_preview(name, parsed.ingredients[name]) for name in INGREDIENT_NAMES
    }


def test_an_invalid_config_keeps_each_valid_ingredients_figures_and_no_help():
    client = TestClient(app)
    data = default_config()
    data["weather_temperature_ranges"]["rainy"]["min"] = 8  # a gap
    data["ingredients"]["sugar"]["fresh_days"] = 20  # not under its max days
    res = client.post("/api/v1/config/preview", json=data).json()
    verdict = client.post("/api/v1/config/validate", json=data).json()
    assert res["valid"] is False and res["message"] == verdict["message"]
    assert res["help"] is None and res["customers_per_day"] is None
    assert res["ingredients"]["sugar"] is None
    lemons = GameConfig.model_validate(default_config()).ingredients["lemons"]
    assert res["ingredients"]["lemons"] == ingredient_preview("lemons", lemons)


def test_a_config_over_the_customers_cap_is_refused_with_its_figure():
    client = TestClient(app)
    data = crowded()
    figure = max_expected_customers(GameConfig.validate_uncapped(data))
    assert figure > MAX_CUSTOMERS_PER_DAY
    res = client.post("/api/v1/config/preview", json=data).json()
    verdict = client.post("/api/v1/config/validate", json=data).json()
    assert res["valid"] is False and res["message"] == verdict["message"]
    assert res["message"].startswith(f"Up to {figure:,} customers could come on the busiest day")
    assert "limit of 5,000 a day" in res["message"]
    # The figure comes with the refusal, so the screen shows how far over it is.
    assert res["customers_per_day"] == figure and res["help"] is None
    assert all(res["ingredients"][name] is not None for name in INGREDIENT_NAMES)

    # Lowered to the cap's side, the same traffic fields pass.
    for prefs in data["people_preferences"].values():
        prefs["spawn_per_hour"] = 30
    res = client.post("/api/v1/config/preview", json=data).json()
    assert res["valid"] is True and 0 < res["customers_per_day"] <= MAX_CUSTOMERS_PER_DAY


@pytest.mark.parametrize("body", [{}, {"ingredients": 5}, {"ingredients": {"ice": "cold"}}])
def test_config_preview_route_answers_any_object(body):
    res = TestClient(app).post("/api/v1/config/preview", json=body)
    assert res.status_code == 200
    assert res.json()["valid"] is False
    assert res.json()["ingredients"] == {name: None for name in INGREDIENT_NAMES}


# The frontend's tests render the help from this recorded reply: it must be what the server
# answers today. Regenerate it from the defaults when a figure deliberately changes.
PREVIEW_FIXTURE = Path(__file__).parent.parent / "fixtures" / "config_preview.json"
PREVIEW_FRONTEND_COPY = (
    Path(__file__).parents[3]
    / "Lemony_Frontend"
    / "src"
    / "__tests__"
    / "fixtures"
    / "config_preview.json"
)


def test_the_config_preview_fixture_is_what_the_server_answers():
    recorded = json.loads(PREVIEW_FIXTURE.read_text())
    assert recorded["config"] == default_config()
    res = TestClient(app).post("/api/v1/config/preview", json=recorded["config"]).json()
    assert res == recorded["reply"]


@pytest.mark.skipif(
    not PREVIEW_FRONTEND_COPY.exists(), reason="frontend repo not checked out next to the backend"
)
def test_the_config_preview_fixture_matches_the_frontend_copy():
    assert PREVIEW_FRONTEND_COPY.read_bytes() == PREVIEW_FIXTURE.read_bytes()


def test_cup_cost_route_gives_the_engines_day_one_figures():
    client = TestClient(app)
    recipe = {"ice": 1, "sugar": 1, "lemons": 2}
    small = {name: {"100": 1} for name in ("ice", "sugar", "lemons", "cups")}
    body = {"config": default_config(), "purchases": small, "recipe": recipe}
    res = client.post("/api/v1/config/cup-cost", json=body).json()
    # 100 lemons make 50 cups at list price: 0.01 + 0.02 + 2 x 0.05 + 0.04.
    assert res == {"spend": 12.0, "cups_possible": 50, "cost_per_cup": 0.17}

    big = {name: {"700": 2} for name in ("ice", "sugar", "lemons", "cups")}
    res = client.post("/api/v1/config/cup-cost", json={**body, "purchases": big}).json()
    assert res["cups_possible"] == 700 and res["cost_per_cup"] == 0.14  # 20% off
    # 1400 x (0.01 + 0.02 + 0.05 + 0.04) x 0.8: more than the 100 starting cash, still answered.
    assert res["spend"] == 134.4

    res = client.post("/api/v1/config/cup-cost", json={**body, "purchases": {}}).json()
    assert res == {"spend": 0.0, "cups_possible": 0, "cost_per_cup": None}


@pytest.mark.parametrize(
    "change, message",
    [
        ({"purchases": {"lemons": {"100": -1}}}, "You can buy 0 to 100 packs"),
        ({"purchases": {"lemons": {"7": 1}}}, "not sold in packs of 7"),
        ({"recipe": {"ice": -1, "sugar": 1, "lemons": 2}}, "ice per cup must be between"),
        ({"config": {**default_config(), "num_days": 31}}, "num_days"),
    ],
)
def test_cup_cost_route_refuses_bad_input_with_the_games_message(change, message):
    body = {
        "config": default_config(),
        "purchases": {},
        "recipe": {"ice": 1, "sugar": 1, "lemons": 2},
    }
    res = TestClient(app).post("/api/v1/config/cup-cost", json={**body, **change})
    assert res.status_code == 422 and message in res.json()["detail"]


@pytest.mark.asyncio
async def test_the_game_state_carries_the_limits(emits):
    await connect("s1", {"guestId": new_guest()})
    await game.create_game("s1", {"config": config()})
    assert emits.of("game_state")[-1]["state"]["limits"] == public_limits()
