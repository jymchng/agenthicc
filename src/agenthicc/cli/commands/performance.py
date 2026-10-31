"""Offline performance diagnostics command (AGX-14)."""

from __future__ import annotations

from agenthicc.cli.context import CLIContext


def perf_report(ctx: CLIContext) -> None:
    """Run fixture-free performance probes and compare them with budgets."""
    del ctx
    from agenthicc.performance import report_local

    report_local()
