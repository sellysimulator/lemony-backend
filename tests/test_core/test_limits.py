"""Every number a player configures has a hard bound, and a non-finite one is refused."""

import copy
import math
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config_model import GameConfig
from app.core.defaults import default_config

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
