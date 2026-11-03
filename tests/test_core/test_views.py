"""What the client shows beside the engine's records: day views, the playback figures on
every event, the summary's totals and the state's plan screen figures.

The client derives none of these, so each is checked here against the engine's own
accounting (its stock, its record), never against a copy of the client's old maths.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config_model import GameConfig
from app.core.defaults import INGREDIENT_NAMES, PERSON_TYPES, default_config
from app.core.demand import NEUTRAL_POPULARITY, apply_popularity, popularity_factor
from app.core.engine import (
    DayPlan,
    LemonadeGame,
    cup_usage,
    cups_possible,
    day_view,
    success_rate,
    summarize,
)
from app.core.ingredients import pack_offers
from app.core.money import round_cents
from app.core.plans import opening_plan

PLAN = {
    "purchases": {"ice": {"100": 1}, "sugar": {"100": 1}, "lemons": {"300": 1}, "cups": {"100": 1}},
    "price": 0.6,
    "recipe": {"ice": 1, "sugar": 1, "lemons": 2},
}


def make_game(seed: int = 11, **over: Any) -> LemonadeGame:
    data = default_config()
    data.update(over)
    return LemonadeGame(GameConfig.model_validate(data), seed)


def play(game: LemonadeGame, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    result = game.run_day(DayPlan.model_validate(plan or PLAN))
    if game.phase == "played":
        game.acknowledge_day(game.day)
    return result


# ------------------------------------------------------------ the events
def test_every_event_carries_the_running_sales_and_the_engines_stock():
    game = make_game(num_days=2)
    stock_before = {name: game.inventory[name].total() for name in INGREDIENT_NAMES}
    result = game.run_day(DayPlan.model_validate(PLAN))
    record, events = result["record"], result["events"]
    assert events, "the default day has visitors"

    opening = record["inventory_open"]
    assert opening == {n: stock_before[n] + record["purchased"][n] for n in INGREDIENT_NAMES}
    usage = cup_usage(record["recipe"])
    sold = 0
    for e in events:
        sold += e["outcome"] == "bought"
        assert e["sales"] == round_cents(sold * record["price"])
        assert e["stock"] == {n: opening[n] - sold * usage[n] for n in INGREDIENT_NAMES}
        assert e["cups_left"] == cups_possible(e["stock"], record["recipe"])
    last = events[-1]
    assert last["sales"] == record["revenue"]
    # The close: what the night then spoiled, and what it left.
    assert last["stock"] == {
        n: record["inventory_end"][n] + record["perished"][n] for n in INGREDIENT_NAMES
    }


def test_a_sell_out_ends_with_no_cups_left(monkeypatch):
    monkeypatch.setattr(LemonadeGame, "weather_for_day", lambda self, day: ("sunny", 30.0))
    small = {name: {"100": 1} for name in INGREDIENT_NAMES}
    plan = {"purchases": small, "price": 0.3, "recipe": {"ice": 1, "sugar": 1, "lemons": 5}}
    result = make_game().run_day(DayPlan.model_validate(plan))
    assert result["record"]["cups_open"] == 20  # 100 lemons, 5 a cup
    assert result["record"]["sold_out"] > 0
    assert result["events"][-1]["cups_left"] == 0


def test_the_seeded_replay_ignores_the_playback_figures():
    twin = make_game(seed=5)
    game = make_game(seed=5)
    a = twin.run_day(DayPlan.model_validate(PLAN))["events"]
    b = game.run_day(DayPlan.model_validate(PLAN))["events"]
    keys = ("id", "type", "arrive_min", "outcome", "reason", "probability")
    assert [{k: e[k] for k in keys} for e in a] == [{k: e[k] for k in keys} for e in b]


# ------------------------------------------------------------ day views
def test_a_day_view_adds_only_derived_figures_and_never_changes_the_record():
    game = make_game(num_days=3)
    play(game)
    play(game)
    stored = [dict(d) for d in game.days]
    views = game.day_records()
    assert game.days == stored  # never stored
    for record, view in zip(game.days, views, strict=True):
        for key, value in record.items():
            if key == "by_type":
                for kind, stats in value.items():
                    assert stats.items() <= view["by_type"][kind].items()
            elif key == "by_hour":
                for hour, stats in zip(value, view["by_hour"], strict=True):
                    assert hour.items() <= stats.items()
            else:
                assert view[key] == value, key
    assert views[0]["cash_start"] == 100.0
    assert views[1]["cash_start"] == game.days[0]["cash_end"]


def test_cash_start_plus_the_cash_result_is_cash_end():
    game = make_game(num_days=3)
    for _ in range(3):
        play(game)
    for view in game.day_records():
        assert round_cents(view["cash_start"] + view["profit"]) == view["cash_end"]


def test_type_and_hour_figures_are_the_engines_counts():
    game = make_game()
    view = play(game)["record"]
    for kind in PERSON_TYPES:
        t = view["by_type"][kind]
        assert t["conversion"] == (round(t["buyers"] / t["visitors"], 4) if t["visitors"] else None)
    for h in view["by_hour"]:
        assert h["refused"] == h["visitors"] - h["buyers"] - h["sold_out"]
        assert h["conversion"] == (round(h["buyers"] / h["visitors"], 4) if h["visitors"] else None)
    assert sum(h["refused"] for h in view["by_hour"]) == view["refused"]


def test_popularity_factor_is_the_one_the_buy_chances_used():
    game = make_game(starting_popularity=0.2)
    view = play(game)["record"]
    assert view["popularity"] == 0.2
    assert view["popularity_factor"] == 0.7
    assert apply_popularity(0.5, 0.2) == 0.5 * view["popularity_factor"]
    assert popularity_factor(NEUTRAL_POPULARITY) == 1


def test_a_day_without_popularity_or_opening_stock_has_neither_figure():
    game = make_game()
    record = dict(play(game)["record"])
    del record["popularity"], record["inventory_open"]
    view = day_view(record, 1.0)
    assert view["popularity_factor"] is None and view["cups_open"] is None


def test_success_rate_is_none_without_visitors():
    assert success_rate(0, 0) is None
    assert success_rate(1, 3) == 0.3333


# ------------------------------------------------------------ the summary
def test_the_summary_adds_up_the_days():
    game = make_game(num_days=3)
    for _ in range(3):
        play(game)
    s = game.summary()
    assert s["conversion"] == round(s["total_buyers"] / s["total_visitors"], 4)
    assert s["total_perished_value"] == round_cents(sum(d["perished_value"] for d in game.days))
    assert s["neutral_popularity"] == NEUTRAL_POPULARITY
    hours = range(9, 18)
    assert [h["hour"] for h in s["hour_totals"]] == list(hours)
    for row in s["hour_totals"]:
        visitors = sum(
            h["visitors"] for d in game.days for h in d["by_hour"] if h["hour"] == row["hour"]
        )
        buyers = sum(
            h["buyers"] for d in game.days for h in d["by_hour"] if h["hour"] == row["hour"]
        )
        assert (row["visitors"], row["buyers"], row["not_bought"]) == (
            visitors,
            buyers,
            visitors - buyers,
        )
    assert s["days"] == game.day_records()


def test_a_summary_with_no_visitors_has_no_success_rate():
    s = summarize(num_days=1, starting_cash=10, final_cash=10, final_popularity=None, days=[])
    assert s["conversion"] is None and s["hour_totals"] == [] and s["total_perished_value"] == 0


# ------------------------------------------------------------ the state
def test_the_state_carries_the_plan_screens_figures():
    game = make_game(starting_popularity=0.3)
    state = game.view()
    assert state["popularity_factor"] == 0.8
    assert state["packs"] == {
        name: pack_offers(game.config.ingredients[name]) for name in INGREDIENT_NAMES
    }
    assert state["suggested_plan"] == opening_plan(game.config)
    play(game)
    state = game.view()
    assert state["suggested_plan"] == {"price": 0.6, "recipe": PLAN["recipe"]}
    assert state["days"] == game.day_records()


@pytest.mark.parametrize("seed", [1, 2])
def test_the_opening_cups_are_the_stocks_cups(seed):
    game = make_game(seed=seed)
    view = play(game)["record"]
    assert view["cups_open"] == cups_possible(view["inventory_open"], view["recipe"])
