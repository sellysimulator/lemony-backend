"""The rounding rules: money (`round_cents`) and every other game quantity (`half_up`).

Python's `round()` rounds halves to even (banker's rounding: 2.5 -> 2, 3.5 -> 4), so
no game quantity uses it; a quantity rounded to a step (a customer count, a
temperature to 0.5 °C) rounds halves up with `half_up`.

Money: round to whole cents, halves away from zero, after trimming float noise to 12
significant digits: 0.125 is stored as 0.12499999999999999..., and without the
trim it would round down. Only the server rounds: every money figure it sends is
already rounded, so the client prints it with two decimals and rounds nothing.
"""

from __future__ import annotations

import math


def round_cents(value: float) -> float:
    cents = float(f"{value * 100:.12g}")
    return math.copysign(math.floor(abs(cents) + 0.5), cents) / 100 + 0.0


def half_up(value: float) -> int:
    """The nearest whole number, halves up (towards +inf): 2.5 -> 3, -2.5 -> -2."""
    return math.floor(value + 0.5)
