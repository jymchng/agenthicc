"""Per-pool usage accounting (SUBAGENT-06).

Accounting reads the session usage ledger; it is not a second meter. Every
worker's provider calls are recorded under a run id that names its pool
(:func:`worker_run_id`), so one pool's spend is the set of completed ledger
records carrying that pool id. Cost is summed only over records with a known
cost, and a pool with any record of unknown cost reports its cost as
unavailable rather than as zero.

Subagents have no limits of any kind (operator decisions, 2026-11-07 and
2026-11-22): no token, turn, tool-call, iteration, time or cost budget. A worker
may use as many tokens, turns, tool calls, minutes and dollars as its task needs.
Usage is counted and reported, never limited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agenthicc.runners.usage_ledger import UsageLedger, UsageRecord

__all__ = [
    "PoolUsage",
    "pool_usage",
    "worker_run_id",
    "worker_usage",
]

CostStatus = Literal["authoritative", "estimated", "unavailable"]


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
        """Input plus output tokens."""
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
