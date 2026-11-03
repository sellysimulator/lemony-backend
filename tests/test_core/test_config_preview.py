"""The figures `POST /config/preview` gives the config screen and its help.

Each is checked against the engine function that decides the same thing in a game
(`roll_day_weather`, `Ingredient.hazard`, `pack_price`, the demand functions,
`purchase_cost` / `cup_cost`), so the help can never show a rule the game does not play.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from app.core.config_model import FloatRange, GameConfig, IngredientConfig
from app.core.config_preview import (
    SPOILAGE_NIGHTS,
    config_help,
    ingredient_preview,
    typical_temperature,
)
from app.core.defaults import INGREDIENT_NAMES, PERSON_TYPES, WEATHER_TYPES, default_config
from app.core.demand import (
    apply_popularity,
    buy_probability,
    cheap_floor,
    expected_spawn,
    hour_score,
    ingredient_score,
    ingredient_scores,
    recipe_quality,
    refusal_reason,
    temperature_score,
    willingness_to_pay,
)
from app.core.engine import LemonadeGame
from app.core.ingredients import Ingredient, pack_offers
from app.core.money import round_cents
from app.core.plans import on_price_step, opening_plan, sample_plan, typical_plan
from app.core.weather import day_temperature, roll_day_weather, weather_odds, weathers_at


def make_config(edit: Any = None) -> GameConfig:
    data = default_config()
    if edit is not None:
        edit(data)
    return GameConfig.model_validate(data)


# ------------------------------------------------------------------ weather
def _odds_by_sweep(config: GameConfig, points: int = 200_000) -> dict[str, float]:
    """The draw swept over evenly spaced points, through the engine's own two steps."""
    lo = config.min_max_values.temperature.min
    hi = config.min_max_values.temperature.max
    odds = {w: 0.0 for w in WEATHER_TYPES}
    for i in range(points):
        matches = weathers_at(config, day_temperature(config, lo + (i + 0.5) * (hi - lo) / points))
        for w in matches:
            odds[w] += 1 / points / len(matches)
    return odds


def _edges(data: dict[str, Any]) -> None:
    # Bounds off the 0.5 grid, and overlapping ranges.
    data["min_max_values"]["temperature"] = {"min": 10.2, "max": 30.3}
    data["weather_temperature_ranges"] = {
        "snowy": {"min": 10.2, "max": 10.2},
        "rainy": {"min": 10.0, "max": 18.0},
        "cloudy": {"min": 15.0, "max": 25.0},
        "sunny": {"min": 22.5, "max": 31.0},
    }


@pytest.mark.parametrize("edit", [None, _edges])
def test_weather_odds_are_the_engines_draw(edit):
    config = make_config(edit)
    odds = weather_odds(config)
    assert sum(odds.values()) == pytest.approx(1)
    sweep = _odds_by_sweep(config)
    for w in WEATHER_TYPES:
        assert odds[w] == pytest.approx(sweep[w], abs=2e-4), w


def test_the_sliver_under_a_bound_off_the_grid_is_the_bound_itself():
    config = make_config(_edges)
    # 10.2 to 10.25 rounds to 10.0, which the range keeps as 10.2: snowy (and rainy) then.
    assert day_temperature(config, 10.22) == 10.2
    assert weathers_at(config, 10.2) == ["rainy", "snowy"]
    assert weather_odds(config)["snowy"] == pytest.approx(0.05 / 20.1 / 2)


def test_roll_day_weather_draws_as_before():
    config = make_config()
    rng, check = random.Random("seed:1:weather"), random.Random("seed:1:weather")
    weather, temperature = roll_day_weather(config, rng)
    lo, hi = 0, 40
    expected_t = min(max(round(check.uniform(lo, hi) * 2) / 2, lo), hi)
    assert temperature == expected_t
    assert weather == check.choice(weathers_at(config, expected_t))
    assert rng.random() == check.random()  # the same number of draws


def test_no_matching_range_takes_the_nearest_and_a_tie_the_first_weather():
    base = make_config()
    ranges = {"sunny": (30, 40), "cloudy": (0, 10), "rainy": (20, 29), "snowy": (12, 18)}
    # Copied without validation: a valid config never leaves a temperature uncovered.
    config = base.model_copy(
        update={
            "weather_temperature_ranges": {
                w: FloatRange.model_construct(min=lo, max=hi) for w, (lo, hi) in ranges.items()
            }
        }
    )
    assert weathers_at(config, 29.6) == ["sunny"]  # 0.4 from sunny, 0.6 from rainy
    assert weathers_at(config, 11) == ["cloudy"]  # 1 from cloudy and snowy: cloudy is listed first
    assert weathers_at(config, 19) == ["rainy"]  # 1 from snowy and rainy: rainy is listed first


# ------------------------------------------------------------------ plans
def test_plans_on_the_default_config():
    config = make_config()
    # Mid-range: (0+5)/2 rounds up to 3, lemons (1+5)/2 = 3; price (0.2+1)/2 = 0.6.
    assert sample_plan(config) == {"price": 0.6, "recipe": {"ice": 3, "sugar": 3, "lemons": 3}}
    # Average favourites 1, 1.5 -> 2, 2; average budget 0.5625 -> 0.55.
    assert typical_plan(config) == {"price": 0.55, "recipe": {"ice": 1, "sugar": 2, "lemons": 2}}
    assert opening_plan(config) == {"price": 0.55, "recipe": {"ice": 3, "sugar": 3, "lemons": 3}}


def test_on_price_step_rounds_halves_up_and_stays_in_range():
    config = make_config()
    assert on_price_step(config, 0.575) == 0.6  # 11.499999999999998 steps without the trim
    assert on_price_step(config, 0.574) == 0.55
    assert on_price_step(config, 0.01) == 0.2
    assert on_price_step(config, 7) == 1.0


# ------------------------------------------------------------------ packs
def test_pack_offers_price_every_pack_as_a_purchase_does():
    cfg = make_config().ingredients["lemons"]
    offers = pack_offers(cfg)
    assert [o["size"] for o in offers] == [100, 300, 500, 700]
    for offer, pack in zip(offers, cfg.packs, strict=True):
        assert offer["price"] == cfg.pack_price(pack)
        assert offer["unit_price"] == round(cfg.pack_price(pack) / pack.size, 6)
        assert offer["saving"] == round_cents(pack.size * cfg.unit_cost - offer["price"])
        assert offer["dearer_than"] is None


def test_a_bigger_pack_dearer_per_unit_names_the_smaller_cheaper_one():
    cfg = IngredientConfig(
        unit_cost=0.1,
        packs=[
            {"size": 10, "discount": 0.2},
            {"size": 20, "discount": 0.3},
            {"size": 50, "discount": 0.1},
        ],
        fresh_days=0,
        max_days=1,
    )
    dearer = {o["size"]: o["dearer_than"] for o in pack_offers(cfg)}
    assert dearer == {10: None, 20: None, 50: 20}  # both are cheaper: the deeper discount


# ------------------------------------------------------------------ ingredient figures
def test_spoilage_figures_are_the_engines_hazard():
    cfg = make_config().ingredients["lemons"]
    figures = ingredient_preview("lemons", cfg)
    stock = Ingredient.from_config("lemons", cfg)
    assert len(figures["hazard"]) == SPOILAGE_NIGHTS
    assert figures["hazard"] == [round(stock.hazard(n), 6) for n in range(1, SPOILAGE_NIGHTS + 1)]
    assert figures["survival"][0] == 1
    for n in range(1, SPOILAGE_NIGHTS + 1):
        assert figures["survival"][n] == pytest.approx(
            figures["survival"][n - 1] * (1 - stock.hazard(n)), abs=1e-6
        )
    # fresh 3, max 7: nights 4 and 5 lose a quarter, then a half of what is left.
    assert figures["example_batch"] == {
        "size": 100,
        "after": [{"night": 4, "left": 75}, {"night": 5, "left": 38}],
    }


def test_break_even_units_of_the_biggest_pack():
    figures = ingredient_preview("lemons", make_config().ingredients["lemons"])
    # 700 lemons at 20% off cost 28.00; at 0.05 each that buys 560 lemons.
    assert figures["break_even_units"] == 560
    single = IngredientConfig(unit_cost=0.1, packs=[{"size": 10}], fresh_days=0, max_days=1)
    assert ingredient_preview("ice", single)["break_even_units"] is None


def test_an_ingredient_that_never_perishes_never_spoils():
    figures = ingredient_preview("cups", make_config().ingredients["cups"])
    assert set(figures["hazard"]) == {0} and set(figures["survival"]) == {1}


# ------------------------------------------------------------------ help
@pytest.fixture(scope="module")
def defaults() -> tuple[GameConfig, dict[str, Any]]:
    config = make_config()
    return config, config_help(config)


def test_grids(defaults):
    _, h = defaults
    assert h["grids"]["hours"] == list(range(9, 18))
    assert h["grids"]["temperatures"] == list(range(41))
    # 0.80 / 30 rounds to one 5-cent step.
    assert h["grids"]["prices"] == [round_cents(0.2 + 0.05 * k) for k in range(17)]
    assert h["grids"]["per_cup"]["lemons"] == [1, 2, 3, 4, 5]


def test_weather_figures(defaults):
    config, h = defaults
    game = LemonadeGame(config, 0)
    odds = weather_odds(config)
    for w in WEATHER_TYPES:
        f = h["weather"][w]
        assert f["odds"] == round(odds[w], 6)
        assert f["expected_days"] == round(odds[w] * 7, 6)
        temp = typical_temperature(config, w)
        assert f["typical_temperature"] == temp
        visitors = sum(
            expected_spawn(p, hr, w, temp, config.weather_multipliers[w])
            for hr in range(9, 18)
            for p in game.people
        )
        assert f["day_visitors"] == round(visitors, 6)
    assert h["weather"]["sunny"]["typical_temperature"] == 32.5


def test_person_figures_are_the_demand_functions(defaults):
    config, h = defaults
    grids, typical = h["grids"], h["plans"]["typical"]
    for person in LemonadeGame(config, 0).people:
        f = h["people"][person.kind]
        assert f["hour_score"] == [round(hour_score(person, x), 6) for x in grids["hours"]]
        assert f["temperature_score"] == [
            round(temperature_score(person, t), 6) for t in grids["temperatures"]
        ]
        assert f["ingredient_score"]["sugar"] == [
            round(ingredient_score(person, "sugar", a), 6) for a in grids["per_cup"]["sugar"]
        ]
        scores = ingredient_scores(person, typical["recipe"])
        wtp = willingness_to_pay(person, recipe_quality(scores), config.quality_swing)
        chance = buy_probability(person, typical["price"], wtp)
        assert f["worked"] == {
            "scores": {k: round(v, 6) for k, v in scores.items()},
            "quality": round(recipe_quality(scores), 6),
            "wtp": round_cents(wtp),
            "chance": round(chance, 6),
            "reason": refusal_reason(person, typical["price"], typical["recipe"], scores),
        }
        assert f["popularity_chance"] == [
            round(apply_popularity(chance, x), 6) for x in grids["popularity"]
        ]
        best = willingness_to_pay(person, 1, config.quality_swing)
        assert f["wtp_best"] == round_cents(best)
        assert f["cheap_floor"] == round_cents(cheap_floor(person))
        reach = person.tolerances.price[1] * person.average_expense
        assert (f["sure_price"], f["long_shot_price"]) == (
            round_cents(best - reach),
            round_cents(best + reach),
        )
        for probe in f["favourite_probes"]:
            assert probe["chance"] == round(buy_probability(person, probe["price"], best), 6)


def test_favourite_chance_is_every_types_chance_for_one_types_favourite_cup(defaults):
    config, h = defaults
    people = LemonadeGame(config, 0).people
    adult = h["people"]["Adult"]
    favourite = {"ice": 1, "sugar": 1, "lemons": 3}
    assert set(adult["favourite_chance"]) == set(PERSON_TYPES)
    for person in people:
        quality = recipe_quality(ingredient_scores(person, favourite))
        wtp = willingness_to_pay(person, quality, config.quality_swing)
        assert adult["favourite_chance"][person.kind] == [
            round(buy_probability(person, p, wtp), 6) for p in h["grids"]["prices"]
        ]


def test_popularity_and_quality_swing(defaults):
    _, h = defaults
    assert h["quality_swing"] == {"best": 1.4, "average": 1.0, "worst": 0.6}
    assert h["popularity"]["start_factor"] == 1.0
    assert [p["popularity"] for p in h["popularity"]["probes"]] == [0, 0.25, 0.5, 0.75, 1]
    assert [p["factor"] for p in h["popularity"]["probes"]] == [0.5, 0.75, 1.0, 1.25, 1.5]
    custom = config_help(make_config(lambda d: d.update(starting_popularity=0.3)))
    assert [p["popularity"] for p in custom["popularity"]["probes"]] == [0, 0.25, 0.3, 0.5, 0.75, 1]


def test_cup_cost_examples_are_the_engines_day_one(defaults):
    config, h = defaults
    game = LemonadeGame(config, 0)
    sample = h["plans"]["sample"]
    for which, size in (("smallest", "100"), ("biggest", "700")):
        example = h["cup_cost"][which]
        purchases = {name: {size: 1} for name in INGREDIENT_NAMES}
        assert example["purchases"] == purchases
        spend, _, lots = game.purchase_cost(purchases)
        cups, cost = game.cup_cost(sample["recipe"], lots)
        assert (example["spend"], example["cups_possible"], example["cost_per_cup"]) == (
            spend,
            cups,
            cost,
        )
        assert example["covers"] == round(100 / spend, 4)
        assert example["margin_per_cup"] == round_cents(sample["price"] - cost)
    assert h["cup_cost"]["usage"] == {"ice": 3, "sugar": 3, "lemons": 3, "cups": 1}
    assert h["pack_cash_share"]["lemons"] == [0.05, 0.1425, 0.225, 0.28]


def test_help_reads_nothing_random():
    config = make_config()
    assert config_help(config) == config_help(config)


def test_every_number_is_finite(defaults):
    def walk(value: Any) -> None:
        if isinstance(value, float):
            assert math.isfinite(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(defaults[1])
