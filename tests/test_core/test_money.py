"""The one money rounding rule, and the pack prices it gives.

Only the server rounds money: every figure it sends is already rounded, and the client
just prints it. Halves go away from zero after float noise is trimmed to 12 digits.
"""

import pytest

from app.core.config_model import IngredientConfig
from app.core.money import round_cents

ROUND_CENTS = [
    (0.125, 0.13),
    (0.135, 0.14),
    (0.145, 0.15),
    (1.005, 1.01),
    (2.675, 2.68),
    (1.255, 1.26),
    (0.124999, 0.12),
    (0.0049, 0.0),
    (0.005, 0.01),
    (-0.125, -0.13),
    (-2.675, -2.68),
    (-0.004, 0.0),
    (0.0, 0.0),
    (12345.675, 12345.68),
    (999999.995, 1000000.0),
    (33.25, 33.25),
    (0.30000000000000004, 0.3),
]

# (size, unit cost, discount, price of the pack)
PACK_PRICE = [
    (3, 0.07, 0.1, 0.19),
    (500, 0.15, 0.2, 60.0),
    (100, 0.05, 0.05, 4.75),
    (25, 0.01, 0.5, 0.13),
    (7, 0.15, 0.5, 0.53),
    (1, 0.25, 0.5, 0.13),
    (333, 0.03, 0.0, 9.99),
    (10000, 99.99, 0.9, 99990.0),
]


@pytest.mark.parametrize("value, expected", ROUND_CENTS, ids=str)
def test_round_cents(value, expected):
    assert round_cents(value) == expected


def test_round_cents_never_gives_negative_zero():
    assert str(round_cents(-0.004)) == "0.0"


@pytest.mark.parametrize("size, unit_cost, discount, expected", PACK_PRICE)
def test_pack_price(size, unit_cost, discount, expected):
    ing = IngredientConfig(
        unit_cost=unit_cost,
        packs=[{"size": size, "discount": discount}],
        fresh_days=0,
        max_days=1,
    )
    assert ing.pack_price(ing.packs[0]) == expected
