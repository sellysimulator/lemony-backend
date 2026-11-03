"""Every figure the config screen and its help show, for a draft configuration.

``POST /config/preview`` answers with these, so the client draws the help's charts,
tables and worked examples and the config screen's pack prices and spoilage bars
without working out any of them. Each figure comes from the function the engine
uses for it: the demand functions in ``demand.py`` (on ``build_people``'s types),
``weather.weather_odds`` (``roll_day_weather``'s own steps), ``Ingredient.hazard``,
``ingredients.pack_offers`` (``IngredientConfig.pack_price``), and
``LemonadeGame.purchase_cost`` / ``cup_cost`` for the cost-per-cup examples.

* ``ingredient_preview(cfg)`` needs only that ingredient's settings, so the config
  screen keeps its pack prices and spoilage bars while another part is invalid.
* ``config_help(config)`` needs the whole config. Its x-axes (``grids``) are chosen
  here too, so every series lines up with them: hours of the day, a temperature
  grid of at most 60 points, a price grid on multiples of the price step, every
  amount of each recipe ingredient, and popularity in tenths.

Money figures are rounded to the cent; shares and visitor counts to 6 places.
"""

from __future__ import annotations

import math
from typing import Any

from .config_model import FloatRange, GameConfig, IngredientConfig
from .defaults import INGREDIENT_NAMES, PERSON_TYPES, RECIPE_INGREDIENTS, WEATHER_TYPES
from .demand import (
    apply_popularity,
    buy_probability,
    cheap_floor,
    expected_spawn,
    hour_score,
    ingredient_score,
    ingredient_scores,
    popularity_factor,
    recipe_quality,
    refusal_reason,
    temperature_score,
    willingness_to_pay,
    wtp_share,
)
from .engine import LemonadeGame, cup_usage
from .ingredients import Ingredient, pack_offers
from .limits import PRICE_STEP
from .money import round_cents
from .people import PersonType
from .plans import favourite_recipe, sample_plan, typical_plan
from .weather import weather_odds

# Nights covered by the spoilage figures (the help's charts show up to 30).
SPOILAGE_NIGHTS = 30
# The worked spoilage example: a batch of this many units, none of it sold.
EXAMPLE_BATCH = 100
# At most this many points on the temperature, price and recipe-amount grids.
GRID_CAP = 60
# Popularity in tenths, 0 to 1.
POPULARITY_GRID = [k / 10 for k in range(11)]
# Popularities the starting-popularity table always lists (with the starting one).
POPULARITY_PROBES = (0.0, 0.25, 0.5, 0.75, 1.0)
# A whole count from a quotient of money values is nudged before ceil: 8.55 / 0.03 is
# 285.00000000000006.
_NOISE = 1e-9


def _share(value: float) -> float:
    return round(value, 6)


def _half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _steps(low: float, high: float, step: float) -> list[float]:
    """``low``, ``low + step``, ... up to ``high``, at most GRID_CAP points, to the cent."""
    out: list[float] = []
    k = 0
    while low + k * step <= high + _NOISE and len(out) < GRID_CAP:
        out.append(round_cents(low + k * step))
        k += 1
    return out


def _price_grid(price: FloatRange) -> list[float]:
    """About 30 steps across the price range, each a multiple of the price step."""
    multiple = _half_up(round((price.max - price.min) / 30 / PRICE_STEP, 9))
    return _steps(price.min, price.max, max(1, multiple) * PRICE_STEP)


def _temperature_grid(temperature: FloatRange) -> list[float]:
    """Whole-degree steps, widened so a wide range keeps about 40 points."""
    step = max(1, math.ceil((temperature.max - temperature.min) / 40))
    return _steps(temperature.min, temperature.max, step)


def typical_temperature(config: GameConfig, weather: str) -> float:
    """A typical day of this weather: the middle of its range, inside the day's range."""
    t = config.min_max_values.temperature
    r = config.weather_temperature_ranges[weather]
    return min(t.max, max(t.min, (r.min + r.max) / 2))


def _chance(config: GameConfig, person: PersonType, price: float, recipe: dict[str, int]) -> float:
    """The buy chance at a neutral popularity, exactly as run_day forms it."""
    quality = recipe_quality(ingredient_scores(person, recipe))
    return buy_probability(person, price, willingness_to_pay(person, quality, config.quality_swing))


# ------------------------------------------------------------------ ingredients
def ingredient_preview(name: str, cfg: IngredientConfig) -> dict[str, Any]:
    """One ingredient's packs and spoilage, from its own settings only.

    ``packs`` is ``pack_offers``; ``smallest`` / ``biggest`` are two of them;
    ``break_even_units`` is how many units of the biggest pack you must use for it to
    beat buying them in the smallest (None with a single size); ``hazard[n - 1]`` the
    chance a unit spoils on night ``n``; ``survival[n]`` the share of a batch still
    good after ``n`` nights; ``example_batch`` what is left of 100 units one and two
    nights past the fresh days.
    """
    offers = pack_offers(cfg)
    smallest, biggest = offers[0], offers[-1]
    if biggest["size"] > smallest["size"]:
        small_unit = smallest["price"] / smallest["size"]
        break_even = math.ceil(biggest["price"] / small_unit - _NOISE) if small_unit > 0 else 0
    else:
        break_even = None
    stock = Ingredient.from_config(name, cfg)
    nights = [min(cfg.fresh_days + 1, cfg.max_days), min(cfg.fresh_days + 2, cfg.max_days)]
    return {
        "packs": offers,
        "smallest": smallest,
        "biggest": biggest,
        "break_even_units": break_even,
        "hazard": [_share(stock.hazard(n)) for n in range(1, SPOILAGE_NIGHTS + 1)],
        "survival": [_share(stock.survival(n)) for n in range(SPOILAGE_NIGHTS + 1)],
        "example_batch": {
            "size": EXAMPLE_BATCH,
            "after": [
                {"night": n, "left": _half_up(EXAMPLE_BATCH * stock.survival(n))} for n in nights
            ],
        },
    }


# ----------------------------------------------------------------------- help
def _person_figures(
    config: GameConfig,
    game: LemonadeGame,
    person: PersonType,
    grids: dict[str, Any],
    typical: dict[str, Any],
) -> dict[str, Any]:
    prefs = config.people_preferences[person.kind]
    swing = config.quality_swing
    price_range = config.min_max_values.price
    best = willingness_to_pay(person, 1, swing)
    floor = cheap_floor(person)
    reach = person.tolerances.price[1] * person.average_expense
    probes: list[dict[str, float]] = []
    for raw in (floor, person.average_expense, best, price_range.max):
        price = round_cents(min(price_range.max, max(price_range.min, raw)))
        if price not in [p["price"] for p in probes]:
            probes.append({"price": price, "chance": _share(buy_probability(person, price, best))})
    scores = ingredient_scores(person, typical["recipe"])
    quality = recipe_quality(scores)
    wtp = willingness_to_pay(person, quality, swing)
    typical_chance = buy_probability(person, typical["price"], wtp)
    temperatures = {w: typical_temperature(config, w) for w in WEATHER_TYPES}
    multipliers = config.weather_multipliers

    def spawn(hour: int, weather: str) -> float:
        return expected_spawn(person, hour, weather, temperatures[weather], multipliers[weather])

    return {
        "hour_score": [_share(hour_score(person, h)) for h in grids["hours"]],
        "temperature_score": [_share(temperature_score(person, t)) for t in grids["temperatures"]],
        "ingredient_score": {
            name: [_share(ingredient_score(person, name, a)) for a in grids["per_cup"][name]]
            for name in RECIPE_INGREDIENTS
        },
        # Chance to buy the typical plan's recipe at each price of the grid.
        "typical_chance": [
            _share(_chance(config, person, p, typical["recipe"])) for p in grids["prices"]
        ],
        # Each type's chance to buy THIS type's favourite cup at each price of the grid.
        "favourite_chance": {
            kind: [
                _share(_chance(config, other, p, favourite_recipe(config, person.kind)))
                for p in grids["prices"]
            ]
            for kind, other in zip(PERSON_TYPES, game.people, strict=True)
        },
        # Expected visitors of this type per hour, each weather at its typical temperature.
        "spawn": {w: [_share(spawn(h, w)) for h in grids["hours"]] for w in WEATHER_TYPES},
        "preferred_hour_spawn": {w: _share(spawn(prefs.preferred_hour, w)) for w in WEATHER_TYPES},
        "wtp_best": round_cents(best),
        "wtp_worst": round_cents(willingness_to_pay(person, 0, swing)),
        "cheap_floor": round_cents(floor),
        # One "price tolerance above" in money: half buy at wtp_best, ~95% this much under
        # it (sure_price) and ~5% this much over it (long_shot_price).
        "price_reach": round_cents(reach),
        "sure_price": round_cents(best - reach),
        "long_shot_price": round_cents(best + reach),
        "favourite_probes": probes,
        "favourite_at_budget": _share(buy_probability(person, person.average_expense, best)),
        # The worked example at the typical plan, at a neutral popularity.
        "worked": {
            "scores": {name: _share(scores[name]) for name in RECIPE_INGREDIENTS},
            "quality": _share(quality),
            "wtp": round_cents(wtp),
            "chance": _share(typical_chance),
            "reason": refusal_reason(person, typical["price"], typical["recipe"], scores),
        },
        # The typical plan's chance, scaled by each popularity of the grid.
        "popularity_chance": [_share(apply_popularity(typical_chance, x)) for x in POPULARITY_GRID],
    }


def _cup_cost_example(
    config: GameConfig, game: LemonadeGame, sample: dict[str, Any], which: str
) -> dict[str, Any]:
    """Day 1 with one pack of each ingredient (the smallest or the biggest) and the
    mid-range recipe, priced by the engine; cash is not checked."""
    purchases = {
        name: {
            str(
                (min if which == "smallest" else max)(
                    p.size for p in config.ingredients[name].packs
                )
            ): 1
        }
        for name in INGREDIENT_NAMES
    }
    spend, _, lots = game.purchase_cost(purchases)
    cups, cost = game.cup_cost(sample["recipe"], lots)
    return {
        "purchases": purchases,
        "spend": spend,
        "cups_possible": cups,
        "cost_per_cup": cost,
        # How many times the starting cash buys these packs; None when they cost nothing.
        "covers": round(config.starting_cash / spend, 4) if spend > 0 else None,
        # The mid-range price less the cost per cup.
        "margin_per_cup": None if cost is None else round_cents(sample["price"] - cost),
    }


def config_help(config: GameConfig) -> dict[str, Any]:
    """Every figure of the config help for ``config`` (see the module docstring)."""
    game = LemonadeGame(config, seed=0)  # nothing is drawn: the seed plays no part
    mm = config.min_max_values
    grids: dict[str, Any] = {
        "hours": list(range(mm.hour.min, mm.hour.max + 1)),
        "temperatures": _temperature_grid(mm.temperature),
        "prices": _price_grid(mm.price),
        "per_cup": {
            name: list(range(getattr(mm, name).min, getattr(mm, name).max + 1))[:GRID_CAP]
            for name in RECIPE_INGREDIENTS
        },
        "popularity": POPULARITY_GRID,
    }
    sample = sample_plan(config)
    typical = typical_plan(config)
    odds = weather_odds(config)
    weather: dict[str, dict[str, float]] = {}
    for w in WEATHER_TYPES:
        temp = typical_temperature(config, w)
        multiplier = config.weather_multipliers[w]
        weather[w] = {
            "odds": _share(odds[w]),
            "expected_days": _share(odds[w] * config.num_days),
            "typical_temperature": temp,
            # Expected visitors over a whole day of this weather, every type together.
            "day_visitors": _share(
                sum(
                    expected_spawn(person, h, w, temp, multiplier)
                    for h in grids["hours"]
                    for person in game.people
                )
            ),
        }
    people = {
        person.kind: _person_figures(config, game, person, grids, typical) for person in game.people
    }
    start = config.starting_popularity
    probes = sorted({*POPULARITY_PROBES, start})
    return {
        "grids": grids,
        "plans": {"sample": sample, "typical": typical},
        "weather": weather,
        "people": people,
        # What a cup is worth as a share of the budget: loved, average, disliked in every way.
        "quality_swing": {
            "best": _share(wtp_share(1, config.quality_swing)),
            "average": _share(wtp_share(0.5, config.quality_swing)),
            "worst": _share(wtp_share(0, config.quality_swing)),
        },
        "popularity": {
            "start_factor": _share(popularity_factor(start)),
            "probes": [
                {
                    "popularity": x,
                    "factor": _share(popularity_factor(x)),
                    # Each type buying its favourite cup at its budget, at this popularity.
                    "favourite_at_budget": {
                        kind: _share(apply_popularity(people[kind]["favourite_at_budget"], x))
                        for kind in PERSON_TYPES
                    },
                }
                for x in probes
            ],
        },
        "cup_cost": {
            # Units of each ingredient the mid-range cup takes, the cup included.
            "usage": cup_usage(sample["recipe"]),
            "smallest": _cup_cost_example(config, game, sample, "smallest"),
            "biggest": _cup_cost_example(config, game, sample, "biggest"),
        },
        # Each pack's price as a share of the starting cash (0 with no starting cash).
        "pack_cash_share": {
            name: [
                _share(offer["price"] / config.starting_cash if config.starting_cash else 0.0)
                for offer in pack_offers(config.ingredients[name])
            ]
            for name in INGREDIENT_NAMES
        },
    }
