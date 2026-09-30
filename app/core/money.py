"""The one rounding rule for money, shared with the frontend (src/utils/money.ts).

Round to whole cents, halves away from zero, after trimming float noise to 12
significant digits: 0.125 is stored as 0.12499999999999999..., and without the
trim it would round down on one side and up on the other. Both sides compute
in the same order on IEEE doubles, so they agree to the cent.

The shared cases in tests/fixtures/rounding_cases.json must pass on both sides.
"""

from __future__ import annotations

import math


def round_cents(value: float) -> float:
    cents = float(f"{value * 100:.12g}")
    return math.copysign(math.floor(abs(cents) + 0.5), cents) / 100 + 0.0
