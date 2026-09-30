"""Daily weather: a random temperature decides the weather type.

The temperature is uniform over the configured range, rounded to 0.5 °C, and
constant for the whole day. The weather is any type whose configured
temperature range contains it (chosen at random when ranges overlap).
"""

from __future__ import annotations

import random

from .config_model import GameConfig
from .defaults import WEATHER_TYPES


def roll_day_weather(config: GameConfig, rng: random.Random) -> tuple[str, float]:
    lo = config.min_max_values.temperature.min
    hi = config.min_max_values.temperature.max
    temperature = round(rng.uniform(lo, hi) * 2) / 2
    temperature = min(max(temperature, lo), hi)
    ranges = config.weather_temperature_ranges
    matches = [w for w in WEATHER_TYPES if ranges[w].min <= temperature <= ranges[w].max]
    if not matches:
        # Only reachable when rounding lands in a sub-0.5 sliver: take the nearest range.
        matches = [
            min(
                WEATHER_TYPES,
                key=lambda w: min(abs(ranges[w].min - temperature), abs(ranges[w].max - temperature)),
            )
        ]
    return rng.choice(matches), temperature
