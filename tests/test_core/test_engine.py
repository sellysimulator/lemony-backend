import random

import pytest
from pydantic import ValidationError

from app.core.config_model import GameConfig
from app.core.defaults import default_config
from app.core.demand import buy_scores, expected_spawn, poisson, triangle_kernel
from app.core.engine import DayPlan, LemonadeGame, PlanError
from app.core.ingredients import Batch, Ingredient
from app.core.people import build_people
from app.core.weather import roll_day_weather


def cfg(**overrides):
    data = default_config()
    data.update(overrides)
    return GameConfig.model_validate(data)


def plan(**kw):
    base = {
        "purchases": {"ice": {"100": 1}, "sugar": {"100": 1}, "lemons": {"100": 1}, "cups": {"100": 1}},
        "price": 0.6,
        "recipe": {"ice": 2, "sugar": 2, "lemons": 3},
    }
    base.update(kw)
    return DayPlan.model_validate(base)


# ---------------------------------------------------------------- config
def test_defaults_validate():
    cfg()


def test_weather_gap_rejected():
    data = default_config()
    data["weather_temperature_ranges"]["rainy"] = {"min": 6, "max": 15}
    with pytest.raises(ValidationError, match="gap starting at 5"):
        GameConfig.model_validate(data)


def test_weather_gap_at_start_rejected():
    data = default_config()
    data["weather_temperature_ranges"]["snowy"] = {"min": 2, "max": 5}
    with pytest.raises(ValidationError, match="gap"):
        GameConfig.model_validate(data)


def test_preference_outside_range_rejected():
    data = default_config()
    data["people_preferences"]["Adult"]["preferred_hour"] = 18
    with pytest.raises(ValidationError, match="preferred hour"):
        GameConfig.model_validate(data)


def test_type_names_locked():
    data = default_config()
    data["people_preferences"]["Toddler"] = data["people_preferences"].pop("Child")
    with pytest.raises(ValidationError, match="exactly these keys"):
        GameConfig.model_validate(data)


def test_num_days_bounds():
    with pytest.raises(ValidationError):
        cfg(num_days=31)


# ------------------------------------------------------------ perishing
def test_hazard_fresh_ramp():
    sugar = Ingredient("sugar", 0.1, fresh_days=5, max_days=10, never_perishes=False)
    assert [sugar.hazard(d) for d in range(1, 11)] == [0, 0, 0, 0, 0, 0.2, 0.4, 0.6, 0.8, 1]


def test_ice_gone_first_night_and_cups_never():
    rng = random.Random(1)
    ice = Ingredient("ice", 0.05, 0, 1, False, [Batch(100)])
    assert ice.end_of_day_perish(rng) == (100, pytest.approx(5.0)) and ice.total() == 0
    cups = Ingredient("cups", 0.05, 30, 31, True, [Batch(100)])
    for _ in range(60):
        assert cups.end_of_day_perish(rng) == (0, 0.0)
    assert cups.total() == 100


def test_sugar_all_gone_by_max_days():
    rng = random.Random(2)
    sugar = Ingredient("sugar", 0.1, 5, 10, False, [Batch(500)])
    lost = [sugar.end_of_day_perish(rng)[0] for _ in range(10)]
    assert lost[:5] == [0] * 5
    assert sum(lost) == 500


def test_fifo_consume():
    ing = Ingredient("lemons", 0.1, 3, 7, False, [Batch(5, age=3), Batch(10, age=0)])
    ing.consume(7)
    assert [(b.qty, b.age) for b in ing.batches] == [(8, 0)]


def test_consume_and_perish_valued_at_price_paid():
    ing = Ingredient("lemons", 0.1, 0, 1, False, [Batch(5, age=0, unit_cost=0.08), Batch(10, age=0)])
    assert ing.consume(7) == pytest.approx(5 * 0.08 + 2 * 0.1)
    lost, value = ing.end_of_day_perish(random.Random(0))
    assert lost == 8 and value == pytest.approx(0.8)


# ---------------------------------------------------------------- packs
def test_pack_price_applies_its_discount():
    lemons = cfg().ingredients["lemons"]
    pack = lemons.pack(500)
    assert pack is not None and pack.discount == pytest.approx(0.2)
    assert lemons.pack_price(pack) == pytest.approx(60.0)  # 500 x 0.15 x 0.8
    assert lemons.pack(75) is None


def test_legacy_pack_sizes_read_as_undiscounted_packs():
    data = default_config()
    for ing in data["ingredients"].values():
        del ing["packs"]
        ing["pack_sizes"] = [100, 50]
    lemons = GameConfig.model_validate(data).ingredients["lemons"]
    assert [(p.size, p.discount) for p in lemons.packs] == [(50, 0.0), (100, 0.0)]


@pytest.mark.parametrize(
    "packs, message",
    [
        ([{"size": 50, "discount": 0.95}], "between 0% and 90%"),
        ([{"size": 50, "discount": -0.1}], "between 0% and 90%"),
        ([{"size": 0, "discount": 0}], "between 1 and 10000"),
        ([{"size": 50, "discount": 0}, {"size": 50, "discount": 0.1}], "distinct"),
        ([], "at least 1"),
    ],
)
def test_bad_packs_rejected(packs, message):
    data = default_config()
    data["ingredients"]["sugar"]["packs"] = packs
    with pytest.raises(ValidationError, match=message):
        GameConfig.model_validate(data)


def test_purchase_uses_pack_discounts_and_cost_per_cup_uses_price_paid():
    game = LemonadeGame(cfg(), seed=11)
    cost, units, lots = game.purchase_cost({"lemons": {"500": 1, "50": 2}})
    assert units["lemons"] == 600
    assert cost == pytest.approx(60.0 + 2 * 7.5)
    assert lots["lemons"] == [(100, pytest.approx(0.15)), (500, pytest.approx(0.12))]

    big = {name: {"500": 1} for name in ("ice", "sugar", "lemons", "cups")}
    game = LemonadeGame(cfg(starting_cash=500), seed=11)
    record = game.run_day(plan(purchases=big))["record"]
    assert record["buyers"] > 0
    # Everything came from 20%-off packs, so a cup costs 80% of list price.
    assert record["cost_per_cup"] == pytest.approx(0.8 * game.list_cost_per_cup(record["recipe"]), abs=0.01)


# --------------------------------------------------------------- demand
def test_kernel_clamped():
    assert triangle_kernel(50, 15, 25) == 0
    assert triangle_kernel(15, 15, 25) == 1


def test_spawn_peaks_at_preferred_hour_and_snow_cuts():
    people = {p.kind: p for p in build_people(cfg())}
    child = people["Child"]
    at_noon = expected_spawn(child, 12, "sunny", 30, 1.0)
    at_nine = expected_spawn(child, 9, "sunny", 30, 1.0)
    assert at_noon == pytest.approx(10.0)
    assert at_nine < at_noon
    snowy = expected_spawn(child, 12, "snowy", 2, 0.2)
    assert snowy < child.spawn_per_hour


def test_price_kernel_symmetric():
    adult = {p.kind: p for p in build_people(cfg())}["Adult"]
    recipe = {"ice": 1, "sugar": 2, "lemons": 4}
    cheap = buy_scores(adult, 0.40, recipe)["price"]
    pricey = buy_scores(adult, 1.20, recipe)["price"]
    assert cheap == pytest.approx(pricey) and cheap < 1


def test_poisson_mean():
    rng = random.Random(3)
    samples = [poisson(rng, 7.5) for _ in range(4000)]
    assert sum(samples) / len(samples) == pytest.approx(7.5, rel=0.05)


def test_weather_matches_range():
    c = cfg()
    rng = random.Random(4)
    for _ in range(200):
        w, t = roll_day_weather(c, rng)
        r = c.weather_temperature_ranges[w]
        assert r.min <= t <= r.max


# ---------------------------------------------------------------- engine
def test_day_is_deterministic_and_roundtrips():
    a = LemonadeGame(cfg(), seed=42)
    b = LemonadeGame.from_payload(LemonadeGame(cfg(), seed=42).to_payload())
    ra, rb = a.run_day(plan()), b.run_day(plan())
    assert ra == rb
    assert LemonadeGame.from_payload(a.to_payload()).to_payload() == a.to_payload()


def test_overspend_rejected():
    game = LemonadeGame(cfg(starting_cash=5), seed=1)
    with pytest.raises(PlanError, match="only have"):
        game.run_day(plan())


def test_invalid_pack_size_rejected():
    game = LemonadeGame(cfg(), seed=1)
    with pytest.raises(PlanError, match="packs of 7"):
        game.run_day(plan(purchases={"ice": {"7": 1}}))


def test_stock_out_produces_sold_out():
    game = LemonadeGame(cfg(), seed=5)
    result = game.run_day(plan(purchases={"ice": {"50": 1}, "sugar": {"50": 1}, "lemons": {"50": 1}, "cups": {"50": 1}}))
    rec = result["record"]
    assert rec["buyers"] <= 16  # 50 lemons / 3 per cup
    assert rec["sold_out"] > 0
    assert all(e["outcome"] in {"bought", "sold_out", "refused"} for e in result["events"])


def test_money_and_inventory_accounting():
    game = LemonadeGame(cfg(), seed=9)
    rec = game.run_day(plan())["record"]
    assert rec["spend"] == pytest.approx(35.0 * 0.95)  # four 100-packs at 5% off
    assert rec["cash_end"] == pytest.approx(50 - rec["spend"] + rec["revenue"])
    assert rec["revenue"] == pytest.approx(rec["buyers"] * 0.6)
    assert rec["perished"]["ice"] == 100 - rec["buyers"] * 2  # all remaining ice melts
    assert rec["perished"]["cups"] == 0
    events = [e["arrive_min"] for e in game.last_day_events]
    assert events == sorted(events)


def test_day_lifecycle_and_finish():
    game = LemonadeGame(cfg(num_days=2), seed=1)
    game.run_day(plan())
    assert game.phase == "played"
    with pytest.raises(PlanError):
        game.run_day(plan())
    assert game.acknowledge_day(1) and game.day == 2 and game.phase == "planning"
    game.run_day(plan(purchases={}))
    assert game.phase == "finished"
    s = game.summary()
    assert s["days_played"] == 2 and s["total_profit"] == pytest.approx(game.cash - 50)
