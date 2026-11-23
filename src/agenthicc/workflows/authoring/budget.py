"""Durable repair accounting for authoring workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

__all__ = ["RepairBudget", "RepairBudgetExhausted"]

_DEFAULT_MAX_CYCLES = 3


class RepairBudgetExhausted(RuntimeError):
    """The next repair would exceed the configured budget."""


@dataclass(frozen=True, slots=True)
class RepairBudget:
    """The initial capacity and the number of repair rounds already admitted."""

    max_cycles: int = _DEFAULT_MAX_CYCLES
    used: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.max_cycles) is not int
            or type(self.used) is not int
            or not 0 <= self.used <= self.max_cycles
        ):
            raise ValueError("repair budget needs integers with 0 <= used <= max_cycles")

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> Self:
        """Read the operator's initial budget from the workflow authoring table."""
        limit = settings.get("max_repair_cycles", _DEFAULT_MAX_CYCLES)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("workflows.authoring.max_repair_cycles must be a non-negative integer")
        return cls(max_cycles=limit)

    @classmethod
    def from_payload(cls, raw: object) -> Self:
        """Rehydrate a checkpoint, requiring both persisted fields."""
        if not isinstance(raw, dict):
            raise ValueError("authoring checkpoint is missing its repair budget")
        max_cycles = raw.get("max_cycles")
        used = raw.get("used")
        if not isinstance(max_cycles, int) or not isinstance(used, int):
            raise ValueError("authoring checkpoint has an invalid repair budget")
        return cls(max_cycles=max_cycles, used=used)

    def charge(self) -> RepairBudget:
        """Return a new budget with one more admitted repair round."""
        if self.used == self.max_cycles:
            raise RepairBudgetExhausted(f"repair budget exhausted (limit {self.max_cycles})")
        return RepairBudget(max_cycles=self.max_cycles, used=self.used + 1)
