"""Money rounding must match the frontend exactly; both read the same cases."""

import json
from pathlib import Path

import pytest

from app.core.config_model import IngredientConfig
from app.core.money import round_cents

FIXTURE = Path(__file__).parent.parent / "fixtures" / "rounding_cases.json"
FRONTEND_COPY = (
    Path(__file__).parents[3]
    / "Lemony_Frontend"
    / "src"
    / "__tests__"
    / "fixtures"
    / "rounding_cases.json"
)
CASES = json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("case", CASES["round_cents"], ids=lambda c: str(c["value"]))
def test_round_cents(case):
    assert round_cents(case["value"]) == case["expected"]


@pytest.mark.parametrize(
    "case", CASES["pack_price"], ids=lambda c: f'{c["size"]}x{c["unit_cost"]}-{c["discount"]}'
)
def test_pack_price(case):
    ing = IngredientConfig(
        unit_cost=case["unit_cost"],
        packs=[{"size": case["size"], "discount": case["discount"]}],
        fresh_days=0,
        max_days=1,
    )
    assert ing.pack_price(ing.packs[0]) == case["expected"]


@pytest.mark.skipif(
    not FRONTEND_COPY.exists(), reason="frontend repo not checked out next to the backend"
)
def test_fixture_matches_frontend_copy():
    assert json.loads(FRONTEND_COPY.read_text()) == CASES
