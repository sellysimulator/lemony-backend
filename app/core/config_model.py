"""The validated game configuration.

Every value is editable by the player except the *names* of the weather types
and person types: those dictionaries must carry exactly the fixed keys.
Cross-field rules (a preference must lie inside its range, weather temperature
ranges must cover the whole temperature range, ...) raise ``ValueError`` with a
sentence the configuration screen shows verbatim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .defaults import (
    INGREDIENT_NAMES,
    MAX_NUM_DAYS,
    PERSON_TYPES,
    RECIPE_INGREDIENTS,
    WEATHER_TYPES,
)

Weather = Literal["sunny", "cloudy", "rainy", "snowy"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntRange(_Strict):
    min: int
    max: int

    @model_validator(mode="after")
    def _ordered(self) -> IntRange:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) must not exceed max ({self.max})")
        return self


class FloatRange(_Strict):
    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> FloatRange:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) must not exceed max ({self.max})")
        return self


class MinMaxValues(_Strict):
    ice: IntRange
    sugar: IntRange
    lemons: IntRange
    temperature: FloatRange
    hour: IntRange
    price: FloatRange

    @model_validator(mode="after")
    def _bounds(self) -> MinMaxValues:
        for name in RECIPE_INGREDIENTS:
            rng: IntRange = getattr(self, name)
            if rng.min < 0 or rng.max > 20:
                raise ValueError(f"{name} per cup must stay within 0-20")
        if not (0 <= self.hour.min < self.hour.max <= 23):
            raise ValueError("Opening hours must satisfy 0 <= open < close <= 23")
        if self.temperature.min < -30 or self.temperature.max > 60:
            raise ValueError("Temperature range must stay within -30..60 °C")
        if self.temperature.min == self.temperature.max:
            raise ValueError("Temperature range must not be a single value")
        if self.price.min < 0 or self.price.max > 100 or self.price.max <= 0:
            raise ValueError("Price range must stay within 0-100 and max must be above 0")
        return self


class PersonPreferences(_Strict):
    spawn_per_hour: float = Field(ge=0, le=200)
    average_expense: float = Field(ge=0, le=100)
    preferred_degrees: float
    preferred_weather: Weather
    preferred_ice: int
    preferred_sugar: int
    preferred_lemons: int
    preferred_hour: int


class IngredientConfig(_Strict):
    unit_cost: float = Field(ge=0, le=100)
    pack_sizes: list[int] = Field(min_length=1, max_length=8)
    fresh_days: int = Field(ge=0, le=60)
    max_days: int = Field(ge=1, le=61)
    never_perishes: bool = False

    @field_validator("pack_sizes")
    @classmethod
    def _packs(cls, value: list[int]) -> list[int]:
        if any(size < 1 or size > 10_000 for size in value):
            raise ValueError("pack sizes must be between 1 and 10000 units")
        if len(set(value)) != len(value):
            raise ValueError("pack sizes must be distinct")
        return sorted(value)

    @model_validator(mode="after")
    def _window(self) -> IngredientConfig:
        if not self.never_perishes and self.fresh_days >= self.max_days:
            raise ValueError(
                f"fresh days ({self.fresh_days}) must be less than max days ({self.max_days})"
            )
        return self


def _exact_keys(value: dict, expected: tuple[str, ...], what: str) -> dict:
    if set(value) != set(expected):
        raise ValueError(f"{what} must have exactly these keys: {', '.join(expected)}")
    return value


class GameConfig(_Strict):
    num_days: int = Field(ge=1, le=MAX_NUM_DAYS)
    starting_cash: float = Field(ge=0, le=1_000_000)
    min_max_values: MinMaxValues
    people_preferences: dict[str, PersonPreferences]
    weather_multipliers: dict[str, float]
    weather_temperature_ranges: dict[str, FloatRange]
    ingredients: dict[str, IngredientConfig]

    @field_validator("people_preferences")
    @classmethod
    def _people_keys(cls, value: dict) -> dict:
        return _exact_keys(value, PERSON_TYPES, "people_preferences")

    @field_validator("weather_multipliers")
    @classmethod
    def _multiplier_keys(cls, value: dict[str, float]) -> dict[str, float]:
        _exact_keys(value, WEATHER_TYPES, "weather_multipliers")
        for name, mult in value.items():
            if not 0 <= mult <= 3:
                raise ValueError(f"weather multiplier for {name} must be within 0-3")
        return value

    @field_validator("weather_temperature_ranges")
    @classmethod
    def _range_keys(cls, value: dict) -> dict:
        return _exact_keys(value, WEATHER_TYPES, "weather_temperature_ranges")

    @field_validator("ingredients")
    @classmethod
    def _ingredient_keys(cls, value: dict) -> dict:
        return _exact_keys(value, INGREDIENT_NAMES, "ingredients")

    @model_validator(mode="after")
    def _cross_field(self) -> GameConfig:
        mm = self.min_max_values
        for ptype, prefs in self.people_preferences.items():
            for name in RECIPE_INGREDIENTS:
                pref = getattr(prefs, f"preferred_{name}")
                rng: IntRange = getattr(mm, name)
                if not rng.min <= pref <= rng.max:
                    raise ValueError(
                        f"{ptype}: preferred {name} ({pref}) must be within {rng.min}-{rng.max}"
                    )
            if not mm.hour.min <= prefs.preferred_hour <= mm.hour.max:
                raise ValueError(
                    f"{ptype}: preferred hour ({prefs.preferred_hour}) must be within "
                    f"opening hours {mm.hour.min}-{mm.hour.max}"
                )
            if not mm.temperature.min <= prefs.preferred_degrees <= mm.temperature.max:
                raise ValueError(
                    f"{ptype}: preferred temperature ({prefs.preferred_degrees}) must be within "
                    f"{mm.temperature.min}-{mm.temperature.max}"
                )
            if not mm.price.min <= prefs.average_expense <= mm.price.max:
                raise ValueError(
                    f"{ptype}: average expense ({prefs.average_expense}) must be within the "
                    f"price range {mm.price.min}-{mm.price.max}"
                )
        self._check_weather_coverage()
        return self

    def _check_weather_coverage(self) -> None:
        """The weather ranges, together, must cover the temperature range with no gap."""
        lo, hi = self.min_max_values.temperature.min, self.min_max_values.temperature.max
        spans = sorted(
            (max(r.min, lo), min(r.max, hi))
            for r in self.weather_temperature_ranges.values()
            if r.max >= lo and r.min <= hi
        )
        reach = lo
        for start, end in spans:
            if start > reach:
                break
            reach = max(reach, end)
        if reach < hi or not spans or spans[0][0] > lo:
            gap_start = reach if spans and spans[0][0] <= lo else lo
            raise ValueError(
                f"Weather temperature ranges leave a gap starting at {gap_start} °C; "
                f"together they must cover {lo}-{hi} °C"
            )
