"""The morning plan's what-if curve: expected figures for every price on the grid.

Pure and deterministic. Only today's weather is read (a fresh per-operation RNG,
exactly as ``LemonadeGame.view`` gets it); nothing is drawn for visitors or perishing,
and ``game`` is not modified. The figures are expected values built from the same
functions ``run_day`` uses:

    weight(type) = sum over hours of expected_spawn(...)
    p(type, price) = apply_popularity(buy_probability(price, wtp(recipe quality)), popularity)
    chance         = sum(weight * p) / sum(weight)
    expected_cups  = min(sum(weight * p), cups_possible)
    expected_margin = expected_cups * (price - cost_per_cup)

``cups_possible`` and ``cost_per_cup`` come from ``LemonadeGame.cup_cost``: the cups
the stock plus the planned packs make, and their average cost (the one meaning of
cost per cup, see ``engine``). When stock binds, ``expected_cups`` is an upper bound
on the simulated mean.

The plan's own figures come from the engine's checks too: ``spend`` and ``units`` from
``purchase_cost``, ``cash_after`` from ``cash_after``, and ``can_add`` says, per pack,
whether one more passes ``purchase_cost`` (the packs-per-size limit) and ``affordable``.
Given the player's ``price``, ``validate_price`` judges it (``price_error``) and
``at_price`` holds the curve's figures at exactly that price, with the margin per cup.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .defaults import INGREDIENT_NAMES, RECIPE_INGREDIENTS
from .demand import (
    apply_popularity,
    buy_probability,
    expected_spawn,
    ingredient_scores,
    recipe_quality,
    willingness_to_pay,
)
from .engine import DayPlan, LemonadeGame, PlanError
from .limits import PRICE_STEP
from .money import round_cents

__all__ = ["MAX_POINTS", "PRICE_STEP", "plan_preview"]

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


def _can_add(
    game: LemonadeGame, purchases: dict[str, dict[str, int]]
) -> dict[str, dict[str, bool]]:
    """Per ingredient and pack size: whether the plan with one more of that pack still passes
    the engine's purchase rules and fits the cash."""
    counts: dict[str, dict[int, int]] = {name: {} for name in INGREDIENT_NAMES}
    for name, packs in purchases.items():
        for size, count in packs.items():
            counts[name][int(size)] = counts[name].get(int(size), 0) + count
    out: dict[str, dict[str, bool]] = {}
    for name in INGREDIENT_NAMES:
        out[name] = {}
        for pack in game.config.ingredients[name].packs:
            more = {n: {str(s): c for s, c in sizes.items()} for n, sizes in counts.items()}
            more[name][str(pack.size)] = counts[name].get(pack.size, 0) + 1
            try:
                cost, _, _ = game.purchase_cost(more)
            except PlanError:
                out[name][str(pack.size)] = False
            else:
                out[name][str(pack.size)] = game.affordable(cost)
    return out


def plan_preview(game: LemonadeGame, plan: DayPlan, price: float | None = None) -> dict[str, Any]:
    """Expected visitors, chance, cups and margin across the price grid, and the plan's own
    figures. ``plan.price`` is ignored; ``price``, when given, is judged and priced.
    Raises ``PlanError`` for a recipe or purchases ``run_day`` would refuse."""
    spend, units, lots = game.validate_purchases(plan)
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

    cups_possible, cost_per_cup = game.cup_cost(recipe, lots)

    def point(at: float) -> tuple[float, float, float]:
        """(chance, expected cups, expected margin) at the price ``at``."""
        buyers = sum(
            weight * apply_popularity(buy_probability(person, at, wtp_i), popularity)
            for person, weight, wtp_i in zip(game.people, weights, wtp, strict=True)
        )
        cups = min(buyers, float(cups_possible))
        chance = round(buyers / total_weight, 4) if total_weight > 0 else 0.0
        # No cup can be made without a cost, so no stock means no margin.
        margin = cups * (at - cost_per_cup) if cost_per_cup is not None else 0.0
        return chance, round(cups, 2), round_cents(margin)

    prices = _price_grid(mm.price.min, mm.price.max)
    points = [point(p) for p in prices]
    result: dict[str, Any] = {
        "prices": prices,
        "chance": [c for c, _, _ in points],
        "expected_visitors": round(total_weight, 2),
        "expected_cups": [cups for _, cups, _ in points],
        "expected_margin": [m for _, _, m in points],
        "cups_possible": cups_possible,
        "cost_per_cup": cost_per_cup,
        "spend": spend,
        "cash_after": game.cash_after(spend),
        "units": units,
        "can_add": _can_add(game, plan.purchases),
    }
    if price is not None:
        result.update(_at_price(game, price, cost_per_cup, point))
    return result


Point = Callable[[float], tuple[float, float, float]]


def _at_price(
    game: LemonadeGame, price: float, cost_per_cup: float | None, point: Point
) -> dict[str, Any]:
    """The player's price judged as ``run_day`` judges it, and the curve at exactly it."""
    try:
        game.validate_price(price)
    except PlanError as exc:
        return {"price": None, "price_error": str(exc), "at_price": None}
    charged = round_cents(price)  # what run_day charges
    chance, cups, margin = point(charged)
    per_cup = None if cost_per_cup is None else round_cents(charged - cost_per_cup)
    return {
        "price": charged,
        "price_error": None,
        "at_price": {
            "chance": chance,
            "expected_cups": cups,
            "expected_margin": margin,
            # Price less cost per cup, and the share of the price it keeps.
            "margin_per_cup": per_cup,
            "margin_share": (
                round((charged - cost_per_cup) / charged, 4)
                if cost_per_cup is not None and charged > 0
                else None
            ),
        },
    }
