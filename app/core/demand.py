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

Popularity — carried across the game, fixed for a day, applied on top of P(buy):

    rate        = cups sold / visitors of one day (sold-out visitors count as not sold)
    popularity  = mean(rate of every played day with visitors), else starting_popularity
    final P     = min(1, P(buy) * (0.5 + popularity))

So 0.5 is neutral, 0 halves the chance and 1 raises it by half.

Why a visitor said no — from the one draw ``u`` the engine already makes per
visitor (``buys = u < final P``), so naming the reason draws nothing:

    not_popular   if u < P(buy): they would have bought at a neutral popularity
                  (only possible while popularity < 0.5)
    otherwise     refusal_reason(): too_cheap, too_pricey or the worst ingredient
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


def temperature_score(person: PersonType, temperature: float) -> float:
    """How much this type likes the day's temperature (0-1); its range sets the reach."""
    return triangle_kernel(temperature, person.preferred_degrees, person.denominators.temperature)


def hour_score(person: PersonType, hour: int) -> float:
    """How much this type likes being out at ``hour`` (0-1); the opening hours set the reach."""
    return triangle_kernel(hour, person.preferred_hour, person.denominators.hour)


def spawn_bonus(person: PersonType, hour: int, weather: str, temperature: float) -> float:
    temp_s = temperature_score(person, temperature)
    hour_s = hour_score(person, hour)
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


def ingredient_score(person: PersonType, name: str, amount: float) -> float:
    """How much this type likes ``amount`` of one recipe ingredient (0-1), from its tolerances."""
    below, above = getattr(person.tolerances, name)
    return asymmetric_kernel(amount, getattr(person, f"preferred_{name}"), below, above)


def ingredient_scores(person: PersonType, recipe: dict[str, int]) -> dict[str, float]:
    # Ice, sugar, lemons, in that order: refusal_reason breaks a tie on the first.
    return {
        name: ingredient_score(person, name, recipe[name]) for name in ("ice", "sugar", "lemons")
    }


def recipe_quality(scores: dict[str, float]) -> float:
    return statistics.mean(scores.values())


def wtp_share(quality: float, quality_swing: float) -> float:
    """What a cup of ``quality`` is worth to a customer, as a share of their budget."""
    return 1 + quality_swing * (2 * quality - 1)


def willingness_to_pay(person: PersonType, quality: float, quality_swing: float) -> float:
    return person.average_expense * wtp_share(quality, quality_swing)


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


def popularity(rates: list[float], start: float) -> float:
    """The mean of the daily success rates so far; ``start`` until there is one."""
    return statistics.mean(rates) if rates else start


# The popularity at which buy chances are left as price and recipe make them.
NEUTRAL_POPULARITY = 0.5


def popularity_factor(popularity: float) -> float:
    """What every buy chance is multiplied by: ``0.5 + popularity`` (1 at NEUTRAL_POPULARITY)."""
    return 0.5 + popularity


def apply_popularity(probability: float, popularity: float) -> float:
    """Scale a buy chance by ``popularity_factor``: neutral at 0.5, capped at 1."""
    return min(1.0, probability * popularity_factor(popularity))


NOT_POPULAR = "not_popular"


def refused_for_popularity(draw: float, base: float) -> bool:
    """Whether a refusal was popularity's doing: the visitor's draw would have bought at a
    neutral popularity (0.5), where the chance is ``base`` itself.

    Never true when popularity is 0.5 or more: the chance is then at least ``base``, so a
    draw that refused is at least ``base`` too.
    """
    return draw < base


def refusal_reason(
    person: PersonType, price: float, recipe: dict[str, int], scores: dict[str, float]
) -> str:
    """Why a customer said no to the price and recipe, phrased for the player.

    The engine asks only when popularity did not cause the refusal
    (``refused_for_popularity``). Under the suspicious floor it is the price. Otherwise the price is blamed when
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
