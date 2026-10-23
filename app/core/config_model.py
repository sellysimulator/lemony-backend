"""The validated game configuration.

Every value is editable by the player except the *names* of the weather types
and person types: those dictionaries must carry exactly the fixed keys.
Cross-field rules (a preference must lie inside its range, weather temperature
ranges must cover the whole temperature range, ...) raise ``ValueError`` with a
sentence the configuration screen shows verbatim.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .defaults import (
    INGREDIENT_NAMES,
    PERSON_TYPES,
    RECIPE_INGREDIENTS,
    WEATHER_TYPES,
)
from .limits import (
    MAX_AVERAGE_EXPENSE,
    MAX_FRESH_DAYS,
    MAX_HOUR,
    MAX_MAX_DAYS,
    MAX_NUM_DAYS,
    MAX_PACK_DISCOUNT,
    MAX_PACK_SIZE,
    MAX_PACKS,
    MAX_PER_CUP,
    MAX_PRICE,
    MAX_PRICE_TOLERANCE_ABOVE,
    MAX_SPAWN_PER_HOUR,
    MAX_STARTING_CASH,
    MAX_TEMPERATURE,
    MAX_TOLERANCE,
    MAX_UNIT_COST,
    MAX_WEATHER_MULTIPLIER,
    MIN_TEMPERATURE,
)
from .money import round_cents

Weather = Literal["sunny", "cloudy", "rainy", "snowy"]


class _Strict(BaseModel):
    # No NaN or infinity anywhere: JSON cannot carry them back to the client or into MySQL.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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
            if rng.min < 0 or rng.max > MAX_PER_CUP:
                raise ValueError(f"{name} per cup must stay within 0-{MAX_PER_CUP}")
        if not (0 <= self.hour.min < self.hour.max <= MAX_HOUR):
            raise ValueError(f"Opening hours must satisfy 0 <= open < close <= {MAX_HOUR}")
        if self.temperature.min < MIN_TEMPERATURE or self.temperature.max > MAX_TEMPERATURE:
            raise ValueError(
                f"Temperature range must stay within {MIN_TEMPERATURE}..{MAX_TEMPERATURE} °C"
            )
        if self.temperature.min == self.temperature.max:
            raise ValueError("Temperature range must not be a single value")
        if self.price.min < 0 or self.price.max > MAX_PRICE or self.price.max <= 0:
            raise ValueError(f"Price range must stay within 0-{MAX_PRICE} and max must be above 0")
        return self


class Tolerance(_Strict):
    """How far below / above a preference a customer still accepts, down to a score of 0."""

    below: float = Field(ge=0, le=MAX_TOLERANCE)
    above: float = Field(ge=0, le=MAX_TOLERANCE)


class Tolerances(_Strict):
    """Ingredients in units per cup; price as a share of the customer's budget."""

    ice: Tolerance
    sugar: Tolerance
    lemons: Tolerance
    price: Tolerance

    @model_validator(mode="after")
    def _price_shares(self) -> Tolerances:
        if self.price.below > 1:
            raise ValueError("price tolerance below must be a share of the budget, 0-1 (0-100%)")
        if not 0 < self.price.above <= MAX_PRICE_TOLERANCE_ABOVE:
            raise ValueError(
                "price tolerance above must be above 0 and at most "
                f"{MAX_PRICE_TOLERANCE_ABOVE} ({MAX_PRICE_TOLERANCE_ABOVE:.0%})"
            )
        return self


class PersonPreferences(_Strict):
    spawn_per_hour: float = Field(ge=0, le=MAX_SPAWN_PER_HOUR)
    average_expense: float = Field(ge=0, le=MAX_AVERAGE_EXPENSE)
    preferred_degrees: float
    preferred_weather: Weather
    preferred_ice: int
    preferred_sugar: int
    preferred_lemons: int
    preferred_hour: int
    tolerances: Tolerances


class PackOption(_Strict):
    """One pack on sale: ``size`` units at ``unit_cost`` each, less ``discount`` (0-0.9)."""

    size: int
    discount: float = 0.0

    @model_validator(mode="after")
    def _bounds(self) -> PackOption:
        if not 1 <= self.size <= MAX_PACK_SIZE:
            raise ValueError(f"pack sizes must be between 1 and {MAX_PACK_SIZE} units")
        if not 0 <= self.discount <= MAX_PACK_DISCOUNT:
            raise ValueError(f"pack discounts must be between 0% and {MAX_PACK_DISCOUNT:.0%}")
        return self


class IngredientConfig(_Strict):
    unit_cost: float = Field(ge=0, le=MAX_UNIT_COST)
    packs: list[PackOption] = Field(min_length=1, max_length=MAX_PACKS)
    fresh_days: int = Field(ge=0, le=MAX_FRESH_DAYS)
    max_days: int = Field(ge=1, le=MAX_MAX_DAYS)
    never_perishes: bool = False

    @model_validator(mode="before")
    @classmethod
    def _legacy_pack_sizes(cls, data: Any) -> Any:
        """Configs saved before pack discounts carry ``pack_sizes: [int]``; read them as undiscounted packs."""
        if isinstance(data, dict) and "pack_sizes" in data and "packs" not in data:
            sizes = data["pack_sizes"]
            data = {k: v for k, v in data.items() if k != "pack_sizes"}
            data["packs"] = (
                [{"size": size, "discount": 0.0} for size in sizes]
                if isinstance(sizes, list)
                else sizes
            )
        return data

    @field_validator("packs")
    @classmethod
    def _packs(cls, value: list[PackOption]) -> list[PackOption]:
        sizes = [p.size for p in value]
        if len(set(sizes)) != len(sizes):
            raise ValueError("pack sizes must be distinct")
        return sorted(value, key=lambda p: p.size)

    def pack(self, size: int) -> PackOption | None:
        return next((p for p in self.packs if p.size == size), None)

    def pack_price(self, pack: PackOption) -> float:
        """What one pack costs, rounded to the cent."""
        return round_cents(pack.size * self.unit_cost * (1 - pack.discount))

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
    starting_cash: float = Field(ge=0, le=MAX_STARTING_CASH)
    min_max_values: MinMaxValues
    # How much the recipe moves what customers will pay: a perfect cup raises it to
    # budget x (1 + quality_swing), a cup they hate lowers it to budget x (1 - quality_swing).
    quality_swing: float = Field(ge=0, le=1)
    # Popularity before the first day with visitors. Each buy chance is scaled by
    # (0.5 + popularity), so 0.5 is neutral. Defaulted so configs saved before it still load.
    starting_popularity: float = Field(default=0.5, ge=0, le=1)
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
            if not 0 <= mult <= MAX_WEATHER_MULTIPLIER:
                raise ValueError(
                    f"weather multiplier for {name} must be within 0-{MAX_WEATHER_MULTIPLIER}"
                )
        return value

    @field_validator("weather_temperature_ranges")
    @classmethod
    def _range_keys(cls, value: dict[str, FloatRange]) -> dict[str, FloatRange]:
        _exact_keys(value, WEATHER_TYPES, "weather_temperature_ranges")
        for name, rng in value.items():
            if rng.min < MIN_TEMPERATURE or rng.max > MAX_TEMPERATURE:
                raise ValueError(
                    f"{name} temperature range must stay within "
                    f"{MIN_TEMPERATURE}..{MAX_TEMPERATURE} °C"
                )
        return value

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
