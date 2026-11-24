"""`GET /api/v1/games/id/{public_id}/results`: a finished game's results, open to anyone
holding its public id, naming no player."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import abuse
from app.db.session import SessionLocal
from app.main import app
from app.models.game import Game
from app.sockets.handlers import game

from ..conftest import new_guest
from .test_play import PLAN, config, connect

#: Keys that would name a player, or expose an internal id, the seed or a secret.
FORBIDDEN_KEYS = {
    "id",
    "game_id",
    "user_id",
    "uid",
    "firebase_uid",
    "email",
    "display_name",
    "name",
    "photo_url",
    "guest_identity",
    "identity",
    "session_token",
    "seed",
    "rng_seed",
    "engine",
    "config",
    "started_at",
}


def keys_of(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


async def finished(emits, sid: str, auth: dict[str, str]) -> str:
    await connect(sid, auth)
    await game.create_game(sid, {"config": config(2)})
    gid = emits.of("game_created")[-1]["game_id"]
    await game.submit_day(sid, {"game_id": gid, "day": 1, **PLAN})
    await game.ack_day(sid, {"game_id": gid, "day": 1})
    await game.submit_day(sid, {"game_id": gid, "day": 2, **PLAN, "purchases": {}})
    public_id: str = emits.of("game_persisted")[-1]["public_id"]
    return public_id


@pytest.mark.asyncio
async def test_results_open_without_auth_and_match_the_owner_view(
    emits, firebase_tokens
):
    firebase_tokens["tok"] = {
        "uid": "uid-results-7",
        "name": "Lemon Lover",
        "email": "ll@x.io",
    }
    public_id = await finished(emits, "s1", {"idToken": "tok"})
    client = TestClient(app)
    res = client.get(f"/api/v1/games/id/{public_id}/results")
    assert res.status_code == 200
    body = res.json()
    assert body["public_id"] == public_id and body["days_played"] == 2
    assert len(body["days"]) == 2 and body["days"][0]["by_hour"]
    assert body["finished_at"].endswith("Z")
    # The owner's detail, the same serialisation, minus the settings and the start time.
    owner = client.get(
        f"/api/v1/users/me/games/{public_id}", headers={"Authorization": "Bearer tok"}
    ).json()
    assert owner["config"] and owner["started_at"]
    assert body == {k: v for k, v in owner.items() if k not in {"config", "started_at"}}
    # A token sent along changes nothing.
    assert (
        client.get(
            f"/api/v1/games/id/{public_id}/results",
            headers={"Authorization": "Bearer tok"},
        ).json()
        == body
    )


@pytest.mark.asyncio
async def test_results_name_no_player(emits, firebase_tokens):
    firebase_tokens["tok"] = {
        "uid": "uid-results-8",
        "name": "Lemon Lover",
        "email": "ll@x.io",
    }
    signed_in = await finished(emits, "s1", {"idToken": "tok"})
    guest = new_guest()
    as_guest = await finished(emits, "g1", {"guestId": guest})
    client = TestClient(app)
    client.post(
        "/api/v1/users/upsert", json={}, headers={"Authorization": "Bearer tok"}
    )
    with SessionLocal() as db:
        rows = {g.public_id: g for g in db.query(Game).all()}
        secrets = {
            public_id: [str(g.rng_seed), str(g.user_id or ""), g.guest_identity or ""]
            for public_id, g in rows.items()
        }
    assert rows[as_guest].guest_identity == guest
    for public_id in (signed_in, as_guest):
        res = client.get(f"/api/v1/games/id/{public_id}/results")
        assert res.status_code == 200
        body = res.json()
        assert body["days"] and body["public_id"] == public_id
        assert not keys_of(body) & FORBIDDEN_KEYS
        text = json.dumps(body)
        for value in (
            "uid-results-8",
            "Lemon Lover",
            "ll@x.io",
            guest,
            "session_token",
        ):
            assert value not in text
        seed, _user_id, guest_identity = secrets[public_id]
        assert seed not in text
        if guest_identity:
            assert guest_identity not in text


def test_an_unknown_or_malformed_id_is_a_404() -> None:
    client = TestClient(app)
    for public_id in ("0" * 32, "f" * 32, "not-an-id", "A" * 32, "0" * 33):
        res = client.get(f"/api/v1/games/id/{public_id}/results")
        assert res.status_code == 404
        assert res.json() == {"detail": "Game not found."}


def test_results_count_against_the_heavy_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(abuse.heavy_limiter, "limit", 2)
    client = TestClient(app)
    sent = {"X-Forwarded-For": "1.1.1.1"}
    codes = [
        client.get(f"/api/v1/games/id/{'0' * 32}/results", headers=sent).status_code
        for _ in range(3)
    ]
    assert codes == [404, 404, 429]
    assert abuse.is_heavy_path(f"/api/v1/games/id/{'0' * 32}/results/")
    assert not abuse.is_heavy_path("/api/v1/games/claim")
    assert not abuse.is_heavy_path(f"/api/v1/users/me/games/{'0' * 32}")
