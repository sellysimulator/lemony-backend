"""The cap on customers a day: the busiest day a config allows is refused over the limit.

Every traffic limit is fine alone (spawn per hour, opening hours, weather multipliers), but
together they made one day ~83,000 customers. ``demand.max_expected_customers`` is the
figure the config is judged by and the config screen shows; it is checked here against a
brute force over every day the engine can deal.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config_model import GameConfig
from app.core.defaults import PERSON_TYPES, WEATHER_TYPES, default_config
from app.core.demand import expected_spawn, max_expected_customers
from app.core.engine import DayPlan, LemonadeGame
from app.core.limits import (
    MAX_CUSTOMERS_PER_DAY,
    MAX_HOUR,
    MAX_SPAWN_PER_HOUR,
    MAX_WEATHER_MULTIPLIER,
    public_limits,
)
from app.core.people import build_people
from app.core.weather import roll_day_weather, temperatures_by_weather, weather_odds, weathers_at

PLAN = {"purchases": {}, "price": 0.5, "recipe": {"ice": 1, "sugar": 1, "lemons": 2}}


def crowd(spawn: float) -> dict[str, Any]:
    """The defaults open all day, every type at ``spawn`` visitors an hour."""
    data = default_config()
    data["min_max_values"]["hour"] = {"min": 0, "max": MAX_HOUR}
    for prefs in data["people_preferences"].values():
        prefs["spawn_per_hour"] = spawn
    return data


def every_limit() -> dict[str, Any]:
    """Every traffic field at its own limit: the config that made an 83,000-customer day."""
    data = crowd(MAX_SPAWN_PER_HOUR)
    data["weather_multipliers"] = {w: MAX_WEATHER_MULTIPLIER for w in WEATHER_TYPES}
    return data


def worst_day(config: GameConfig) -> float:
    """The exact worst day, by brute force: every (weather, temperature) the roll can deal,
    and the expected arrivals ``run_day`` draws for it, summed over types and hours."""
    people = build_people(config)
    hours = range(config.min_max_values.hour.min, config.min_max_values.hour.max + 1)
    return max(
        sum(
            expected_spawn(p, h, w, t, config.weather_multipliers[w]) for p in people for h in hours
        )
        for w, temperatures in temperatures_by_weather(config).items()
        for t in temperatures
    )


def random_config(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    data = default_config()
    lo = rng.randint(0, 10)
    data["min_max_values"]["hour"] = {"min": lo, "max": rng.randint(lo + 1, MAX_HOUR)}
    hours = data["min_max_values"]["hour"]
    for prefs in data["people_preferences"].values():
        prefs["spawn_per_hour"] = rng.choice([0, 0.5, rng.uniform(0, 60)])
        prefs["preferred_hour"] = rng.randint(hours["min"], hours["max"])
        prefs["preferred_degrees"] = rng.randint(0, 80) / 2
        prefs["preferred_weather"] = rng.choice(WEATHER_TYPES)
    data["weather_multipliers"] = {w: rng.uniform(0, MAX_WEATHER_MULTIPLIER) for w in WEATHER_TYPES}
    if rng.random() < 0.5:  # overlapping ranges, one weather never dealt
        data["weather_temperature_ranges"] = {
            "snowy": {"min": 50, "max": 60},
            "rainy": {"min": 0, "max": 22.5},
            "cloudy": {"min": 10, "max": 30},
            "sunny": {"min": 20.3, "max": 40},
        }
    return data


# ------------------------------------------------------------------ the figure
def test_the_defaults_expect_at_most_228_customers_a_day_far_under_the_cap():
    config = GameConfig.model_validate(default_config())
    assert max_expected_customers(config) == 228
    assert 228 * 20 < MAX_CUSTOMERS_PER_DAY
    assert worst_day(config) == pytest.approx(225.05, abs=0.01)


@pytest.mark.parametrize("seed", range(40))
def test_the_figure_never_undercounts_the_worst_day_and_is_close_to_it(seed):
    config = GameConfig.validate_uncapped(random_config(seed))
    exact = worst_day(config)
    figure = max_expected_customers(config)
    assert exact <= figure + 1e-9
    # Each type at its best temperature adds at most a third (the temperature's share of
    # the bonus) to the exact worst day.
    assert figure <= math.ceil(exact * 4 / 3) + 1


def test_the_figure_is_exact_when_every_type_likes_the_same_temperature():
    data = every_limit()
    for prefs in data["people_preferences"].values():
        prefs["preferred_degrees"] = 27.5
    config = GameConfig.validate_uncapped(data)
    assert max_expected_customers(config) == math.ceil(worst_day(config) - 1e-9)


def test_a_weather_the_config_never_deals_does_not_count():
    data = crowd(30)
    data["weather_multipliers"]["snowy"] = MAX_WEATHER_MULTIPLIER
    data["weather_temperature_ranges"]["snowy"] = {"min": 50, "max": 60}
    data["weather_temperature_ranges"]["rainy"]["min"] = 0
    config = GameConfig.validate_uncapped(data)
    assert temperatures_by_weather(config)["snowy"] == []
    data["weather_multipliers"]["snowy"] = 0
    assert max_expected_customers(config) == max_expected_customers(
        GameConfig.validate_uncapped(data)
    )


def test_temperatures_by_weather_are_the_days_the_roll_deals():
    config = GameConfig.validate_uncapped(random_config(3))
    listed = temperatures_by_weather(config)
    odds = weather_odds(config)
    for w in WEATHER_TYPES:
        assert bool(listed[w]) == (odds[w] > 0)
        assert all(w in weathers_at(config, t) for t in listed[w])
    for day in range(500):
        weather, temperature = roll_day_weather(config, random.Random(f"x:{day}:weather"))
        assert temperature in listed[weather]


# --------------------------------------------------------------------- the cap
def test_the_served_cap_is_the_constant():
    assert public_limits()["customers_per_day"] == {"min": 0, "max": MAX_CUSTOMERS_PER_DAY}
    assert MAX_CUSTOMERS_PER_DAY == 5000


def test_a_config_just_under_the_cap_is_accepted_and_just_over_refused():
    def figure(spawn: float) -> int:
        return max_expected_customers(GameConfig.validate_uncapped(crowd(spawn)))

    cap = public_limits()["customers_per_day"]
    assert isinstance(cap, dict)
    under, over = 0.0, float(MAX_SPAWN_PER_HOUR)
    for _ in range(80):
        mid = (under + over) / 2
        under, over = (mid, over) if figure(mid) <= cap["max"] else (under, mid)
    assert figure(under) == 5000 and figure(over) == 5001

    assert max_expected_customers(GameConfig.model_validate(crowd(under))) == 5000
    with pytest.raises(ValidationError) as refused:
        GameConfig.model_validate(crowd(over))
    message = str(refused.value)
    assert "Up to 5,001 customers could come on the busiest day" in message
    assert "over the limit of 5,000 a day" in message
    for field in ("base visitors per hour", "opening hours", "weather traffic multipliers"):
        assert field in message


def test_every_traffic_limit_at_once_is_refused_never_scaled_down():
    data = every_limit()
    with pytest.raises(ValidationError, match="Up to 96,131 customers"):
        GameConfig.model_validate(data)
    # Nothing is adjusted: read without the cap, the config is exactly what was sent.
    kept = GameConfig.validate_uncapped(data)
    assert kept.people_preferences["Child"].spawn_per_hour == MAX_SPAWN_PER_HOUR
    assert kept.weather_multipliers == every_limit()["weather_multipliers"]
    assert kept.min_max_values.hour.max == MAX_HOUR


def test_another_refusal_comes_first():
    data = every_limit()
    data["people_preferences"]["Adult"]["preferred_degrees"] = 45  # outside 0-40
    with pytest.raises(ValidationError, match="Adult: preferred temperature"):
        GameConfig.model_validate(data)
    with pytest.raises(ValidationError, match="Adult: preferred temperature"):
        GameConfig.validate_uncapped(data)


# ----------------------------------------------------------------- old games
def test_a_stored_game_over_the_cap_still_loads_and_plays():
    data = crowd(40)  # ~5,800 customers on the busiest day
    with pytest.raises(ValidationError, match="over the limit"):
        GameConfig.model_validate(data)
    # A game created before the cap: its stored payload carries the config as it was.
    game = LemonadeGame(GameConfig.validate_uncapped(data), seed=5)
    payload = game.to_payload()
    loaded = LemonadeGame.from_payload(payload)
    assert loaded.config.model_dump() == game.config.model_dump()
    result = loaded.run_day(DayPlan.model_validate(PLAN))
    assert result["record"]["visitors"] == len(result["events"]) > 0
    again = LemonadeGame.from_payload(loaded.to_payload())
    assert again.days == loaded.days and again.view()["phase"] == "played"


@pytest.mark.parametrize("kind", PERSON_TYPES)
def test_each_types_visitors_drive_the_figure(kind):
    data = crowd(0)
    base = max_expected_customers(GameConfig.model_validate(data))
    data["people_preferences"][kind]["spawn_per_hour"] = 10
    assert base == 0 and max_expected_customers(GameConfig.model_validate(data)) > 0
