"""How many people come to the stand, and whether each one buys.

Spawn, per hour and person type (the user's "base + preference bonus"):

    temp_s    = k(temperature, preferred_degrees)
    hour_s    = k(hour, preferred_hour)
    weather_s = 1 if weather == preferred_weather else 0
    bonus     = mean(weather_s, hour_s, temp_s)
    expected  = spawn_per_hour * (1 + bonus) * weather_multiplier[weather]
    count     ~ Poisson(expected)

Buy — the recipe sets what a customer is willing to pay, the price is compared
against it:

    score_i  = 1 at the favourite amount of ingredient i, falling linearly to 0
               at tolerance.below units under it / tolerance.above units over it
    quality  = mean(score_ice, score_sugar, score_lemons)
    wtp      = budget * (1 + quality_swing * (2 * quality - 1))
    spread   = tolerance.price.above * budget / ln(19)
    P(buy)   = 1 / (1 + exp((price - wtp) / spread))

So half of a type buys at exactly ``wtp``, 95 % at ``wtp - above * budget`` and
5 % at ``wtp + above * budget``. A price under ``budget * (1 - tolerance.price.below)``
looks suspicious: P(buy) is scaled by ``price / that floor``.
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


def asymmetric_kernel(current: float, preferred: float, below: float, above: float) -> float:
    """Like ``triangle_kernel`` with a separate reach under and over the preference."""
    return triangle_kernel(current, preferred, below if current < preferred else above)


def ingredient_scores(person: PersonType, recipe: dict[str, int]) -> dict[str, float]:
    t = person.tolerances
    return {
        "ice": asymmetric_kernel(recipe["ice"], person.preferred_ice, *t.ice),
        "sugar": asymmetric_kernel(recipe["sugar"], person.preferred_sugar, *t.sugar),
        "lemons": asymmetric_kernel(recipe["lemons"], person.preferred_lemons, *t.lemons),
    }


def recipe_quality(scores: dict[str, float]) -> float:
    return statistics.mean(scores.values())


def willingness_to_pay(person: PersonType, quality: float, quality_swing: float) -> float:
    return person.average_expense * (1 + quality_swing * (2 * quality - 1))


def cheap_floor(person: PersonType) -> float:
    """Below this price the cup looks suspicious."""
    return person.average_expense * (1 - person.tolerances.price[0])


def buy_probability(person: PersonType, price: float, wtp: float) -> float:
    spread = person.tolerances.price[1] * person.average_expense / math.log(19)
    if spread <= 0:
        probability = 1.0 if price <= wtp else 0.0
    else:
        z = max(-60.0, min(60.0, (price - wtp) / spread))
        probability = 1 / (1 + math.exp(z))
    floor = cheap_floor(person)
    if price < floor:
        probability *= price / floor
    return probability


def refusal_reason(
    person: PersonType, price: float, recipe: dict[str, int], scores: dict[str, float]
) -> str:
    """Why a customer said no, phrased for the player.

    Under the suspicious floor it is the price. Otherwise the price is blamed when
    it sits further over the budget (in units of the price tolerance) than the
    worst ingredient sits under a perfect score; else that ingredient is.
    """
    if price < cheap_floor(person):
        return "too_cheap"
    factor = min(scores, key=lambda key: scores[key])
    budget, above = person.average_expense, person.tolerances.price[1]
    price_pressure = (price - budget) / (above * budget) if budget > 0 else 1.0
    if scores[factor] >= 1 or price_pressure >= 1 - scores[factor]:
        return "too_pricey"
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
