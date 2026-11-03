"""Daily weather: a random temperature decides the weather type.

The temperature is uniform over the configured range, rounded to 0.5 °C, and
constant for the whole day. The weather is any type whose configured
temperature range contains it (chosen at random when ranges overlap).

``weather_odds`` is the exact long-run share of days of each weather that
``roll_day_weather`` gives, built from the same two steps (``day_temperature``,
``weathers_at``), so the help never works the odds out on its own.
``temperatures_by_weather`` lists, from those same steps, every temperature each
weather can come with (the cap on customers a day takes its worst case over them).
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator

from .config_model import GameConfig
from .defaults import WEATHER_TYPES


def day_temperature(config: GameConfig, drawn: float) -> float:
    """The day's temperature from a uniform draw over the range: to 0.5 °C, kept in range."""
    lo = config.min_max_values.temperature.min
    hi = config.min_max_values.temperature.max
    temperature = round(drawn * 2) / 2
    return min(max(temperature, lo), hi)


def weathers_at(config: GameConfig, temperature: float) -> list[str]:
    """Every weather whose range holds ``temperature``, in WEATHER_TYPES order.

    Only when rounding lands in a sub-0.5 sliver does none match; then it is the nearest
    range, the first in WEATHER_TYPES order on a tie.
    """
    ranges = config.weather_temperature_ranges
    matches = [w for w in WEATHER_TYPES if ranges[w].min <= temperature <= ranges[w].max]
    if not matches:
        matches = [
            min(
                WEATHER_TYPES,
                key=lambda w: min(
                    abs(ranges[w].min - temperature), abs(ranges[w].max - temperature)
                ),
            )
        ]
    return matches


def roll_day_weather(config: GameConfig, rng: random.Random) -> tuple[str, float]:
    lo = config.min_max_values.temperature.min
    hi = config.min_max_values.temperature.max
    temperature = day_temperature(config, rng.uniform(lo, hi))
    return rng.choice(weathers_at(config, temperature)), temperature


def _draw_steps(config: GameConfig) -> Iterator[tuple[float, float]]:
    """Every temperature ``roll_day_weather`` can give, with the share of draws that give it.

    Every stretch of the uniform draw that rounds to one 0.5 °C step gives one temperature
    (``day_temperature`` of a draw inside it).
    """
    lo = config.min_max_values.temperature.min
    hi = config.min_max_values.temperature.max
    if hi <= lo:
        return
    for step in range(math.floor(lo * 2) - 1, math.ceil(hi * 2) + 2):
        start = max(lo, (step - 0.5) / 2)
        end = min(hi, (step + 0.5) / 2)
        if end <= start:
            continue
        yield day_temperature(config, (start + end) / 2), (end - start) / (hi - lo)


def weather_odds(config: GameConfig) -> dict[str, float]:
    """The share of days of each weather, exactly as ``roll_day_weather`` deals them.

    Each temperature's weathers share its stretch of the draw evenly, as ``rng.choice``
    picks among them.
    """
    odds = {w: 0.0 for w in WEATHER_TYPES}
    for temperature, share in _draw_steps(config):
        matches = weathers_at(config, temperature)
        for w in matches:
            odds[w] += share / len(matches)
    return odds


def temperatures_by_weather(config: GameConfig) -> dict[str, list[float]]:
    """Every temperature each weather can come with, as ``roll_day_weather`` deals them.

    A weather with an empty list never happens under this config.
    """
    out: dict[str, list[float]] = {w: [] for w in WEATHER_TYPES}
    for temperature, _ in _draw_steps(config):
        for w in weathers_at(config, temperature):
            if temperature not in out[w]:
                out[w].append(temperature)
    return out
