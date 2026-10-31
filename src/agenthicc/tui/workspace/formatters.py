"""Presentation-only formatters shared by the TUI status surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.subagents.pool import SubagentPoolState

_TOKEN_UNITS = ("k", "M", "B", "T", "P", "E", "Z", "Y", "R", "Q")


def compact_token_count(count: int) -> str:
    """Return a rounded display value without changing the canonical count."""
    count = max(0, count)
    if count < 1_000:
        return str(count)

    divisor = 1_000
    unit_index = 0
    while True:
        tenths = (count * 10 + divisor // 2) // divisor
        if tenths < 10_000 or unit_index == len(_TOKEN_UNITS) - 1:
            whole, decimal = divmod(tenths, 10)
            number = f"{whole}.{decimal}"
            if unit_index > 0 and decimal == 0:
                number = str(whole)
            return f"{number}{_TOKEN_UNITS[unit_index]}"
        divisor *= 1_000
        unit_index += 1


def compact_duration(seconds: float) -> str:
    """Format elapsed seconds with concise, non-redundant time units."""
    remaining = max(0, int(seconds))
    hours, remaining = divmod(remaining, 3_600)
    minutes, seconds_part = divmod(remaining, 60)
    units: list[str] = []
    if hours:
        units.append(f"{hours}h")
    if minutes:
        units.append(f"{minutes}m")
    if seconds_part or not units:
        units.append(f"{seconds_part}s")
    return " ".join(units)


def wrap_display_text(value: str, width: int) -> list[str]:
    """Wrap plain display text by terminal cells without discarding content."""
    from rich.console import Console
    from rich.text import Text

    columns = max(1, width)
    console = Console(width=columns, color_system=None, force_terminal=False)
    return [line.plain for line in Text(value).wrap(console, width=columns, overflow="fold")]


def identity_rows(session_id: str, current_directory: str, width: int) -> list[str]:
    """Render complete session and directory identities as width-safe rows."""
    rows = wrap_display_text(session_id, width) if session_id else []
    if current_directory:
        rows.extend(wrap_display_text(current_directory, width))
    return rows


def usage_quality_label(status: str, *, screen_reader: bool, compact: bool = False) -> str:
    """Describe non-complete token usage without implying missing values are zero."""
    if status == "complete":
        return ""
    if status == "partial":
        if screen_reader or not compact:
            return "usage partial"
        return "?"
    return "usage unavailable"


def cost_label(cost: float, status: str, *, screen_reader: bool) -> str:
    """Format cost independently from tokens while retaining its provenance."""
    if status not in {"authoritative", "estimated"}:
        return "cost unknown"
    formatted = f"${cost:.3f}"
    if status == "estimated":
        return f"estimated cost {formatted}" if screen_reader else f"~{formatted}"
    return f"cost {formatted}" if screen_reader else formatted


@dataclass(frozen=True, slots=True)
class PoolStatusCounts:
    """Counts derived from one immutable worker snapshot."""

    running: int
    queued: int
    needs_input: int
    done: int
    failed: int
    stopped: int


def pool_status_counts(state: SubagentPoolState) -> PoolStatusCounts:
    """Count worker statuses without maintaining a second UI counter."""
    workers = state.workers
    return PoolStatusCounts(
        running=sum(worker.status == "running" for worker in workers),
        queued=sum(worker.status == "pending" for worker in workers),
        needs_input=sum(worker.status == "waiting" for worker in workers),
        done=sum(worker.status == "done" for worker in workers),
        failed=sum(worker.status == "failed" for worker in workers),
        stopped=sum(worker.status == "stopped" for worker in workers),
    )
