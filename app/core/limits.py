"""Hard limits on every number a player configures or submits.

Kept apart from ``config_model`` so code that only needs a limit (the config
defaults route, the engine's plan checks) does not depend on the whole config.
Every player-supplied number has an upper bound here; ``tests/test_core/
test_limits.py`` sets each config number far out of range and expects a refusal.
"""

from __future__ import annotations

MAX_NUM_DAYS = 30
MAX_STARTING_CASH = 1_000_000

MAX_PER_CUP = 20  # units of one ingredient in a cup
MIN_TEMPERATURE = -30  # °C, for the temperature range and every weather's range
MAX_TEMPERATURE = 60
MAX_HOUR = 23
MAX_PRICE = 100

MAX_SPAWN_PER_HOUR = 200
MAX_AVERAGE_EXPENSE = 100
MAX_TOLERANCE = 20  # units per cup, for the ingredient tolerances
MAX_PRICE_TOLERANCE_ABOVE = 5  # a share of the budget (500%)
MAX_WEATHER_MULTIPLIER = 3

MAX_UNIT_COST = 100
MAX_PACKS = 8  # pack sizes on sale per ingredient
MAX_PACK_SIZE = 10_000
MAX_PACK_DISCOUNT = 0.9
MAX_FRESH_DAYS = 60
MAX_MAX_DAYS = 61

MAX_PACKS_PER_SIZE = 100  # packs of one size bought in a day
