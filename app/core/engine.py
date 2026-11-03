"""The Lemony game engine: pure, seeded, no I/O.

A game is rebuilt from its payload for every operation (``from_payload`` /
``to_payload``). Randomness is derived from ``(seed, day, purpose)`` so a day
replays identically no matter when it is run, and nothing touches the global
``random`` module.

Day lifecycle:
    planning --run_day()--> played --acknowledge_day()--> planning (next day)
    the last day's run_day() goes straight to finished.

Cost per cup has one meaning everywhere (``cup_cost``): the average cost of the
cups you can make from your stock. Each ingredient is used in the order cups use
it: held stock oldest batch first, at the unit price paid for it, then today's
purchases, one lot per pack size, dearest per unit first (so the cheapest stock
is kept longest). The plan preview prices the stock plus the planned packs; the
day record holds what the cups sold cost, or, on a day with no sales, what the
cups you could have made would have cost (``None`` with nothing to make a cup).

What the client shows beside a day record is worked out here too, never by the
client: ``day_view`` adds the cash the day began with, the popularity factor, each
type's and hour's success rate, the refusals per hour and the cups the opening stock
made; ``summarize`` adds the game's success rate, the spoilage value, the traffic per
hour and the neutral popularity. Each event of a played day carries the running
sales, the stock left and the cups it still makes, read off the engine's own stock.

The per-customer events are returned by ``run_day`` (and sent once, in ``day_result``)
but never kept: the payload holds the day records only, so its size does not grow with
the customers of a day. A payload saved before carries ``last_day_events``; it is
ignored, and gone on the next save.
"""

from __future__ import annotations

import random
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config_model import GameConfig
from .defaults import INGREDIENT_NAMES, PERSON_TYPES, RECIPE_INGREDIENTS
from .demand import (
    NEUTRAL_POPULARITY,
    NOT_POPULAR,
    apply_popularity,
    buy_probability,
    expected_spawn,
    ingredient_scores,
    poisson,
    popularity,
    popularity_factor,
    recipe_quality,
    refusal_reason,
    refused_for_popularity,
    willingness_to_pay,
)
from .ingredients import Ingredient, pack_offers
from .limits import MAX_PACKS_PER_SIZE, public_limits
from .money import round_cents
from .people import PersonType, build_people
from .plans import opening_plan
from .weather import roll_day_weather

Phase = Literal["planning", "played", "finished"]


class PlanError(ValueError):
    """A day plan the rules refuse. The message is shown to the player."""


class DayPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # {ingredient: {pack_size (as string, JSON keys): count}}
    purchases: dict[str, dict[str, int]] = Field(default_factory=dict)
    price: float
    recipe: dict[str, int]


def _money(value: float) -> float:
    return round_cents(value)


Lots = dict[str, list[tuple[int, float]]]


def cup_usage(recipe: dict[str, int]) -> dict[str, int]:
    """Units of each ingredient one cup takes: the recipe, plus the cup itself."""
    return {**{name: recipe[name] for name in RECIPE_INGREDIENTS}, "cups": 1}


def cups_possible(held: dict[str, int], recipe: dict[str, int]) -> int:
    """How many cups ``held`` units make with ``recipe``: the scarcest ingredient decides."""
    usage = cup_usage(recipe)
    return min(held.get(name, 0) // amount for name, amount in usage.items() if amount > 0)


def success_rate(buyers: int, visitors: int) -> float | None:
    """Cups sold per visitor, to 4 places; None with no visitors."""
    return round(buyers / visitors, 4) if visitors else None


def cup_cost(stock: Lots, recipe: dict[str, int]) -> tuple[int, float | None]:
    """(cups you can make, their average cost) from ``stock``.

    ``stock`` lists each ingredient's units in the order cups use them, as
    ``(units, unit price paid)``. The cost is rounded to the cent; it is ``None``
    when not a single cup can be made.
    """
    per_cup = cup_usage(recipe)
    held = {name: sum(qty for qty, _ in stock.get(name, [])) for name in per_cup}
    cups = cups_possible(held, recipe)
    if cups <= 0:
        return 0, None
    total = 0.0
    for name, amount in per_cup.items():
        remaining = amount * cups
        for qty, unit_cost in stock.get(name, []):
            if remaining <= 0:
                break
            take = min(qty, remaining)
            total += take * unit_cost
            remaining -= take
    return cups, _money(total / cups)


def day_view(record: dict[str, Any], cash_start: float) -> dict[str, Any]:
    """A day record with the figures the screens show beside it. Never stored.

    Derived from the record's own fields (so a stored day gets them too): the cash the day
    began with, the factor today's popularity put on every buy chance, each type's and
    hour's success rate (None with no visitors), the refusals per hour, and the cups the
    opening stock made (only on days that kept their opening stock).
    """
    view = dict(record)
    view["cash_start"] = cash_start
    pop = record.get("popularity")
    view["popularity_factor"] = None if pop is None else round(popularity_factor(pop), 4)
    view["by_type"] = {
        kind: {**stats, "conversion": success_rate(stats["buyers"], stats["visitors"])}
        for kind, stats in record["by_type"].items()
    }
    view["by_hour"] = [
        {
            **h,
            "refused": h["visitors"] - h["buyers"] - h["sold_out"],
            "conversion": success_rate(h["buyers"], h["visitors"]),
        }
        for h in record["by_hour"]
    ]
    opening = record.get("inventory_open")
    view["cups_open"] = None if opening is None else cups_possible(opening, record["recipe"])
    return view


def day_views(days: list[dict[str, Any]], starting_cash: float) -> list[dict[str, Any]]:
    """``day_view`` of every record: each day begins with the cash the one before ended on."""
    views = []
    cash = _money(starting_cash)
    for record in days:
        views.append(day_view(record, cash))
        cash = record["cash_end"]
    return views


def _hour_totals(days: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Visitors and buyers per opening hour, over every day."""
    totals: dict[int, list[int]] = {}
    for record in days:
        for h in record["by_hour"]:
            visitors_buyers = totals.setdefault(h["hour"], [0, 0])
            visitors_buyers[0] += h["visitors"]
            visitors_buyers[1] += h["buyers"]
    return [
        {"hour": hour, "visitors": v, "buyers": b, "not_bought": v - b}
        for hour, (v, b) in sorted(totals.items())
    ]


def summarize(
    *,
    num_days: int,
    starting_cash: float,
    final_cash: float,
    final_popularity: float | None,
    days: list[dict[str, Any]],
) -> dict[str, Any]:
    """The game summary, built from the day records.

    The one builder for a live game (``LemonadeGame.summary``) and a stored one
    (``db_service.load_game_detail``), so the two cannot drift apart. Its ``days`` are
    ``day_view``s; ``conversion`` is all cups sold over all visitors (None with none).
    """
    total_visitors = sum(d["visitors"] for d in days)
    total_buyers = sum(d["buyers"] for d in days)
    return {
        "num_days": num_days,
        "days_played": len(days),
        "starting_cash": _money(starting_cash),
        "final_cash": final_cash,
        "total_profit": _money(final_cash - starting_cash),
        "total_revenue": _money(sum(d["revenue"] for d in days)),
        "total_spend": _money(sum(d["spend"] for d in days)),
        "total_visitors": total_visitors,
        "total_buyers": total_buyers,
        "total_sold_out": sum(d["sold_out"] for d in days),
        "conversion": success_rate(total_buyers, total_visitors),
        "final_popularity": final_popularity,
        "neutral_popularity": NEUTRAL_POPULARITY,
        "perished_totals": {
            name: sum(d["perished"][name] for d in days) for name in INGREDIENT_NAMES
        },
        "total_perished_value": _money(sum(d["perished_value"] for d in days)),
        "hour_totals": _hour_totals(days),
        "days": day_views(days, starting_cash),
    }


class LemonadeGame:
    def __init__(self, config: GameConfig, seed: int) -> None:
        self.config = config
        self.seed = seed
        self.people: list[PersonType] = build_people(config)
        self.day = 1
        self.phase: Phase = "planning"
        self.cash = _money(config.starting_cash)
        self.inventory: dict[str, Ingredient] = {
            name: Ingredient.from_config(name, config.ingredients[name])
            for name in INGREDIENT_NAMES
        }
        self.days: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ rng
    def _rng(self, day: int, purpose: str) -> random.Random:
        return random.Random(f"{self.seed}:{day}:{purpose}")

    def weather_for_day(self, day: int) -> tuple[str, float]:
        return roll_day_weather(self.config, self._rng(day, "weather"))

    # --------------------------------------------------------------- pricing
    def purchase_cost(
        self, purchases: dict[str, dict[str, int]]
    ) -> tuple[float, dict[str, int], dict[str, list[tuple[int, float]]]]:
        """Validate purchases; return (cost, units per ingredient, lots).

        Each pack size is priced with its own discount, so every size bought
        becomes its own lot of ``(units, unit price paid)``.
        """
        units: dict[str, int] = {name: 0 for name in INGREDIENT_NAMES}
        lots: dict[str, list[tuple[int, float]]] = {name: [] for name in INGREDIENT_NAMES}
        cost = 0.0
        for name, packs in purchases.items():
            if name not in self.config.ingredients:
                raise PlanError(f"Unknown ingredient: {name}")
            cfg = self.config.ingredients[name]
            for size_key, count in packs.items():
                try:
                    size = int(size_key)
                except (TypeError, ValueError):
                    raise PlanError(f"Invalid pack size for {name}: {size_key}") from None
                pack = cfg.pack(size)
                if pack is None:
                    raise PlanError(f"{name} is not sold in packs of {size}")
                if not 0 <= count <= MAX_PACKS_PER_SIZE:
                    # Refused, never clamped: a negative count is no order at all.
                    raise PlanError(
                        f"You can buy 0 to {MAX_PACKS_PER_SIZE} packs of {size} {name} a day, "
                        f"not {count}"
                    )
                if count == 0:
                    continue
                price = cfg.pack_price(pack)
                units[name] += size * count
                cost += price * count
                lots[name].append((size * count, price / size))
        for name_lots in lots.values():
            name_lots.sort(
                key=lambda lot: -lot[1]
            )  # same-day lots: dearest used first, cheapest kept longest
        return _money(cost), units, lots

    def cup_cost(
        self, recipe: dict[str, int], lots: Lots | None = None
    ) -> tuple[int, float | None]:
        """(cups you can make, their average cost) from the stock held plus ``lots`` bought
        today (``purchase_cost``'s lots, already dearest first). See ``cup_cost``."""
        stock: Lots = {
            name: [(b.qty, ing.paid(b)) for b in ing.batches] + list((lots or {}).get(name, []))
            for name, ing in self.inventory.items()
        }
        return cup_cost(stock, recipe)

    def validate_price(self, price: float) -> None:
        mm = self.config.min_max_values
        if not mm.price.min <= price <= mm.price.max:
            raise PlanError(f"Price must be between {mm.price.min:.2f} and {mm.price.max:.2f}")

    def affordable(self, cost: float) -> bool:
        """Whether ``cost`` fits the cash in hand (there are no loans)."""
        return cost <= self.cash + 1e-9

    def cash_after(self, spend: float) -> float:
        """The cash left once ``spend`` is paid."""
        return _money(self.cash - spend)

    def validate_recipe(self, recipe: dict[str, int]) -> None:
        """Every recipe ingredient set, each inside its config range (never below 0)."""
        mm = self.config.min_max_values
        if set(recipe) != set(RECIPE_INGREDIENTS):
            raise PlanError("Recipe must set ice, sugar and lemons")
        for name in RECIPE_INGREDIENTS:
            rng = getattr(mm, name)
            if not rng.min <= recipe[name] <= rng.max:
                raise PlanError(f"{name} per cup must be between {rng.min} and {rng.max}")

    def validate_purchases(
        self, plan: DayPlan
    ) -> tuple[float, dict[str, int], dict[str, list[tuple[int, float]]]]:
        """The plan's recipe and purchases, and that the cash covers them (not its price)."""
        self.validate_recipe(plan.recipe)
        cost, units, lots = self.purchase_cost(plan.purchases)
        if not self.affordable(cost):
            raise PlanError(f"Purchases cost ${cost:.2f} but you only have ${self.cash:.2f}")
        return cost, units, lots

    def _validate_plan(
        self, plan: DayPlan
    ) -> tuple[float, dict[str, int], dict[str, list[tuple[int, float]]]]:
        self.validate_price(plan.price)
        return self.validate_purchases(plan)

    # ------------------------------------------------------------------ day
    def current_popularity(self) -> float:
        """Mean success rate (cups sold / visitors) of the days so far that had visitors."""
        rates = [d["buyers"] / d["visitors"] for d in self.days if d["visitors"]]
        return popularity(rates, self.config.starting_popularity)

    def _can_make_cup(self, recipe: dict[str, int]) -> bool:
        if self.inventory["cups"].total() < 1:
            return False
        return all(self.inventory[name].total() >= recipe[name] for name in RECIPE_INGREDIENTS)

    def _make_cup(self, recipe: dict[str, int]) -> float:
        """Use one cup's ingredients, oldest stock first; return what they cost."""
        cost = sum(self.inventory[name].consume(recipe[name]) for name in RECIPE_INGREDIENTS)
        return cost + self.inventory["cups"].consume(1)

    def run_day(self, plan: DayPlan) -> dict[str, Any]:
        if self.phase != "planning":
            raise PlanError("This day has already been played")
        spend, units, lots = self._validate_plan(plan)
        recipe = {name: int(plan.recipe[name]) for name in RECIPE_INGREDIENTS}
        price = _money(plan.price)

        self.cash = self.cash_after(spend)
        for name, name_lots in lots.items():
            for qty, unit_cost in name_lots:
                self.inventory[name].add(qty, unit_cost)
        inventory_open = {name: self.inventory[name].total() for name in INGREDIENT_NAMES}
        # The stock as the day goes, for the playback: it changes only when a cup is sold.
        stock = dict(inventory_open)
        cups_left = cups_possible(stock, recipe)

        weather, temperature = self.weather_for_day(self.day)
        multiplier = self.config.weather_multipliers[weather]
        rng = self._rng(self.day, "customers")
        mm = self.config.min_max_values
        pop = self.current_popularity()

        events: list[dict[str, Any]] = []
        by_type = {k: {"visitors": 0, "buyers": 0, "sold_out": 0} for k in PERSON_TYPES}
        by_hour: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        # Recipe, price and popularity are fixed for the day, so each type's view of them is too.
        scores_by_type = {p.kind: ingredient_scores(p, recipe) for p in self.people}
        # The chance at a neutral popularity, then the one applied today.
        base_by_type = {
            p.kind: buy_probability(
                p,
                price,
                willingness_to_pay(
                    p, recipe_quality(scores_by_type[p.kind]), self.config.quality_swing
                ),
            )
            for p in self.people
        }
        probability_by_type = {
            kind: apply_popularity(base, pop) for kind, base in base_by_type.items()
        }
        revenue = 0.0
        ingredients_used = 0.0

        for hour in range(mm.hour.min, mm.hour.max + 1):
            arrivals: list[PersonType] = []
            for person in self.people:
                lam = expected_spawn(person, hour, weather, temperature, multiplier)
                arrivals.extend([person] * poisson(rng, lam))
            rng.shuffle(arrivals)
            hour_stats = {"hour": hour, "visitors": len(arrivals), "buyers": 0, "sold_out": 0}
            n = len(arrivals)
            for i, person in enumerate(arrivals):
                probability = probability_by_type[person.kind]
                draw = rng.random()  # the visitor's one draw: it decides and explains
                wants = draw < probability
                reason = None
                if wants and self._can_make_cup(recipe):
                    ingredients_used += self._make_cup(recipe)
                    revenue += price
                    stock = {name: self.inventory[name].total() for name in INGREDIENT_NAMES}
                    cups_left = cups_possible(stock, recipe)
                    outcome = "bought"
                    by_type[person.kind]["buyers"] += 1
                    hour_stats["buyers"] += 1
                elif wants:
                    outcome = "sold_out"
                    by_type[person.kind]["sold_out"] += 1
                    hour_stats["sold_out"] += 1
                else:
                    outcome = "refused"
                    if refused_for_popularity(draw, base_by_type[person.kind]):
                        reason = NOT_POPULAR
                    else:
                        reason = refusal_reason(person, price, recipe, scores_by_type[person.kind])
                    reasons[reason] = reasons.get(reason, 0) + 1
                by_type[person.kind]["visitors"] += 1
                events.append(
                    {
                        "id": len(events),
                        "type": person.kind,
                        "arrive_min": round(hour * 60 + (i + 0.5) * 60 / n, 2),
                        "outcome": outcome,
                        "reason": reason,
                        "probability": round(probability, 3),
                        # After this visitor: the sales so far and the stock left.
                        "sales": _money(revenue),
                        "stock": dict(stock),
                        "cups_left": cups_left,
                    }
                )
            by_hour.append(hour_stats)

        revenue = _money(revenue)
        buyers = sum(1 for e in events if e["outcome"] == "bought")
        # With no sales the stock is as it opened: price the cups it could have made.
        cost_per_cup = _money(ingredients_used / buyers) if buyers else self.cup_cost(recipe)[1]
        perish_rng = self._rng(self.day, "perish")
        losses = {
            name: self.inventory[name].end_of_day_perish(perish_rng) for name in INGREDIENT_NAMES
        }
        perished = {name: lost for name, (lost, _) in losses.items()}
        perished_value = _money(sum(value for _, value in losses.values()))
        self.cash = _money(self.cash + revenue)

        visitors = len(events)
        record = {
            "day": self.day,
            "weather": weather,
            "temperature": temperature,
            "price": price,
            "recipe": recipe,
            # What the cups sold cost, at the price paid; with no sales, what the cups you
            # could have made would have cost; None with nothing to make a cup from.
            "cost_per_cup": cost_per_cup,
            "purchased": units,
            # The stock when the stand opened, today's packs included.
            "inventory_open": inventory_open,
            "spend": spend,
            "revenue": revenue,
            "profit": _money(revenue - spend),
            "visitors": visitors,
            "buyers": buyers,
            "sold_out": sum(1 for e in events if e["outcome"] == "sold_out"),
            "refused": sum(1 for e in events if e["outcome"] == "refused"),
            "conversion": round(buyers / visitors, 4) if visitors else 0.0,
            # The popularity applied to today's buy chances (before today's rate counts).
            "popularity": round(pop, 4),
            "perished": perished,
            "perished_value": perished_value,
            "cash_end": self.cash,
            "inventory_end": {name: self.inventory[name].total() for name in INGREDIENT_NAMES},
            "by_type": by_type,
            "by_hour": by_hour,
            "refusal_reasons": reasons,
        }
        self.days.append(record)
        self.phase = "finished" if self.day >= self.config.num_days else "played"
        # The events go out with this result only: the game keeps no per-customer list.
        return {"record": self.day_records()[-1], "events": events}

    def acknowledge_day(self, day: int) -> bool:
        """Move from a played day to planning the next. Returns False if nothing changed."""
        if self.phase != "played" or day != self.day:
            return False
        self.day += 1
        self.phase = "planning"
        return True

    # --------------------------------------------------------------- views
    def day_records(self) -> list[dict[str, Any]]:
        """Every day record as the client gets it (``day_view``)."""
        return day_views(self.days, self.config.starting_cash)

    def suggested_plan(self) -> dict[str, Any]:
        """What the plan screen starts from: the last day's price and recipe, else
        ``plans.opening_plan``."""
        if self.days:
            last = self.days[-1]
            return {"price": last["price"], "recipe": dict(last["recipe"])}
        return opening_plan(self.config)

    def summary(self) -> dict[str, Any]:
        return summarize(
            num_days=self.config.num_days,
            starting_cash=self.config.starting_cash,
            final_cash=self.cash,
            final_popularity=round(self.current_popularity(), 4),
            days=self.days,
        )

    def view(self) -> dict[str, Any]:
        """Everything the client renders. Never includes the seed of future days' weather."""
        weather, temperature = self.weather_for_day(self.day)
        pop = self.current_popularity()
        return {
            "day": self.day,
            "num_days": self.config.num_days,
            "phase": self.phase,
            "cash": self.cash,
            "today": {"weather": weather, "temperature": temperature},
            # Applies to the next day played (today's, while planning), and the factor it
            # puts on every buy chance.
            "popularity": round(pop, 4),
            "popularity_factor": round(popularity_factor(pop), 4),
            "inventory": {name: ing.view() for name, ing in self.inventory.items()},
            # The packs on sale, priced as a purchase prices them.
            "packs": {
                name: pack_offers(self.config.ingredients[name]) for name in INGREDIENT_NAMES
            },
            "suggested_plan": self.suggested_plan(),
            "days": self.day_records(),
            "config": self.config.model_dump(),
            # The bounds the client enforces and shows (packs per size, price step, ...).
            "limits": public_limits(),
        }

    # ----------------------------------------------------------- payload
    def to_payload(self) -> dict[str, Any]:
        return {
            "config": self.config.model_dump(),
            "seed": self.seed,
            "day": self.day,
            "phase": self.phase,
            "cash": self.cash,
            "inventory": {name: ing.to_dict() for name, ing in self.inventory.items()},
            "days": self.days,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LemonadeGame:
        # A stored game: every rule but the customers-per-day cap, so a game made before
        # the cap (or under a looser one) still loads and plays.
        config = GameConfig.validate_uncapped(payload["config"])
        game = cls(config, int(payload["seed"]))
        game.day = int(payload["day"])
        game.phase = payload["phase"]
        game.cash = float(payload["cash"])
        game.inventory = {
            name: Ingredient.from_dict(name, config.ingredients[name], payload["inventory"][name])
            for name in INGREDIENT_NAMES
        }
        game.days = list(payload.get("days", []))
        # A payload saved before carries `last_day_events`: ignored, and dropped on the next save.
        return game
