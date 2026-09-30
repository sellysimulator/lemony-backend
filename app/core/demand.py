"""How many people come to the stand, and whether each one buys.

Spawn, per hour and person type (the user's "base + preference bonus"):

    temp_s    = k(temperature, preferred_degrees)
    hour_s    = k(hour, preferred_hour)
    weather_s = 1 if weather == preferred_weather else 0
    bonus     = mean(weather_s, hour_s, temp_s)
    expected  = spawn_per_hour * (1 + bonus) * weather_multiplier[weather]
    count     ~ Poisson(expected)

Buy (the user's formula, unchanged): the mean of four triangle-kernel scores —
price vs average expense (symmetric on purpose: a suspiciously cheap cup is
penalised too), and ice / sugar / lemons vs preference — is the probability.
"""

from __future__ import annotations

import math
import random
import statistics

from .people import PersonType


def triangle_kernel(current: float, preferred: float, denominator: float) -> float:
    """1 at the preference, falling linearly to 0 at ``denominator`` away; clamped to [0, 1]."""
    if denominator <= 0:
        return 1.0 if current == preferred else 0.0
    return min(1.0, max(0.0, 1 - abs(current - preferred) / denominator))


def spawn_bonus(person: PersonType, hour: int, weather: str, temperature: float) -> float:
    d = person.denominators
    temp_s = triangle_kernel(temperature, person.preferred_degrees, d.temperature)
    hour_s = triangle_kernel(hour, person.preferred_hour, d.hour)
    weather_s = 1.0 if weather == person.preferred_weather else 0.0
    return statistics.mean([weather_s, hour_s, temp_s])


def expected_spawn(
    person: PersonType, hour: int, weather: str, temperature: float, weather_multiplier: float
) -> float:
    bonus = spawn_bonus(person, hour, weather, temperature)
    return person.spawn_per_hour * (1 + bonus) * weather_multiplier


def poisson(rng: random.Random, lam: float) -> int:
    """Poisson sample: Knuth for small means, rounded normal approximation above 30."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    threshold = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def buy_scores(person: PersonType, price: float, recipe: dict[str, int]) -> dict[str, float]:
    d = person.denominators
    return {
        "price": triangle_kernel(price, person.average_expense, d.price),
        "ice": triangle_kernel(recipe["ice"], person.preferred_ice, d.ice),
        "sugar": triangle_kernel(recipe["sugar"], person.preferred_sugar, d.sugar),
        "lemons": triangle_kernel(recipe["lemons"], person.preferred_lemons, d.lemons),
    }


def buy_probability(scores: dict[str, float]) -> float:
    return statistics.mean(scores.values())


def refusal_reason(person: PersonType, price: float, recipe: dict[str, int], scores: dict[str, float]) -> str:
    """The lowest-scoring factor, phrased for the player."""
    factor = min(scores, key=lambda key: scores[key])
    if factor == "price":
        return "too_cheap" if price < person.average_expense else "too_pricey"
    preferred = getattr(person, f"preferred_{factor}")
    too_much = recipe[factor] > preferred
    return {
        ("ice", True): "too_much_ice",
        ("ice", False): "needs_more_ice",
        ("sugar", True): "too_sweet",
        ("sugar", False): "not_sweet_enough",
        ("lemons", True): "too_sour",
        ("lemons", False): "needs_more_lemon",
    }[(factor, too_much)]
