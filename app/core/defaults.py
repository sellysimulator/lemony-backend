"""Default game configuration.

These are the values of the original ``config.py``, reshaped into one
document the player edits on the configuration screen. Changes from the
original values:

* Adult ``preferred_hour`` 18 -> 17: 18 lies outside the 9-17 opening hours.
* ``spawn_per_hour`` moved into each person's preferences.
* New: ``num_days``, ``starting_cash``, the ``price`` range (gives the buy
  formula its price denominator), ``weather_temperature_ranges`` and the
  per-ingredient perishing model (``fresh_days`` / ``max_days`` /
  ``never_perishes``) replacing ``ingredients_time_alive``.
"""

from __future__ import annotations

import copy
from typing import Any

WEATHER_TYPES: tuple[str, ...] = ("sunny", "cloudy", "rainy", "snowy")
PERSON_TYPES: tuple[str, ...] = ("Child", "Teenager", "Adult", "Senior")
INGREDIENT_NAMES: tuple[str, ...] = ("ice", "sugar", "lemons", "cups")
RECIPE_INGREDIENTS: tuple[str, ...] = ("ice", "sugar", "lemons")

MAX_NUM_DAYS = 30

_DEFAULT_CONFIG: dict[str, Any] = {
    "num_days": 7,
    "starting_cash": 50.0,
    "min_max_values": {
        "ice": {"min": 0, "max": 5},
        "sugar": {"min": 0, "max": 5},
        "lemons": {"min": 0, "max": 5},
        "temperature": {"min": 0, "max": 40},
        "hour": {"min": 9, "max": 17},
        "price": {"min": 0.0, "max": 3.0},
    },
    "people_preferences": {
        "Child": {
            "spawn_per_hour": 5,
            "average_expense": 0.20,
            "preferred_degrees": 30,
            "preferred_weather": "sunny",
            "preferred_ice": 3,
            "preferred_sugar": 3,
            "preferred_lemons": 2,
            "preferred_hour": 12,
        },
        "Teenager": {
            "spawn_per_hour": 3,
            "average_expense": 0.30,
            "preferred_degrees": 25,
            "preferred_weather": "cloudy",
            "preferred_ice": 2,
            "preferred_sugar": 3,
            "preferred_lemons": 3,
            "preferred_hour": 15,
        },
        "Adult": {
            "spawn_per_hour": 5,
            "average_expense": 0.80,
            "preferred_degrees": 20,
            "preferred_weather": "sunny",
            "preferred_ice": 1,
            "preferred_sugar": 2,
            "preferred_lemons": 4,
            "preferred_hour": 17,
        },
        "Senior": {
            "spawn_per_hour": 2,
            "average_expense": 0.75,
            "preferred_degrees": 15,
            "preferred_weather": "cloudy",
            "preferred_ice": 2,
            "preferred_sugar": 2,
            "preferred_lemons": 2,
            "preferred_hour": 14,
        },
    },
    "weather_multipliers": {"sunny": 1.0, "cloudy": 0.7, "rainy": 0.5, "snowy": 0.2},
    "weather_temperature_ranges": {
        "snowy": {"min": 0, "max": 5},
        "rainy": {"min": 5, "max": 15},
        "cloudy": {"min": 15, "max": 25},
        "sunny": {"min": 25, "max": 40},
    },
    "ingredients": {
        "ice": {
            "unit_cost": 0.05,
            "pack_sizes": [50, 100, 200, 500],
            "fresh_days": 0,
            "max_days": 1,
            "never_perishes": False,
        },
        "sugar": {
            "unit_cost": 0.10,
            "pack_sizes": [50, 100, 200, 500],
            "fresh_days": 5,
            "max_days": 10,
            "never_perishes": False,
        },
        "lemons": {
            "unit_cost": 0.15,
            "pack_sizes": [50, 100, 200, 500],
            "fresh_days": 3,
            "max_days": 7,
            "never_perishes": False,
        },
        "cups": {
            "unit_cost": 0.05,
            "pack_sizes": [50, 100, 200, 500],
            "fresh_days": 30,
            "max_days": 31,
            "never_perishes": True,
        },
    },
}


def default_config() -> dict[str, Any]:
    """A fresh, mutable copy of the default configuration."""
    return copy.deepcopy(_DEFAULT_CONFIG)
