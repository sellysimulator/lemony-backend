"""One ``Ingredient`` class for ice, sugar, lemons and cups.

Stock is held as purchase batches so every unit's age is known. Sales consume
the oldest batch first (FIFO). Perishing is checked at the end of each day,
per batch, with a "fresh period + linear ramp" hazard:

    age d (1 on the first end of day after purchase)
    hazard(d) = 0                    if never_perishes or d <= fresh_days
              = 1                    if d >= max_days
              = (d - F) / (M - F)    otherwise

With ``fresh_days = 0`` this is exactly the original rule ``d / time_alive``.
The number of units lost in a batch is Binomial(qty, hazard).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .config_model import IngredientConfig


@dataclass
class Batch:
    qty: int
    age: int = 0


class Ingredient:
    def __init__(
        self,
        name: str,
        unit_cost: float,
        fresh_days: int,
        max_days: int,
        never_perishes: bool,
        batches: list[Batch] | None = None,
    ) -> None:
        self.name = name
        self.unit_cost = unit_cost
        self.fresh_days = fresh_days
        self.max_days = max_days
        self.never_perishes = never_perishes
        # Oldest first; new purchases are appended.
        self.batches: list[Batch] = batches or []

    @classmethod
    def from_config(cls, name: str, cfg: IngredientConfig) -> Ingredient:
        return cls(name, cfg.unit_cost, cfg.fresh_days, cfg.max_days, cfg.never_perishes)

    def hazard(self, age: int) -> float:
        """Probability that a unit of this age perishes at tonight's check."""
        if self.never_perishes or age <= self.fresh_days:
            return 0.0
        if age >= self.max_days:
            return 1.0
        return (age - self.fresh_days) / (self.max_days - self.fresh_days)

    def total(self) -> int:
        return sum(b.qty for b in self.batches)

    def add(self, qty: int) -> None:
        if qty > 0:
            self.batches.append(Batch(qty=qty, age=0))

    def consume(self, qty: int) -> None:
        """Remove ``qty`` units, oldest batch first. Caller checks ``total()`` first."""
        if qty > self.total():
            raise ValueError(f"not enough {self.name}")
        remaining = qty
        for batch in self.batches:
            take = min(batch.qty, remaining)
            batch.qty -= take
            remaining -= take
            if remaining == 0:
                break
        self.batches = [b for b in self.batches if b.qty > 0]

    def end_of_day_perish(self, rng: random.Random) -> int:
        """Age every batch by a day, drop perished units, return how many were lost."""
        lost = 0
        for batch in self.batches:
            batch.age += 1
            p = self.hazard(batch.age)
            if p >= 1.0:
                gone = batch.qty
            elif p <= 0.0:
                gone = 0
            else:
                gone = sum(1 for _ in range(batch.qty) if rng.random() < p)
            batch.qty -= gone
            lost += gone
        self.batches = [b for b in self.batches if b.qty > 0]
        return lost

    def expected_loss_tonight(self) -> float:
        """Expected units lost at the next check, if nothing is sold today."""
        return sum(b.qty * self.hazard(b.age + 1) for b in self.batches)

    def to_dict(self) -> dict[str, Any]:
        return {"batches": [{"qty": b.qty, "age": b.age} for b in self.batches]}

    def view(self) -> dict[str, Any]:
        """Client-facing inventory line."""
        return {
            "total": self.total(),
            "batches": [
                {"qty": b.qty, "age": b.age, "risk_tonight": round(self.hazard(b.age + 1), 4)}
                for b in self.batches
            ],
            "expected_loss_tonight": round(self.expected_loss_tonight(), 2),
        }

    @classmethod
    def from_dict(cls, name: str, cfg: IngredientConfig, data: dict[str, Any]) -> Ingredient:
        ing = cls.from_config(name, cfg)
        ing.batches = [Batch(qty=int(b["qty"]), age=int(b["age"])) for b in data.get("batches", [])]
        return ing
