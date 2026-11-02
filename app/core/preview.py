"""The morning plan's what-if curve: expected figures for every price on the grid.

Pure and deterministic. Only today's weather is read (a fresh per-operation RNG,
exactly as ``LemonadeGame.view`` gets it); nothing is drawn for visitors or perishing,
and ``game`` is not modified. The figures are expected values built from the same
functions ``run_day`` uses:

    weight(type) = sum over hours of expected_spawn(...)
    p(type, price) = apply_popularity(buy_probability(price, wtp(recipe quality)), popularity)
    chance         = sum(weight * p) / sum(weight)
    expected_cups  = min(sum(weight * p), cups_possible)

When stock binds, ``expected_cups`` is an upper bound on the simulated mean.
"""

from __future__ import annotations

import math
from typing import Any

from .defaults import RECIPE_INGREDIENTS
from .demand import (
    apply_popularity,
    buy_probability,
    expected_spawn,
    ingredient_scores,
    recipe_quality,
    willingness_to_pay,
)
from .engine import DayPlan, LemonadeGame
from .money import round_cents

# Mirrors the `step` of the price input in the frontend's PlanDay.tsx.
PRICE_STEP = 0.05
MAX_POINTS = 201

_EPS = 1e-9


def _price_grid(low: float, high: float) -> list[float]:
    """``round_cents(low + k * step)`` while <= high; the step widens past MAX_POINTS."""
    span = max(0.0, high - low)
    multiple = 1
    while math.floor(span / (PRICE_STEP * multiple) + _EPS) + 1 > MAX_POINTS:
        multiple += 1
    step = PRICE_STEP * multiple
    count = math.floor(span / step + _EPS) + 1
    prices = [round_cents(low + k * step) for k in range(count)]
    return [p for p in prices if p <= high + _EPS]


def _cups_possible(game: LemonadeGame, recipe: dict[str, int], units: dict[str, int]) -> int:
    limits = [game.inventory["cups"].total() + units.get("cups", 0)]
    for name in RECIPE_INGREDIENTS:
        amount = recipe[name]
        if amount:
            limits.append((game.inventory[name].total() + units.get(name, 0)) // amount)
    return max(0, min(limits))


def plan_preview(game: LemonadeGame, plan: DayPlan) -> dict[str, Any]:
    """Expected visitors, chance, cups and margin across the price grid. Raises ``PlanError``."""
    _, units, _ = game._validate_plan(plan)
    recipe = {name: int(plan.recipe[name]) for name in RECIPE_INGREDIENTS}
    config = game.config
    mm = config.min_max_values
    weather, temperature = game.weather_for_day(game.day)
    multiplier = config.weather_multipliers[weather]
    popularity = game.current_popularity()

    weights = [
        sum(
            expected_spawn(person, hour, weather, temperature, multiplier)
            for hour in range(mm.hour.min, mm.hour.max + 1)
        )
        for person in game.people
    ]
    total_weight = sum(weights)
    wtp = [
        willingness_to_pay(
            person, recipe_quality(ingredient_scores(person, recipe)), config.quality_swing
        )
        for person in game.people
    ]

    cups_possible = _cups_possible(game, recipe, units)
    cost_per_cup = game.list_cost_per_cup(recipe)
    prices = _price_grid(mm.price.min, mm.price.max)
    chance: list[float] = []
    expected_cups: list[float] = []
    expected_margin: list[float] = []
    for price in prices:
        buyers = sum(
            weight * apply_popularity(buy_probability(person, price, wtp_i), popularity)
            for person, weight, wtp_i in zip(game.people, weights, wtp, strict=True)
        )
        cups = min(buyers, float(cups_possible))
        chance.append(round(buyers / total_weight, 4) if total_weight > 0 else 0.0)
        expected_cups.append(round(cups, 2))
        expected_margin.append(round_cents(cups * (price - cost_per_cup)))

    best = max(range(len(prices)), key=lambda i: (expected_margin[i], -i))
    return {
        "prices": prices,
        "chance": chance,
        "expected_visitors": round(total_weight, 2),
        "expected_cups": expected_cups,
        "expected_margin": expected_margin,
        "cups_possible": cups_possible,
        "best_price": prices[best],
    }
