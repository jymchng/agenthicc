"""Per-pool usage accounting and declared budgets (SUBAGENT-06).

Accounting reads the session usage ledger; it is not a second meter. Every
worker's provider calls are recorded under a run id that names its pool
(:func:`worker_run_id`), so one pool's spend is the set of completed ledger
records carrying that pool id. Cost is summed only over records with a known
cost, and a pool with any record of unknown cost reports its cost as
unavailable rather than as zero.

A declared budget is an optional ``budget:`` mapping on ``spawn_subagents`` —
``tokens``, ``cost_usd`` and ``wall_clock_s``, any subset. It may lower the
operator's limits and never raise them: a declaration above the
``subagents.max_budget`` ceiling is clamped down to it and the clamp is
reported, and a declared ``tokens`` or ``wall_clock_s`` replaces the operator
ceiling of the same unit only when it is lower. Crossing an effective limit is
answered by SUBAGENT-08's stop policy, exactly as an operator ceiling is.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agenthicc.runners.usage_ledger import UsageLedger, UsageRecord

__all__ = [
    "BudgetError",
    "budget_digest",
    "budget_report",
    "EffectiveBudget",
    "PoolBudget",
    "PoolUsage",
    "model_has_known_price",
    "parse_budget",
    "pool_usage",
    "resolve_budget",
    "worker_run_id",
    "worker_usage",
]

CostStatus = Literal["authoritative", "estimated", "unavailable"]
LimitSource = Literal["declared", "operator", "settings_ceiling"]

_BUDGET_KEYS = frozenset({"tokens", "cost_usd", "wall_clock_s"})


class BudgetError(ValueError):
    """A declared budget is malformed or cannot be honoured; names the field."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"budget.{field}: {message}")
        self.field = field


def worker_run_id(parent_run_id: str, pool_id: str, task_id: str) -> str:
    """The ledger run id for one worker: selectable by pool and by worker."""
    return f"{parent_run_id}:{pool_id}:subagent:{task_id}"


def _pool_marker(pool_id: str) -> str:
    return f":{pool_id}:subagent:"


@dataclass(frozen=True, slots=True)
class PoolUsage:
    """Completed ledger spend for one pool or one worker."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    cost_status: CostStatus = "unavailable"

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens — the quantity a ``tokens`` budget limits."""
        return self.input_tokens + self.output_tokens

    @property
    def cost_known(self) -> bool:
        return self.cost_status != "unavailable"

    def to_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 6) if self.cost_known else None,
            "cost_status": self.cost_status,
        }


def _usage_of(records: list[UsageRecord]) -> PoolUsage:
    from agenthicc.runners.usage_ledger import CostStatus as LedgerCostStatus

    if not records:
        return PoolUsage()
    costed = [record for record in records if record.cost_usd is not None]
    if len(costed) != len(records) or any(
        record.cost_status == LedgerCostStatus.UNAVAILABLE for record in records
    ):
        cost_status: CostStatus = "unavailable"
    elif any(record.cost_status == LedgerCostStatus.ESTIMATED for record in costed):
        cost_status = "estimated"
    else:
        cost_status = "authoritative"
    return PoolUsage(
        input_tokens=sum(record.input_tokens or 0 for record in records),
        output_tokens=sum(record.output_tokens or 0 for record in records),
        cache_read_tokens=sum(record.cache_read_tokens or 0 for record in records),
        cache_write_tokens=sum(record.cache_write_tokens or 0 for record in records),
        calls=len(records),
        cost_usd=sum(record.cost_usd or 0.0 for record in costed),
        cost_status=cost_status,
    )


def pool_usage(ledger: UsageLedger | None, pool_id: str) -> PoolUsage:
    """Completed spend of every worker in *pool_id*; provisional calls excluded.

    Excluding provisional records keeps the total monotone while workers run
    and stops a reconciled call from being counted twice.
    """
    if ledger is None or not pool_id:
        return PoolUsage()
    marker = _pool_marker(pool_id)
    return _usage_of([record for record in ledger.records() if marker in record.run_id])


def worker_usage(ledger: UsageLedger | None, pool_id: str, task_id: str) -> PoolUsage:
    """Completed spend of one worker of *pool_id*, across all of its attempts."""
    if ledger is None or not pool_id:
        return PoolUsage()
    suffix = f"{_pool_marker(pool_id)}{task_id}"
    return _usage_of([record for record in ledger.records() if record.run_id.endswith(suffix)])


def model_has_known_price(model: str) -> bool:
    """Whether *model* has a real price rather than lauren-ai's flat fallback.

    lauren-ai prices an unlisted model at a default rate instead of failing.
    A cost budget metered against that default would be a guess, so it is
    refused for an unpriced model.
    """
    from lauren_ai import _transport

    return _transport._price_for_model(model) is not _transport._DEFAULT_PRICE


@dataclass(frozen=True, slots=True)
class PoolBudget:
    """Limits for one pool; ``None`` means the unit is not limited here."""

    tokens: int | None = None
    cost_usd: float | None = None
    wall_clock_s: float | None = None

    @property
    def declared(self) -> bool:
        return any(value is not None for value in (self.tokens, self.cost_usd, self.wall_clock_s))

    def to_dict(self) -> dict[str, object]:
        return {
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "wall_clock_s": self.wall_clock_s,
        }


def _positive_number(field: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError(field, "must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise BudgetError(field, "must be a finite number greater than zero")
    return number


def parse_budget(value: object) -> PoolBudget:
    """Validate a model-supplied ``budget:`` mapping; ``None`` declares nothing."""
    if value is None:
        return PoolBudget()
    if not isinstance(value, Mapping):
        raise BudgetError("budget", "must be an object with tokens, cost_usd or wall_clock_s")
    unknown = sorted(str(key) for key in value if key not in _BUDGET_KEYS)
    if unknown:
        raise BudgetError(unknown[0], "is not a budget key (use tokens, cost_usd, wall_clock_s)")
    tokens_value = value.get("tokens")
    tokens: int | None = None
    if tokens_value is not None:
        if isinstance(tokens_value, bool) or not isinstance(tokens_value, int):
            raise BudgetError("tokens", "must be an integer")
        if tokens_value < 1:
            raise BudgetError("tokens", "must be greater than zero")
        tokens = tokens_value
    cost_value = value.get("cost_usd")
    cost = _positive_number("cost_usd", cost_value) if cost_value is not None else None
    wall_value = value.get("wall_clock_s")
    wall = _positive_number("wall_clock_s", wall_value) if wall_value is not None else None
    return PoolBudget(tokens=tokens, cost_usd=cost, wall_clock_s=wall)


@dataclass(frozen=True, slots=True)
class EffectiveBudget:
    """The limits a pool runs under, and where each one came from."""

    limits: PoolBudget
    declared: PoolBudget
    sources: Mapping[str, LimitSource]
    clamped: Mapping[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "limits": self.limits.to_dict(),
            "declared": self.declared.to_dict(),
            "sources": dict(self.sources),
            "clamped_from": dict(self.clamped),
        }


def resolve_budget(
    declared: PoolBudget,
    *,
    ceiling: PoolBudget,
    operator_tokens: int,
    operator_wall_clock_s: float,
) -> EffectiveBudget:
    """Combine a declaration with the settings ceiling and the operator limits.

    A declaration above ``ceiling`` is lowered to it and reported in
    ``clamped``. A declared ``tokens`` or ``wall_clock_s`` takes effect only
    when it is lower than the operator ceiling of the same unit; cost has no
    operator ceiling, so a declared cost is the only cost limit.
    """
    clamped: dict[str, float] = {}

    def clamp(name: str, value: float | None, cap: float | None) -> float | None:
        if value is None or cap is None or value <= cap:
            return value
        clamped[name] = value
        return cap

    tokens = clamp(
        "tokens",
        declared.tokens,
        ceiling.tokens,
    )
    cost = clamp("cost_usd", declared.cost_usd, ceiling.cost_usd)
    wall = clamp("wall_clock_s", declared.wall_clock_s, ceiling.wall_clock_s)

    sources: dict[str, LimitSource] = {}
    if tokens is not None and tokens < operator_tokens:
        effective_tokens = int(tokens)
        sources["tokens"] = "settings_ceiling" if "tokens" in clamped else "declared"
    else:
        effective_tokens = operator_tokens
        sources["tokens"] = "operator"
    if wall is not None and wall < operator_wall_clock_s:
        effective_wall = float(wall)
        sources["wall_clock_s"] = "settings_ceiling" if "wall_clock_s" in clamped else "declared"
    else:
        effective_wall = operator_wall_clock_s
        sources["wall_clock_s"] = "operator"
    if cost is not None:
        sources["cost_usd"] = "settings_ceiling" if "cost_usd" in clamped else "declared"
    return EffectiveBudget(
        limits=PoolBudget(tokens=effective_tokens, cost_usd=cost, wall_clock_s=effective_wall),
        declared=declared,
        sources=sources,
        clamped=clamped,
    )


#: Which budget unit each stop reason crossed; the rest are operator-only units.
_REASON_UNITS: Mapping[str, str] = {
    "max_tokens_exceeded": "tokens",
    "max_total_time_s_exceeded": "wall_clock_s",
    "max_cost_usd_exceeded": "cost_usd",
    "max_tool_calls_exceeded": "tool_calls",
    "max_iterations_exceeded": "iterations",
}


def budget_report(
    usage: PoolUsage,
    budget: EffectiveBudget | None,
    *,
    crossed_reason: str = "",
    stop_mode: str = "",
) -> dict[str, object]:
    """Usage against limits, for the tool result and the completion events.

    Counts and limits only — no prompt or result text — so it can be logged
    without a redaction pass. ``crossed`` names the limit that stopped the
    pool and where it came from; the stop itself is SUBAGENT-08's policy.
    """
    report: dict[str, object] = {"usage": usage.to_dict()}
    if budget is not None:
        report.update(budget.to_dict())
    if crossed_reason:
        unit = _REASON_UNITS.get(crossed_reason, "")
        source = budget.sources.get(unit, "operator") if budget is not None else "operator"
        report["crossed"] = {
            "reason": crossed_reason,
            "unit": unit,
            "source": source,
            "stop_mode": stop_mode,
        }
    return report


def _limit_text(unit: str, value: float) -> str:
    if unit == "tokens":
        return f"tokens {int(value):,}"
    if unit == "cost_usd":
        return f"cost ${value:.4f}"
    return f"wall clock {value:g}s"


def budget_digest(
    usage: PoolUsage,
    budget: EffectiveBudget | None,
    *,
    crossed_reason: str = "",
    stop_mode: str = "",
) -> str:
    """One line for the model: what the pool spent, against which limits.

    Returned only when a budget was declared or a limit was crossed, so a
    pool that declared nothing keeps exactly the digest it always had.
    """
    declared = budget is not None and budget.declared.declared
    if not declared and not crossed_reason:
        return ""
    cost = (
        f"cost ${usage.cost_usd:.4f} ({usage.cost_status})"
        if usage.cost_known
        else "cost unavailable"
    )
    parts = [
        f"[pool usage] {usage.total_tokens:,} tokens "
        f"(in {usage.input_tokens:,} / out {usage.output_tokens:,}), {cost}"
    ]
    if budget is not None:
        limits = []
        for unit, value in (
            ("tokens", budget.limits.tokens),
            ("cost_usd", budget.limits.cost_usd),
            ("wall_clock_s", budget.limits.wall_clock_s),
        ):
            if value is None:
                continue
            limits.append(f"{_limit_text(unit, value)} ({budget.sources.get(unit, 'operator')})")
        if limits:
            parts.append("limits: " + ", ".join(limits))
        if budget.clamped:
            parts.append(
                "clamped to subagents.max_budget: "
                + ", ".join(
                    f"{unit} requested {value:g}" for unit, value in sorted(budget.clamped.items())
                )
            )
    if crossed_reason:
        unit = _REASON_UNITS.get(crossed_reason, "")
        source = budget.sources.get(unit, "operator") if budget is not None else "operator"
        parts.append(
            f"CROSSED {crossed_reason} ({source} limit); the pool's stop policy "
            f"({stop_mode or 'drain'}) decided what happened to its workers"
        )
    return "; ".join(parts) + "."
