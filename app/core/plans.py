"""Ready-made plans: the plan screen's first suggestion and the help's examples.

None of these is a rule of the game; they are starting points, worked out here so the
client never rounds to the price step or averages the customers itself.

* ``sample_plan``: the middle of every range (the help's "mid-range cup").
* ``typical_plan``: the customers' average favourite recipe at their average budget.
* ``opening_plan``: day 1 on the plan screen: the mid-range recipe at the average budget.

Halves round up, and prices land on the price step inside the price range.
"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Any

from .config_model import GameConfig
from .defaults import PERSON_TYPES, RECIPE_INGREDIENTS
from .limits import PRICE_STEP
from .money import round_cents


def _half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def on_price_step(config: GameConfig, value: float) -> float:
    """``value`` to the nearest price step, kept inside the price range."""
    price = config.min_max_values.price
    # The quotient is trimmed of float noise first: 0.575 / 0.05 is 11.499999999999998.
    stepped = _half_up(round(value / PRICE_STEP, 9)) * PRICE_STEP
    return round_cents(_clamp(round_cents(stepped), price.min, price.max))


def midpoint_recipe(config: GameConfig) -> dict[str, int]:
    mm = config.min_max_values
    return {
        name: _half_up((getattr(mm, name).min + getattr(mm, name).max) / 2)
        for name in RECIPE_INGREDIENTS
    }


def favourite_recipe(config: GameConfig, person: str) -> dict[str, int]:
    prefs = config.people_preferences[person]
    return {name: getattr(prefs, f"preferred_{name}") for name in RECIPE_INGREDIENTS}


def _average_budget(config: GameConfig) -> float:
    return fmean(config.people_preferences[kind].average_expense for kind in PERSON_TYPES)


def sample_plan(config: GameConfig) -> dict[str, Any]:
    price = config.min_max_values.price
    return {
        "price": on_price_step(config, (price.min + price.max) / 2),
        "recipe": midpoint_recipe(config),
    }


def typical_plan(config: GameConfig) -> dict[str, Any]:
    mm = config.min_max_values
    people = [config.people_preferences[kind] for kind in PERSON_TYPES]
    recipe = {}
    for name in RECIPE_INGREDIENTS:
        amount = _half_up(fmean(getattr(p, f"preferred_{name}") for p in people))
        rng = getattr(mm, name)
        recipe[name] = int(_clamp(amount, rng.min, rng.max))
    return {"price": on_price_step(config, _average_budget(config)), "recipe": recipe}


def opening_plan(config: GameConfig) -> dict[str, Any]:
    return {
        "price": on_price_step(config, _average_budget(config)),
        "recipe": midpoint_recipe(config),
    }
