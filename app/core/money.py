"""The one rounding rule for money.

Round to whole cents, halves away from zero, after trimming float noise to 12
significant digits: 0.125 is stored as 0.12499999999999999..., and without the
trim it would round down. Only the server rounds: every money figure it sends is
already rounded, so the client prints it with two decimals and rounds nothing.
"""

from __future__ import annotations

import math


def round_cents(value: float) -> float:
    cents = float(f"{value * 100:.12g}")
    return math.copysign(math.floor(abs(cents) + 0.5), cents) / 100 + 0.0
