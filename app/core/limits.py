"""Hard limits on every number a player configures or submits.

Kept apart from ``config_model`` so code that only needs a limit (the config
defaults route, the engine's plan checks) does not depend on the whole config.
Every player-supplied number has an upper bound here; ``tests/test_core/
test_limits.py`` sets each config number far out of range and expects a refusal.

``public_limits()`` is what the client reads (``GET /config/defaults`` and the
game state's ``limits``), so the config screen, the plan screen and the help
never hand-copy a bound.
"""

from __future__ import annotations

MIN_NUM_DAYS = 1
MAX_NUM_DAYS = 30
MAX_STARTING_CASH = 1_000_000

MAX_PER_CUP = 20  # units of one ingredient in a cup
MIN_TEMPERATURE = -30  # °C, for the temperature range and every weather's range
MAX_TEMPERATURE = 60
MAX_HOUR = 23
MAX_PRICE = 100
# A share from 0 to 1: the recipe effect on price, starting popularity, price tolerance below.
MAX_SHARE = 1

MAX_SPAWN_PER_HOUR = 200
MAX_AVERAGE_EXPENSE = 100
MAX_TOLERANCE = 20  # units per cup, for the ingredient tolerances
MAX_PRICE_TOLERANCE_ABOVE = 5  # a share of the budget (500%); must be above 0
MAX_WEATHER_MULTIPLIER = 3

MAX_UNIT_COST = 100
MIN_PACKS = 1  # pack sizes on sale per ingredient
MAX_PACKS = 8
MIN_PACK_SIZE = 1
MAX_PACK_SIZE = 10_000
MAX_PACK_DISCOUNT = 0.9
MAX_FRESH_DAYS = 60
MIN_MAX_DAYS = 1
MAX_MAX_DAYS = 61

MAX_PACKS_PER_SIZE = 100  # packs of one size bought in a day

# The step of the price input on the plan screen, and of the what-if curve's price grid.
PRICE_STEP = 0.05


def _range(low: float, high: float) -> dict[str, float]:
    return {"min": low, "max": high}


def public_limits() -> dict[str, dict[str, float] | float]:
    """Every bound the client shows or enforces, as inclusive ``{min, max}`` ranges.

    The one exception is ``price_tolerance_above``, whose minimum is exclusive (it must
    be above 0). Recipe amounts, hours, temperatures and prices also have to sit
    inside the config's own ranges; those are checked by ``GameConfig``.
    """
    return {
        "num_days": _range(MIN_NUM_DAYS, MAX_NUM_DAYS),
        "starting_cash": _range(0, MAX_STARTING_CASH),
        "per_cup": _range(0, MAX_PER_CUP),
        "temperature": _range(MIN_TEMPERATURE, MAX_TEMPERATURE),
        "hour": _range(0, MAX_HOUR),
        "price": _range(0, MAX_PRICE),
        "quality_swing": _range(0, MAX_SHARE),
        "starting_popularity": _range(0, MAX_SHARE),
        "spawn_per_hour": _range(0, MAX_SPAWN_PER_HOUR),
        "average_expense": _range(0, MAX_AVERAGE_EXPENSE),
        "tolerance": _range(0, MAX_TOLERANCE),
        "price_tolerance_below": _range(0, MAX_SHARE),
        "price_tolerance_above": _range(0, MAX_PRICE_TOLERANCE_ABOVE),
        "weather_multiplier": _range(0, MAX_WEATHER_MULTIPLIER),
        "unit_cost": _range(0, MAX_UNIT_COST),
        "packs": _range(MIN_PACKS, MAX_PACKS),
        "pack_size": _range(MIN_PACK_SIZE, MAX_PACK_SIZE),
        "pack_discount": _range(0, MAX_PACK_DISCOUNT),
        "fresh_days": _range(0, MAX_FRESH_DAYS),
        "max_days": _range(MIN_MAX_DAYS, MAX_MAX_DAYS),
        "packs_per_size": _range(0, MAX_PACKS_PER_SIZE),
        "price_step": PRICE_STEP,
    }
