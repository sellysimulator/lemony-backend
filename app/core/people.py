"""Customer types and their preference denominators.

A denominator is the largest distance between a preference and either end of
its configured range, so the triangle kernel reaches 0 exactly at the far end.
They are computed once per game.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config_model import GameConfig
from .defaults import PERSON_TYPES


def largest_diff(lo: float, hi: float, preferred: float) -> float:
    return max(preferred - lo, hi - preferred)


@dataclass(frozen=True)
class Denominators:
    ice: float
    sugar: float
    lemons: float
    temperature: float
    hour: float
    price: float


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


def build_people(config: GameConfig) -> list[PersonType]:
    mm = config.min_max_values
    people = []
    for kind in PERSON_TYPES:
        p = config.people_preferences[kind]
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
                    ice=largest_diff(mm.ice.min, mm.ice.max, p.preferred_ice),
                    sugar=largest_diff(mm.sugar.min, mm.sugar.max, p.preferred_sugar),
                    lemons=largest_diff(mm.lemons.min, mm.lemons.max, p.preferred_lemons),
                    temperature=largest_diff(
                        mm.temperature.min, mm.temperature.max, p.preferred_degrees
                    ),
                    hour=largest_diff(mm.hour.min, mm.hour.max, p.preferred_hour),
                    price=largest_diff(mm.price.min, mm.price.max, p.average_expense),
                ),
            )
        )
    return people
