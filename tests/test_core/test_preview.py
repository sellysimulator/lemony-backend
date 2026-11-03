"""LE-4: `plan_preview`, the expected-value what-if curve for the morning plan.

The expectations below are computed here from the documented formulas
(`core/demand.py`, and the cost per cup written out in `reference_cost`), never by
calling the preview, so a slip in the preview's composition shows up as a mismatch.
"""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from app.core.config_model import GameConfig
from app.core.defaults import RECIPE_INGREDIENTS, default_config
from app.core.demand import (
    apply_popularity,
    buy_probability,
    expected_spawn,
    ingredient_scores,
    recipe_quality,
    willingness_to_pay,
)
from app.core.engine import DayPlan, LemonadeGame, PlanError
from app.core.ingredients import Ingredient
from app.core.money import round_cents
from app.core.preview import PRICE_STEP, plan_preview

RECIPE = {"ice": 1, "sugar": 1, "lemons": 1}
# Plenty of every ingredient for the default demand (about 100-250 visitors a day).
BIG_STOCK = {"ice": {"500": 1}, "sugar": {"500": 1}, "lemons": {"500": 1}, "cups": {"500": 1}}


def make_game(seed: int = 7, edit: Any = None) -> LemonadeGame:
    data = default_config()
    if edit is not None:
        edit(data)
    return LemonadeGame(GameConfig.model_validate(data), seed)


def make_plan(
    purchases: dict[str, dict[str, int]] | None = None,
    recipe: dict[str, int] | None = None,
    price: float = 0.5,
) -> DayPlan:
    return DayPlan(
        purchases=BIG_STOCK if purchases is None else purchases,
        recipe=recipe or dict(RECIPE),
        price=price,
    )


def grid(game: LemonadeGame) -> list[float]:
    mm = game.config.min_max_values.price
    out = []
    k = 0
    while (p := round_cents(mm.min + k * 0.05)) <= mm.max:
        out.append(p)
        k += 1
    return out


def units_bought(purchases: dict[str, dict[str, int]]) -> dict[str, int]:
    return {n: sum(int(s) * c for s, c in packs.items()) for n, packs in purchases.items()}


def reference_cups_possible(game: LemonadeGame, plan: DayPlan) -> int:
    bought = units_bought(plan.purchases)

    def have(name: str) -> int:
        return game.inventory[name].total() + bought.get(name, 0)

    limits = [have("cups")]
    for name in RECIPE_INGREDIENTS:
        if plan.recipe[name]:
            limits.append(have(name) // plan.recipe[name])
    return min(limits)


def reference_cost(game: LemonadeGame, plan: DayPlan) -> float | None:
    """Cost per cup, written out: the cups possible, each ingredient used from held stock
    oldest first at the price paid, then from today's packs dearest per unit first."""
    possible = reference_cups_possible(game, plan)
    if possible == 0:
        return None
    total = 0.0
    for name in ("ice", "sugar", "lemons", "cups"):
        cfg = game.config.ingredients[name]
        today = []
        for size, count in plan.purchases.get(name, {}).items():
            pack = cfg.pack(int(size))
            assert pack is not None
            today.append((int(size) * count, cfg.pack_price(pack) / int(size)))
        today.sort(key=lambda lot: -lot[1])
        held = [(b.qty, game.inventory[name].paid(b)) for b in game.inventory[name].batches]
        need = possible * (1 if name == "cups" else plan.recipe[name])
        for qty, unit in held + today:
            take = min(qty, need)
            total += take * unit
            need -= take
    return round_cents(total / possible)


def reference_spend(game: LemonadeGame, purchases: dict[str, dict[str, int]]) -> float:
    """What the packs cost, written out: each pack's price rounded to the cent, then the sum."""
    total = 0.0
    for name, packs in purchases.items():
        cfg = game.config.ingredients[name]
        for size, count in packs.items():
            pack = cfg.pack(int(size))
            assert pack is not None
            total += round_cents(int(size) * cfg.unit_cost * (1 - pack.discount)) * count
    return round_cents(total)


def reference_can_add(game: LemonadeGame, plan: DayPlan) -> dict[str, dict[str, bool]]:
    """One more pack fits when it keeps the size under the daily cap and the cash covers it."""
    out: dict[str, dict[str, bool]] = {}
    for name in ("ice", "sugar", "lemons", "cups"):
        out[name] = {}
        for pack in game.config.ingredients[name].packs:
            have = plan.purchases.get(name, {}).get(str(pack.size), 0)
            more = copy.deepcopy(plan.purchases)
            more.setdefault(name, {})[str(pack.size)] = have + 1
            fits = reference_spend(game, more) <= game.cash + 1e-9
            out[name][str(pack.size)] = have + 1 <= 100 and fits
    return out


def reference(game: LemonadeGame, plan: DayPlan) -> dict[str, Any]:
    """The spec, written out. Only for the default price range (a grid under 201 prices)."""
    cfg = game.config
    weather, temperature = game.weather_for_day(game.day)
    multiplier = cfg.weather_multipliers[weather]
    popularity = game.current_popularity()
    hours = range(cfg.min_max_values.hour.min, cfg.min_max_values.hour.max + 1)
    prices = grid(game)
    possible = reference_cups_possible(game, plan)
    cost = reference_cost(game, plan)
    weights = {
        p.kind: sum(expected_spawn(p, h, weather, temperature, multiplier) for h in hours)
        for p in game.people
    }
    total = sum(weights.values())
    chance, cups, margin = [], [], []
    for price in prices:
        buyers = 0.0
        for p in game.people:
            wtp = willingness_to_pay(
                p, recipe_quality(ingredient_scores(p, plan.recipe)), cfg.quality_swing
            )
            prob = apply_popularity(buy_probability(p, price, wtp), popularity)
            buyers += weights[p.kind] * prob
        chance.append(round(buyers / total, 4) if total else 0.0)
        sold = min(buyers, possible)
        cups.append(round(sold, 2))
        margin.append(round_cents(sold * (price - cost)) if cost is not None else 0.0)
    spend = reference_spend(game, plan.purchases)
    bought = units_bought(plan.purchases)
    return {
        "prices": prices,
        "chance": chance,
        "expected_visitors": round(total, 2),
        "expected_cups": cups,
        "expected_margin": margin,
        "cups_possible": possible,
        "cost_per_cup": cost,
        "spend": spend,
        "cash_after": round_cents(game.cash - spend),
        "units": {n: bought.get(n, 0) for n in ("ice", "sugar", "lemons", "cups")},
        "can_add": reference_can_add(game, plan),
    }


def assert_matches(got: dict[str, Any], want: dict[str, Any]) -> None:
    assert set(got) == set(want)
    assert got["prices"] == want["prices"]
    assert got["cups_possible"] == want["cups_possible"]
    assert got["cost_per_cup"] == want["cost_per_cup"]
    for key in ("spend", "cash_after", "units", "can_add"):
        assert got[key] == want[key], key
    assert got["expected_visitors"] == pytest.approx(want["expected_visitors"], abs=1e-9)
    for key in ("chance", "expected_cups", "expected_margin"):
        assert len(got[key]) == len(want["prices"])
        assert got[key] == pytest.approx(want[key], abs=1e-9), key


# ------------------------------------------------------------------ formulas
@pytest.mark.parametrize(
    "popularity, swing, recipe",
    [
        (0.5, 0.4, {"ice": 1, "sugar": 1, "lemons": 1}),
        (0.9, 0.4, {"ice": 2, "sugar": 2, "lemons": 3}),
        (0.1, 0.8, {"ice": 0, "sugar": 5, "lemons": 5}),
    ],
)
def test_preview_matches_the_documented_formulas(popularity, swing, recipe):
    def edit(data: dict[str, Any]) -> None:
        data["starting_popularity"] = popularity
        data["quality_swing"] = swing

    game = make_game(edit=edit)
    plan = make_plan(recipe=recipe)
    assert_matches(plan_preview(game, plan), reference(game, plan))


def test_preview_uses_the_played_days_popularity_and_todays_weather(monkeypatch):
    game = make_game()
    game.run_day(make_plan())
    game.acknowledge_day(1)
    assert game.current_popularity() != 0.5  # popularity now comes from a played day
    asked: list[int] = []
    real = game.weather_for_day

    def spy(day: int) -> tuple[str, float]:
        asked.append(day)
        return ("rainy", 3.0) if day == game.day else real(day)

    monkeypatch.setattr(game, "weather_for_day", spy)
    plan = make_plan(price=0.4)
    result = plan_preview(game, plan)
    assert set(asked) == {2}  # only today's weather
    assert_matches(result, reference(game, plan))
    # Rainy and 3 degrees: far fewer visitors than the sunny default would bring.
    monkeypatch.setattr(game, "weather_for_day", lambda day: ("sunny", 30.0))
    assert plan_preview(game, plan)["expected_visitors"] > 2 * result["expected_visitors"]


def test_price_in_the_plan_is_ignored():
    game = make_game()
    assert plan_preview(game, make_plan(price=0.2)) == plan_preview(game, make_plan(price=1.0))


def test_rounding_places():
    result = plan_preview(make_game(), make_plan())
    assert result["expected_visitors"] == round(result["expected_visitors"], 2)
    for key, places in (("chance", 4), ("expected_cups", 2), ("expected_margin", 2)):
        assert all(v == round(v, places) for v in result[key]), key


def test_no_visitors_means_zero_chance_and_cups():
    def edit(data: dict[str, Any]) -> None:
        for prefs in data["people_preferences"].values():
            prefs["spawn_per_hour"] = 0

    result = plan_preview(make_game(edit=edit), make_plan())
    n = len(result["prices"])
    assert result["expected_visitors"] == 0
    assert result["chance"] == [0.0] * n and result["expected_cups"] == [0.0] * n
    assert result["expected_margin"] == [0.0] * n


# --------------------------------------------------------------------- grid
def test_default_grid_is_every_five_cents_including_the_max():
    assert PRICE_STEP == 0.05
    prices = plan_preview(make_game(), make_plan())["prices"]
    assert prices[0] == 0.2 and prices[-1] == 1.0 and len(prices) == 17
    assert prices == [round_cents(0.2 + k * 0.05) for k in range(17)]


def test_grid_starts_at_min_even_when_not_on_a_step():
    def edit(data: dict[str, Any]) -> None:
        data["min_max_values"]["price"] = {"min": 0.22, "max": 0.9}

    prices = plan_preview(make_game(edit=edit), make_plan(price=0.3))["prices"]
    assert prices[0] == 0.22 and prices[1] == 0.27
    assert prices[-1] == 0.87  # 0.92 would pass the max
    assert prices == [round_cents(0.22 + k * 0.05) for k in range(len(prices))]


def price_range(lo: float, hi: float) -> Any:
    def edit(data: dict[str, Any]) -> None:
        data["min_max_values"]["price"] = {"min": lo, "max": hi}

    return edit


def test_201_prices_keep_the_five_cent_step():
    result = plan_preview(make_game(edit=price_range(0.0, 10.0)), make_plan(price=1.0))
    assert len(result["prices"]) == 201
    assert result["prices"][:3] == [0.0, 0.05, 0.1] and result["prices"][-1] == 10.0


@pytest.mark.parametrize(
    "lo, hi, step",
    [
        (0.0, 10.05, 0.10),  # 202 prices at 0.05
        (0.2, 20.2, 0.10),  # 401 at 0.05, 201 at 0.10
        (0.0, 29.9, 0.15),  # 0.10 gives 300 prices, 0.15 gives 200: smallest multiple that fits
    ],
)
def test_a_wide_range_widens_the_step_to_a_multiple_of_five_cents(lo, hi, step):
    result = plan_preview(make_game(edit=price_range(lo, hi)), make_plan(price=1.0))
    prices = result["prices"]
    assert len(prices) <= 201
    assert prices == [round_cents(lo + k * step) for k in range(len(prices))]
    assert prices[-1] <= hi < round_cents(lo + len(prices) * step)
    for key in ("chance", "expected_cups", "expected_margin"):
        assert len(result[key]) == len(prices)


# --------------------------------------------------------------- cups_possible
def test_cups_possible_is_the_scarcest_ingredient_with_stock_and_purchases():
    game = make_game()
    game.inventory["lemons"].add(250)
    game.inventory["cups"].add(1000)
    plan = make_plan(
        purchases={"lemons": {"100": 2}, "ice": {"300": 1}, "sugar": {"700": 1}},
        recipe={"ice": 2, "sugar": 1, "lemons": 3},
    )
    # lemons (250 + 200) // 3 = 150, ice 300 // 2 = 150, sugar 700, cups 1000
    assert plan_preview(game, plan)["cups_possible"] == 150
    plan = make_plan(
        purchases={"lemons": {"100": 2}, "ice": {"500": 1}, "sugar": {"700": 1}},
        recipe={"ice": 2, "sugar": 1, "lemons": 3},
    )
    assert plan_preview(game, plan)["cups_possible"] == 150  # lemons now the only limit


def test_cups_possible_floors_each_ingredient():
    game = make_game()
    plan = make_plan(
        purchases={
            "ice": {"100": 1},
            "sugar": {"100": 1},
            "lemons": {"100": 1},
            "cups": {"500": 1},
        },
        recipe={"ice": 3, "sugar": 2, "lemons": 4},
    )
    assert plan_preview(game, plan)["cups_possible"] == 25  # lemons: 100 // 4; ice gives 33


def test_cups_possible_is_limited_by_the_cups_themselves():
    plan = make_plan(purchases={**BIG_STOCK, "cups": {"100": 1}})
    assert plan_preview(make_game(), plan)["cups_possible"] == 100


def test_an_ingredient_with_a_zero_amount_does_not_limit():
    plan = make_plan(
        purchases={"sugar": {"100": 1}, "lemons": {"100": 1}, "cups": {"100": 1}},
        recipe={"ice": 0, "sugar": 1, "lemons": 1},
    )
    assert plan_preview(make_game(), plan)["cups_possible"] == 100  # no ice bought, none needed


def test_stock_caps_the_expected_cups():
    plan = make_plan(purchases={"ice": {"100": 1}, "sugar": {"100": 1}, "cups": {"100": 1}})
    game = make_game()
    game.inventory["lemons"].add(3)
    result = plan_preview(game, plan)
    assert result["cups_possible"] == 3
    assert max(result["expected_cups"]) <= 3
    uncapped = reference(make_game(), make_plan())  # plenty of stock: same demand
    assert uncapped["expected_cups"][0] > 3
    assert result["expected_cups"][0] == 3  # cheapest price sells the most


# --------------------------------------------------------------- cost per cup
def test_there_is_no_best_price_in_the_reply():
    assert "best_price" not in plan_preview(make_game(), make_plan())


def test_cost_per_cup_is_the_stock_cost_with_discounts_and_drives_the_margin():
    game = make_game()
    game.inventory["lemons"].add(40, 0.02)  # older lemons, cheaper than any pack today
    plan = make_plan(
        purchases={
            "ice": {"700": 1},
            "sugar": {"700": 1},
            "lemons": {"100": 1, "700": 1},
            "cups": {"100": 1},
        },
        recipe={"ice": 1, "sugar": 1, "lemons": 2},
    )
    result = plan_preview(game, plan)
    assert result["cups_possible"] == 100  # the 100 cups
    # 200 lemons: the 40 old ones, then the dearer 100-pack, then 60 of the 20%-off 700-pack.
    lemons = 40 * 0.02 + 100 * 0.05 + 60 * 0.04
    cost = round_cents((100 * 0.008 + 100 * 0.016 + lemons + 100 * 0.04) / 100)
    assert result["cost_per_cup"] == cost
    for price, cups, margin in zip(
        result["prices"], result["expected_cups"], result["expected_margin"], strict=True
    ):
        assert margin == pytest.approx(round_cents(cups * (price - cost)), abs=0.011)


def test_with_nothing_to_make_a_cup_there_is_no_cost_and_no_margin():
    result = plan_preview(make_game(), make_plan(purchases={}))
    assert result["cups_possible"] == 0 and result["cost_per_cup"] is None
    assert set(result["expected_margin"]) == {0.0}


# ------------------------------------------------- the plan's own figures
def test_spend_and_units_are_what_run_day_charges_and_adds():
    game = make_game()
    plan = make_plan(purchases={"lemons": {"300": 2, "100": 1}, "cups": {"700": 1}})
    preview = plan_preview(game, plan)
    record = make_game().run_day(plan)["record"]
    assert preview["spend"] == record["spend"]
    assert preview["units"] == record["purchased"]
    assert preview["cash_after"] == round_cents(game.cash - record["spend"])


def test_can_add_stops_at_the_daily_cap_per_size():
    game = make_game()
    game.cash = 10_000.0
    preview = plan_preview(game, make_plan(purchases={"ice": {"100": 99}}))
    assert preview["can_add"]["ice"]["100"] is True
    preview = plan_preview(game, make_plan(purchases={"ice": {"100": 100}}))
    assert preview["can_add"]["ice"]["100"] is False
    assert preview["can_add"]["ice"]["300"] is True  # another size has its own cap


def test_can_add_stops_where_the_cash_runs_out():
    game = make_game()
    game.cash = 6.0  # 100 cups cost 4.00, 100 lemons 5.00
    preview = plan_preview(game, make_plan(purchases={"cups": {"100": 1}}))
    assert preview["cash_after"] == 2.0
    assert preview["can_add"]["cups"]["100"] is False  # 8.00 > 6.00
    assert preview["can_add"]["sugar"]["100"] is True  # 4.00 + 2.00 fits exactly
    assert preview["can_add"]["lemons"]["100"] is False
    # Exactly what submit_day would accept or refuse.
    for name, size in (("sugar", "100"), ("lemons", "100")):
        plan = make_plan(purchases={"cups": {"100": 1}, name: {size: 1}})
        try:
            game._validate_plan(plan)
            accepted = True
        except PlanError:
            accepted = False
        assert accepted is preview["can_add"][name][size]


# ------------------------------------------------- the player's price
def test_without_a_price_there_is_no_verdict_and_no_point():
    preview = plan_preview(make_game(), make_plan())
    assert not {"price", "price_error", "at_price"} & set(preview)


@pytest.mark.parametrize("price", [0.2, 0.55, 1.0])
def test_at_a_grid_price_the_point_is_the_curve(price):
    game = make_game()
    preview = plan_preview(game, make_plan(), price)
    i = preview["prices"].index(price)
    point = preview["at_price"]
    assert preview["price"] == price and preview["price_error"] is None
    assert point["chance"] == preview["chance"][i]
    assert point["expected_cups"] == preview["expected_cups"][i]
    assert point["expected_margin"] == preview["expected_margin"][i]
    cost = preview["cost_per_cup"]
    assert point["margin_per_cup"] == round_cents(price - cost)
    assert point["margin_share"] == round((price - cost) / price, 4)


def test_an_off_grid_price_is_rounded_as_run_day_charges_it():
    game = make_game()
    preview = plan_preview(game, make_plan(), 0.4349)
    assert preview["price"] == 0.43
    want = plan_preview(game, make_plan(), 0.43)["at_price"]
    assert preview["at_price"] == want
    record = make_game().run_day(make_plan(price=0.4349))["record"]
    assert record["price"] == preview["price"]


@pytest.mark.parametrize("price", [0.19, 1.01, -1.0, float("nan"), float("inf")])
def test_a_price_out_of_range_gets_run_days_message_and_no_point(price):
    game = make_game()
    preview = plan_preview(game, make_plan(), price)
    with pytest.raises(PlanError) as refused:
        game._validate_plan(make_plan(price=price))
    assert preview["price_error"] == str(refused.value) == "Price must be between 0.20 and 1.00"
    assert preview["at_price"] is None and preview["price"] is None
    assert preview["prices"]  # the curve is still there


def test_with_nothing_to_make_a_cup_the_price_has_no_margin_per_cup():
    preview = plan_preview(make_game(), make_plan(purchases={}), 0.5)
    assert preview["at_price"]["margin_per_cup"] is None
    assert preview["at_price"]["margin_share"] is None


def test_a_free_cup_has_a_margin_but_no_share_of_the_price():
    def edit(data: dict[str, Any]) -> None:
        data["min_max_values"]["price"]["min"] = 0.0
        for p in data["people_preferences"].values():
            p["average_expense"] = max(p["average_expense"], 0.0)

    preview = plan_preview(make_game(edit=edit), make_plan(), 0.0)
    assert preview["at_price"]["margin_per_cup"] == -preview["cost_per_cup"]
    assert preview["at_price"]["margin_share"] is None


# ------------------------------------------------------------------- refusals
def test_a_bad_recipe_or_purchase_raises_the_same_plan_error_as_run_day():
    game = make_game()
    cases = [
        make_plan(recipe={"ice": 1, "sugar": 1}),  # lemons missing
        make_plan(recipe={"ice": 1, "sugar": 1, "lemons": 99}),
        make_plan(recipe={"ice": 1, "sugar": 1, "lemons": 0}),  # lemons min is 1
        make_plan(purchases={"gold": {"100": 1}}),
        make_plan(purchases={"ice": {"123": 1}}),
        make_plan(purchases={"ice": {"100": 101}}),
        make_plan(purchases={"lemons": {"700": 100}}),  # over the 100 dollars in hand
    ]
    for plan in cases:
        with pytest.raises(PlanError) as expected:
            game._validate_plan(plan)
        with pytest.raises(PlanError) as got:
            plan_preview(game, plan)
        assert str(got.value) == str(expected.value)


def test_purchases_up_to_the_cash_in_hand_are_accepted():
    game = make_game()
    game.cash = 4.5  # 100 sugar costs exactly 2.00, 100 cups 4.00: 6.00 is too much, 4.00 fits
    plan = make_plan(purchases={"cups": {"100": 1}})
    assert plan_preview(game, plan)["cups_possible"] == 0  # accepted; nothing to put in the cups
    with pytest.raises(PlanError):
        plan_preview(game, make_plan(purchases={"cups": {"100": 1}, "sugar": {"100": 1}}))


# ------------------------------------------------------------------- purity
def test_same_input_same_output_and_the_game_is_untouched():
    game = make_game()
    game.run_day(make_plan())  # some history, stock and perish state to protect
    game.acknowledge_day(1)
    before = copy.deepcopy(game.to_payload())
    view_before = copy.deepcopy(game.view())
    plan = make_plan(price=0.6)
    plan_before = plan.model_dump()
    first = plan_preview(game, plan)
    second = plan_preview(game, plan)
    assert first == second
    assert game.to_payload() == before and game.view() == view_before
    assert plan.model_dump() == plan_before


def test_previews_do_not_change_what_run_day_does():
    plan = make_plan(price=0.6)
    twin, previewed = make_game(seed=21), make_game(seed=21)
    for other in (
        make_plan(recipe={"ice": 3, "sugar": 0, "lemons": 2}),
        plan,
        make_plan(purchases={}),
    ):
        plan_preview(previewed, other)
    assert previewed.run_day(plan) == twin.run_day(plan)
    assert previewed.to_payload() == twin.to_payload()
    # And on the next day too: weather, arrivals and perishing all still replay the same way.
    for g in (twin, previewed):
        g.acknowledge_day(1)
    plan_preview(previewed, plan)
    assert previewed.run_day(make_plan(purchases={})) == twin.run_day(make_plan(purchases={}))


def test_preview_draws_nothing_but_todays_weather(monkeypatch):
    game = make_game()
    purposes: list[str] = []
    real = game._rng

    def spy(day: int, purpose: str) -> random.Random:
        purposes.append(f"{day}:{purpose}")
        return real(day, purpose)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the preview must not draw, perish or consume")

    monkeypatch.setattr(game, "_rng", spy)
    for name in ("random", "uniform", "gauss", "choice", "shuffle", "randint", "sample"):
        monkeypatch.setattr(random, name, boom)
    for method in ("end_of_day_perish", "consume", "add"):
        monkeypatch.setattr(Ingredient, method, boom)
    monkeypatch.setattr("app.core.demand.poisson", boom)
    monkeypatch.setattr("app.core.engine.poisson", boom)
    plan_preview(game, make_plan())
    assert purposes in ([], ["1:weather"])  # today's weather, no customers/perish stream


# --------------------------------------------------- simulated days (acceptance 2)
@pytest.mark.parametrize("weather, temperature", [("sunny", 30.0), ("cloudy", 20.0)])
def test_expected_cups_match_the_mean_of_simulated_days(monkeypatch, weather, temperature):
    monkeypatch.setattr(
        LemonadeGame, "weather_for_day", lambda self, day: (weather, temperature), raising=True
    )
    probe = make_game(seed=0)
    plan = make_plan()
    preview = plan_preview(probe, plan)
    # Stock does not bind: it is several times the visitors of the day.
    assert preview["cups_possible"] >= 2 * preview["expected_visitors"]
    days = 400
    for price in (0.3, 0.55, 0.9):
        index = preview["prices"].index(price)
        expected = preview["expected_cups"][index]
        assert expected > 15  # a real quantity, not a vacuous zero
        total = 0
        for seed in range(days):
            game = make_game(seed=seed)
            record = game.run_day(make_plan(price=price))["record"]
            assert record["sold_out"] == 0
            total += record["buyers"]
        assert total / days == pytest.approx(expected, rel=0.04, abs=1.0), price
