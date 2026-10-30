"""Require an approval label when a performance-budget gate is weakened."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agenthicc.performance import (
    DEFAULT_BUDGET_PATH,
    _read_budgets,
    check_budget_changes,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="budget JSON from the PR base")
    parser.add_argument("--current", type=Path, default=DEFAULT_BUDGET_PATH)
    parser.add_argument(
        "--increase-approved",
        action="store_true",
        help="the pull request has the perf-budget-increase label",
    )
    args = parser.parse_args(argv)
    try:
        baseline = _read_budgets(args.baseline)
        current = _read_budgets(args.current)
        problems = check_budget_changes(
            baseline,
            current,
            increase_approved=args.increase_approved,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Performance budget policy check failed: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("Performance budget changes comply with the approval policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
