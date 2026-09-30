"""The Lemony game engine: pure, seeded, no I/O.

A game is rebuilt from its payload for every operation (``from_payload`` /
``to_payload``). Randomness is derived from ``(seed, day, purpose)`` so a day
replays identically no matter when it is run, and nothing touches the global
``random`` module.

Day lifecycle:
    planning --run_day()--> played --acknowledge_day()--> planning (next day)
    the last day's run_day() goes straight to finished.
"""

from __future__ import annotations

import random
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config_model import GameConfig
from .defaults import INGREDIENT_NAMES, PERSON_TYPES, RECIPE_INGREDIENTS
from .demand import buy_probability, buy_scores, expected_spawn, poisson, refusal_reason
from .ingredients import Ingredient
from .people import PersonType, build_people
from .weather import roll_day_weather

Phase = Literal["planning", "played", "finished"]
MAX_PACKS_PER_SIZE = 100


class PlanError(ValueError):
    """A day plan the rules refuse. The message is shown to the player."""


class DayPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # {ingredient: {pack_size (as string, JSON keys): count}}
    purchases: dict[str, dict[str, int]] = Field(default_factory=dict)
    price: float
    recipe: dict[str, int]


def _money(value: float) -> float:
    return round(value + 0.0, 2)


class LemonadeGame:
    def __init__(self, config: GameConfig, seed: int) -> None:
        self.config = config
        self.seed = seed
        self.people: list[PersonType] = build_people(config)
        self.day = 1
        self.phase: Phase = "planning"
        self.cash = _money(config.starting_cash)
        self.inventory: dict[str, Ingredient] = {
            name: Ingredient.from_config(name, config.ingredients[name]) for name in INGREDIENT_NAMES
        }
        self.days: list[dict[str, Any]] = []
        self.last_day_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ rng
    def _rng(self, day: int, purpose: str) -> random.Random:
        return random.Random(f"{self.seed}:{day}:{purpose}")

    def weather_for_day(self, day: int) -> tuple[str, float]:
        return roll_day_weather(self.config, self._rng(day, "weather"))

    # --------------------------------------------------------------- pricing
    def purchase_cost(self, purchases: dict[str, dict[str, int]]) -> tuple[float, dict[str, int]]:
        """Validate purchases; return (cost, units per ingredient)."""
        units: dict[str, int] = {name: 0 for name in INGREDIENT_NAMES}
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
                if size not in cfg.pack_sizes:
                    raise PlanError(f"{name} is not sold in packs of {size}")
                if not 0 <= count <= MAX_PACKS_PER_SIZE:
                    raise PlanError(f"Pack count for {name} must be 0-{MAX_PACKS_PER_SIZE}")
                units[name] += size * count
                cost += size * count * cfg.unit_cost
        return _money(cost), units

    def cost_per_cup(self, recipe: dict[str, int]) -> float:
        ing = self.config.ingredients
        return _money(
            sum(recipe[name] * ing[name].unit_cost for name in RECIPE_INGREDIENTS)
            + ing["cups"].unit_cost
        )

    def _validate_plan(self, plan: DayPlan) -> tuple[float, dict[str, int]]:
        mm = self.config.min_max_values
        if not mm.price.min <= plan.price <= mm.price.max:
            raise PlanError(f"Price must be between {mm.price.min:.2f} and {mm.price.max:.2f}")
        if set(plan.recipe) != set(RECIPE_INGREDIENTS):
            raise PlanError("Recipe must set ice, sugar and lemons")
        for name in RECIPE_INGREDIENTS:
            rng = getattr(mm, name)
            if not rng.min <= plan.recipe[name] <= rng.max:
                raise PlanError(f"{name} per cup must be between {rng.min} and {rng.max}")
        cost, units = self.purchase_cost(plan.purchases)
        if cost > self.cash + 1e-9:
            raise PlanError(f"Purchases cost ${cost:.2f} but you only have ${self.cash:.2f}")
        return cost, units

    # ------------------------------------------------------------------ day
    def _can_make_cup(self, recipe: dict[str, int]) -> bool:
        if self.inventory["cups"].total() < 1:
            return False
        return all(self.inventory[name].total() >= recipe[name] for name in RECIPE_INGREDIENTS)

    def _make_cup(self, recipe: dict[str, int]) -> None:
        for name in RECIPE_INGREDIENTS:
            self.inventory[name].consume(recipe[name])
        self.inventory["cups"].consume(1)

    def run_day(self, plan: DayPlan) -> dict[str, Any]:
        if self.phase != "planning":
            raise PlanError("This day has already been played")
        spend, units = self._validate_plan(plan)
        recipe = {name: int(plan.recipe[name]) for name in RECIPE_INGREDIENTS}
        price = _money(plan.price)

        self.cash = _money(self.cash - spend)
        for name, qty in units.items():
            self.inventory[name].add(qty)

        weather, temperature = self.weather_for_day(self.day)
        multiplier = self.config.weather_multipliers[weather]
        rng = self._rng(self.day, "customers")
        mm = self.config.min_max_values

        events: list[dict[str, Any]] = []
        by_type = {k: {"visitors": 0, "buyers": 0, "sold_out": 0} for k in PERSON_TYPES}
        by_hour: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        revenue = 0.0

        for hour in range(mm.hour.min, mm.hour.max + 1):
            arrivals: list[PersonType] = []
            for person in self.people:
                lam = expected_spawn(person, hour, weather, temperature, multiplier)
                arrivals.extend([person] * poisson(rng, lam))
            rng.shuffle(arrivals)
            hour_stats = {"hour": hour, "visitors": len(arrivals), "buyers": 0, "sold_out": 0}
            n = len(arrivals)
            for i, person in enumerate(arrivals):
                scores = buy_scores(person, price, recipe)
                probability = buy_probability(scores)
                wants = rng.random() < probability
                reason = None
                if wants and self._can_make_cup(recipe):
                    self._make_cup(recipe)
                    revenue += price
                    outcome = "bought"
                    by_type[person.kind]["buyers"] += 1
                    hour_stats["buyers"] += 1
                elif wants:
                    outcome = "sold_out"
                    by_type[person.kind]["sold_out"] += 1
                    hour_stats["sold_out"] += 1
                else:
                    outcome = "refused"
                    reason = refusal_reason(person, price, recipe, scores)
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
                    }
                )
            by_hour.append(hour_stats)

        revenue = _money(revenue)
        perish_rng = self._rng(self.day, "perish")
        perished = {name: self.inventory[name].end_of_day_perish(perish_rng) for name in INGREDIENT_NAMES}
        perished_value = _money(
            sum(qty * self.config.ingredients[name].unit_cost for name, qty in perished.items())
        )
        self.cash = _money(self.cash + revenue)

        visitors = len(events)
        buyers = sum(1 for e in events if e["outcome"] == "bought")
        record = {
            "day": self.day,
            "weather": weather,
            "temperature": temperature,
            "price": price,
            "recipe": recipe,
            "cost_per_cup": self.cost_per_cup(recipe),
            "purchased": units,
            "spend": spend,
            "revenue": revenue,
            "profit": _money(revenue - spend),
            "visitors": visitors,
            "buyers": buyers,
            "sold_out": sum(1 for e in events if e["outcome"] == "sold_out"),
            "refused": sum(1 for e in events if e["outcome"] == "refused"),
            "conversion": round(buyers / visitors, 4) if visitors else 0.0,
            "perished": perished,
            "perished_value": perished_value,
            "cash_end": self.cash,
            "inventory_end": {name: self.inventory[name].total() for name in INGREDIENT_NAMES},
            "by_type": by_type,
            "by_hour": by_hour,
            "refusal_reasons": reasons,
        }
        self.days.append(record)
        self.last_day_events = events
        self.phase = "finished" if self.day >= self.config.num_days else "played"
        return {"record": record, "events": events}

    def acknowledge_day(self, day: int) -> bool:
        """Move from a played day to planning the next. Returns False if nothing changed."""
        if self.phase != "played" or day != self.day:
            return False
        self.day += 1
        self.phase = "planning"
        self.last_day_events = []
        return True

    # --------------------------------------------------------------- views
    def summary(self) -> dict[str, Any]:
        perished_totals = {name: sum(d["perished"][name] for d in self.days) for name in INGREDIENT_NAMES}
        return {
            "num_days": self.config.num_days,
            "days_played": len(self.days),
            "starting_cash": _money(self.config.starting_cash),
            "final_cash": self.cash,
            "total_profit": _money(self.cash - self.config.starting_cash),
            "total_revenue": _money(sum(d["revenue"] for d in self.days)),
            "total_spend": _money(sum(d["spend"] for d in self.days)),
            "total_visitors": sum(d["visitors"] for d in self.days),
            "total_buyers": sum(d["buyers"] for d in self.days),
            "total_sold_out": sum(d["sold_out"] for d in self.days),
            "perished_totals": perished_totals,
            "days": self.days,
        }

    def view(self) -> dict[str, Any]:
        """Everything the client renders. Never includes the seed of future days' weather."""
        weather, temperature = self.weather_for_day(self.day)
        return {
            "day": self.day,
            "num_days": self.config.num_days,
            "phase": self.phase,
            "cash": self.cash,
            "today": {"weather": weather, "temperature": temperature},
            "inventory": {name: ing.view() for name, ing in self.inventory.items()},
            "days": self.days,
            "last_day_events": self.last_day_events,
            "config": self.config.model_dump(),
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
            "last_day_events": self.last_day_events,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LemonadeGame:
        config = GameConfig.model_validate(payload["config"])
        game = cls(config, int(payload["seed"]))
        game.day = int(payload["day"])
        game.phase = payload["phase"]
        game.cash = float(payload["cash"])
        game.inventory = {
            name: Ingredient.from_dict(name, config.ingredients[name], payload["inventory"][name])
            for name in INGREDIENT_NAMES
        }
        game.days = list(payload.get("days", []))
        game.last_day_events = list(payload.get("last_day_events", []))
        return game
