"""The rounding rules: money and the pack prices it gives, and every other game
quantity.

Only the server rounds money: every figure it sends is already rounded, and the client
just prints it. Halves go away from zero after float noise is trimmed to 12 digits.
Every other quantity rounded to a step (a customer count, a temperature) rounds halves
up, never to even as Python's `round()` does.
"""

import random

import pytest

from app.core import demand
from app.core.config_model import GameConfig, IngredientConfig
from app.core.defaults import default_config
from app.core.money import half_up, round_cents
from app.core.weather import day_temperature

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


@pytest.mark.parametrize(
    "value, expected",
    [
        (0.5, 1),
        (1.5, 2),
        (2.5, 3),
        (30.5, 31),
        (2.4999, 2),
        (-2.5, -2),
        (-2.6, -3),
        (0.0, 0),
    ],
)
def test_half_up(value, expected):
    assert half_up(value) == expected


def test_a_poisson_count_on_a_half_rounds_up(monkeypatch):
    rng = random.Random(1)
    monkeypatch.setattr(rng, "gauss", lambda mu, sigma: 40.5)
    assert demand.poisson(rng, 40.0) == 41  # round() gives 40, the even neighbour


def test_a_temperature_on_a_half_step_rounds_up():
    config = GameConfig.model_validate(default_config())
    # 20.25 is halfway between 20.0 and 20.5: round() gives 20.0, the even multiple.
    assert day_temperature(config, 20.25) == 20.5
    assert day_temperature(config, 20.75) == 21.0
