"""Customer types, their spawn denominators and their buying tolerances.

A spawn denominator is the largest distance between a preference (hour,
temperature) and either end of its configured range, so the triangle kernel
reaches 0 exactly at the far end. Buying does not use the ranges: each person
type carries its own ``(below, above)`` tolerance per ingredient (units per
cup) and for price (a share of its budget). Both are computed once per game.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config_model import GameConfig, Tolerance
from .defaults import PERSON_TYPES


def largest_diff(lo: float, hi: float, preferred: float) -> float:
    return max(preferred - lo, hi - preferred)


@dataclass(frozen=True)
class Denominators:
    temperature: float
    hour: float


@dataclass(frozen=True)
class Tolerances:
    """``(below, above)`` per factor. Ingredients in units; price as a share of the budget."""

    ice: tuple[float, float]
    sugar: tuple[float, float]
    lemons: tuple[float, float]
    price: tuple[float, float]


@dataclass(frozen=True)
class PersonType:
    kind: str
    spawn_per_hour: float
    average_expense: float
    preferred_degrees: float
    preferred_weather: str
    preferred_ice: int
    preferred_sugar: int
    preferred_lemons: int
    preferred_hour: int
    denominators: Denominators
    tolerances: Tolerances


def _pair(t: Tolerance) -> tuple[float, float]:
    return (t.below, t.above)


def build_people(config: GameConfig) -> list[PersonType]:
    mm = config.min_max_values
    people = []
    for kind in PERSON_TYPES:
        p = config.people_preferences[kind]
        t = p.tolerances
        people.append(
            PersonType(
                kind=kind,
                spawn_per_hour=p.spawn_per_hour,
                average_expense=p.average_expense,
                preferred_degrees=p.preferred_degrees,
                preferred_weather=p.preferred_weather,
                preferred_ice=p.preferred_ice,
                preferred_sugar=p.preferred_sugar,
                preferred_lemons=p.preferred_lemons,
                preferred_hour=p.preferred_hour,
                denominators=Denominators(
                    temperature=largest_diff(
                        mm.temperature.min, mm.temperature.max, p.preferred_degrees
                    ),
                    hour=largest_diff(mm.hour.min, mm.hour.max, p.preferred_hour),
                ),
                tolerances=Tolerances(
                    ice=_pair(t.ice),
                    sugar=_pair(t.sugar),
                    lemons=_pair(t.lemons),
                    price=_pair(t.price),
                ),
            )
        )
    return people
