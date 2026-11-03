"""Every number a player configures has a hard bound, and a non-finite one is refused."""

import copy
import json
import math
from collections.abc import Iterator
from pathlib import Path as FilePath
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config_model import GameConfig
from app.core.defaults import default_config
from app.core.engine import DayPlan, LemonadeGame, PlanError
from app.core.limits import public_limits

Path = tuple[str | int, ...]


def number_paths(node: Any, path: Path = ()) -> Iterator[Path]:
    """The path of every int or float leaf (not bool) in a config document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from number_paths(value, (*path, key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from number_paths(value, (*path, i))
    elif isinstance(node, int | float) and not isinstance(node, bool):
        yield path


def with_value(path: Path, value: float) -> dict[str, Any]:
    data = copy.deepcopy(default_config())
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


PATHS = list(number_paths(default_config()))


def test_the_walk_reaches_every_kind_of_setting():
    roots = {path[0] for path in PATHS}
    assert roots == {
        "num_days",
        "starting_cash",
        "min_max_values",
        "quality_swing",
        "starting_popularity",
        "people_preferences",
        "weather_multipliers",
        "weather_temperature_ranges",
        "ingredients",
    }
    assert ("weather_temperature_ranges", "sunny", "max") in PATHS


@pytest.mark.parametrize("path", PATHS, ids=lambda p: ".".join(map(str, p)))
@pytest.mark.parametrize("value", [10**12, -(10**12)])
def test_no_setting_is_unbounded(path, value):
    with pytest.raises(ValidationError):
        GameConfig.model_validate(with_value(path, value))


@pytest.mark.parametrize("path", PATHS, ids=lambda p: ".".join(map(str, p)))
@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_no_setting_accepts_a_non_finite_number(path, value):
    with pytest.raises(ValidationError):
        GameConfig.model_validate(with_value(path, value))


def test_weather_temperature_ranges_stay_within_the_temperature_limits():
    data = default_config()
    data["weather_temperature_ranges"]["sunny"]["max"] = 61
    with pytest.raises(
        ValidationError, match="sunny temperature range must stay within -30..60 °C"
    ):
        GameConfig.model_validate(data)
    data["weather_temperature_ranges"]["sunny"]["max"] = 60
    assert GameConfig.model_validate(data).weather_temperature_ranges["sunny"].max == 60


# ---------------------------------------------------------------- served limits
def _set(data: dict[str, Any], path: Path, value: Any) -> None:
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _wide_open(data: dict[str, Any]) -> None:
    """Loosen the cross-field rules so a single field can reach its own bound."""
    data["min_max_values"]["price"] = {"min": 0, "max": 100}
    for prefs in data["people_preferences"].values():
        prefs["preferred_hour"] = 12


# (served limit, path in the config, step past the bound). Fields bounded by another
# field of the config (favourites, weather ranges) are covered by their own messages.
SERVED = [
    ("num_days", ("num_days",), 1),
    ("starting_cash", ("starting_cash",), 0.01),
    ("per_cup", ("min_max_values", "ice", "max"), 1),
    ("hour", ("min_max_values", "hour", "max"), 1),
    ("price", ("min_max_values", "price", "max"), 0.01),
    ("quality_swing", ("quality_swing",), 0.01),
    ("starting_popularity", ("starting_popularity",), 0.01),
    ("spawn_per_hour", ("people_preferences", "Child", "spawn_per_hour"), 0.5),
    ("average_expense", ("people_preferences", "Child", "average_expense"), 0.01),
    ("tolerance", ("people_preferences", "Child", "tolerances", "ice", "below"), 0.5),
    (
        "price_tolerance_below",
        ("people_preferences", "Child", "tolerances", "price", "below"),
        0.01,
    ),
    (
        "price_tolerance_above",
        ("people_preferences", "Child", "tolerances", "price", "above"),
        0.01,
    ),
    ("weather_multiplier", ("weather_multipliers", "sunny"), 0.05),
    ("unit_cost", ("ingredients", "sugar", "unit_cost"), 0.01),
    ("pack_size", ("ingredients", "sugar", "packs", 0, "size"), 1),
    ("pack_discount", ("ingredients", "sugar", "packs", 0, "discount"), 0.01),
    ("max_days", ("ingredients", "sugar", "max_days"), 1),
]


@pytest.mark.parametrize("key, path, step", SERVED, ids=[s[0] for s in SERVED])
def test_every_served_maximum_is_the_one_the_server_enforces(key, path, step):
    bound = public_limits()[key]
    assert isinstance(bound, dict)
    top = int(bound["max"]) if isinstance(step, int) else bound["max"]
    data = copy.deepcopy(default_config())
    _wide_open(data)
    _set(data, path, top)
    GameConfig.model_validate(data)
    _set(data, path, top + step)
    with pytest.raises(ValidationError):
        GameConfig.model_validate(data)


def test_the_temperature_and_fresh_day_limits_are_enforced_at_their_edges():
    limits = public_limits()
    temp, fresh = limits["temperature"], limits["fresh_days"]
    assert isinstance(temp, dict) and isinstance(fresh, dict)
    data = default_config()
    data["min_max_values"]["temperature"] = {"min": temp["min"], "max": temp["max"]}
    data["weather_temperature_ranges"]["snowy"]["min"] = temp["min"]
    data["weather_temperature_ranges"]["sunny"]["max"] = temp["max"]
    GameConfig.model_validate(data)
    data["weather_temperature_ranges"]["sunny"]["max"] = temp["max"] + 0.5
    with pytest.raises(ValidationError):
        GameConfig.model_validate(data)

    data = default_config()
    data["ingredients"]["sugar"].update(
        fresh_days=int(fresh["max"]), max_days=int(fresh["max"]) + 1
    )
    GameConfig.model_validate(data)
    data["ingredients"]["sugar"].update(
        fresh_days=int(fresh["max"]) + 1, max_days=int(fresh["max"]) + 1
    )
    with pytest.raises(ValidationError):
        GameConfig.model_validate(data)


def test_the_served_pack_limits_are_enforced():
    limits = public_limits()
    packs, per_size = limits["packs"], limits["packs_per_size"]
    assert isinstance(packs, dict) and isinstance(per_size, dict)
    data = default_config()
    data["ingredients"]["sugar"]["packs"] = [
        {"size": s, "discount": 0} for s in range(1, int(packs["max"]) + 1)
    ]
    GameConfig.model_validate(data)
    data["ingredients"]["sugar"]["packs"].append({"size": 999, "discount": 0})
    with pytest.raises(ValidationError):
        GameConfig.model_validate(data)

    data = default_config()
    data["starting_cash"] = 1_000_000
    game = LemonadeGame(GameConfig.model_validate(data), seed=1)
    recipe = {"ice": 1, "sugar": 1, "lemons": 2}
    top = int(per_size["max"])
    game.purchase_cost({"lemons": {"100": top}})
    with pytest.raises(PlanError):
        game.purchase_cost({"lemons": {"100": top + 1}})
    with pytest.raises(PlanError):
        game.purchase_cost({"lemons": {"100": int(per_size["min"]) - 1}})
    assert DayPlan(purchases={}, price=0.5, recipe=recipe)


# The frontend's tests render with the served limits; their copy must be what the server sends.
FIXTURE = FilePath(__file__).parent.parent / "fixtures" / "limits.json"
FRONTEND_COPY = (
    FilePath(__file__).parents[3]
    / "Lemony_Frontend"
    / "src"
    / "__tests__"
    / "fixtures"
    / "limits.json"
)


def test_the_limits_fixture_is_what_the_server_serves():
    assert json.loads(FIXTURE.read_text()) == public_limits()


@pytest.mark.skipif(
    not FRONTEND_COPY.exists(), reason="frontend repo not checked out next to the backend"
)
def test_the_limits_fixture_matches_the_frontend_copy():
    assert FRONTEND_COPY.read_bytes() == FIXTURE.read_bytes()
