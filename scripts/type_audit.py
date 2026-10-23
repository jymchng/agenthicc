#!/usr/bin/env python3
"""Measure and ratchet source typing hygiene.

This audit complements mypy. Mypy reports semantic type errors, while this
script keeps a small set of repository policy metrics visible even when a
dynamic construct happens to type-check. It intentionally has no third-party
dependencies so it can run during bootstrap and in Nox.

The baseline is a ratchet that holds in both directions. A debt metric above
its baseline is a regression; one below it means the baseline is stale and
must be lowered with ``--update``, so an improvement cannot silently erode.
``--baseline-ref`` additionally rejects a baseline file that raises any value
compared with a git revision, so the ratchet cannot be loosened by editing it.

A module-level ``# mypy:`` directive switches checks off for a whole file. It
is accepted only as time-boxed debt: the line directly above it must read
``# type-debt: owner=<owner>; expires=<YYYY-MM-DD>``, and the audit fails once
that date has passed.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


SOURCE_ROOT = Path("src/agenthicc")

#: Counts that describe the code base rather than debt. Adding a typed adapter
#: or validator legitimately raises them, so they are reported, never ratcheted.
INVENTORY_METRICS = frozenset({"source_files", "functions"})

#: mypy reads an inline configuration comment only at the start of a line.
_MYPY_DIRECTIVE = "# mypy: "
_TYPE_DEBT = re.compile(
    r"^# type-debt: owner=(?P<owner>[^;]+?); expires=(?P<expires>\d{4}-\d{2}-\d{2})$"
)


@dataclass(frozen=True)
class ModuleSuppression:
    """One module-level ``# mypy:`` directive and the time box above it."""

    path: Path
    line: int
    options: str
    owner: str | None
    expires: datetime.date | None

    def problem(self, today: datetime.date) -> str | None:
        """Return why this suppression is not acceptable on *today*, if it is not."""
        if self.owner is None or self.expires is None:
            return (
                "has no time box; put '# type-debt: owner=<owner>; expires=<YYYY-MM-DD>' "
                "on the line above it"
            )
        if self.expires < today:
            return f"expired on {self.expires.isoformat()} (owner: {self.owner})"
        return None


def _contains_name(node: ast.AST | None, name: str) -> bool:
    return node is not None and any(
        isinstance(part, ast.Name) and part.id == name for part in ast.walk(node)
    )


def _is_bare_annotation(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    annotations: list[ast.AST] = [arg.annotation for arg in args if arg.annotation is not None]
    if node.args.vararg and node.args.vararg.annotation is not None:
        annotations.append(node.args.vararg.annotation)
    if node.args.kwarg and node.args.kwarg.annotation is not None:
        annotations.append(node.args.kwarg.annotation)
    if node.returns is not None:
        annotations.append(node.returns)
    return annotations


def _module_suppressions(path: Path, source: str) -> list[ModuleSuppression]:
    lines = source.split("\n")
    found: list[ModuleSuppression] = []
    for index, line in enumerate(lines):
        if not line.startswith(_MYPY_DIRECTIVE):
            continue
        marker = _TYPE_DEBT.match(lines[index - 1].strip()) if index > 0 else None
        expires: datetime.date | None = None
        if marker is not None:
            try:
                expires = datetime.date.fromisoformat(marker["expires"])
            except ValueError:
                expires = None
        found.append(
            ModuleSuppression(
                path=path,
                line=index + 1,
                options=line[len(_MYPY_DIRECTIVE) :].strip(),
                owner=marker["owner"].strip() if marker is not None else None,
                expires=expires,
            )
        )
    return found


def collect_suppressions(root: Path = SOURCE_ROOT) -> list[ModuleSuppression]:
    """Return every module-level ``# mypy:`` directive below *root*."""
    found: list[ModuleSuppression] = []
    for path in sorted(root.rglob("*.py")):
        found.extend(_module_suppressions(path, path.read_text(encoding="utf-8")))
    return found


def collect_metrics(root: Path = SOURCE_ROOT) -> dict[str, int]:
    """Return policy metrics for Python files below *root*."""
    metrics = {
        "source_files": 0,
        "functions": 0,
        "functions_with_missing_annotations": 0,
        "missing_parameter_annotations": 0,
        "missing_return_annotations": 0,
        "explicit_any_annotations": 0,
        "bare_list_annotations": 0,
        "bare_dict_annotations": 0,
        "getattr_calls": 0,
        "hasattr_calls": 0,
        "type_ignore_comments": 0,
        "mypy_module_suppressions": 0,
    }

    for path in sorted(root.rglob("*.py")):
        metrics["source_files"] += 1
        source = path.read_text(encoding="utf-8")
        metrics["type_ignore_comments"] += source.count("# type: ignore")
        metrics["mypy_module_suppressions"] += len(_module_suppressions(path, source))
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                metrics["functions"] += 1
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if node.args.vararg:
                    args.append(node.args.vararg)
                if node.args.kwarg:
                    args.append(node.args.kwarg)
                required_args = [arg for arg in args if arg.arg not in {"self", "cls"}]
                missing_params = any(arg.annotation is None for arg in required_args)
                missing_return = node.returns is None
                metrics["missing_parameter_annotations"] += missing_params
                metrics["missing_return_annotations"] += missing_return
                metrics["functions_with_missing_annotations"] += missing_params or missing_return
                annotations = _annotations(node)
            elif isinstance(node, ast.AnnAssign):
                annotations = [node.annotation]
            else:
                annotations = []

            for annotation in annotations:
                metrics["explicit_any_annotations"] += _contains_name(annotation, "Any")
                metrics["bare_list_annotations"] += _is_bare_annotation(annotation, "list")
                metrics["bare_dict_annotations"] += _is_bare_annotation(annotation, "dict")

            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"getattr", "hasattr"}:
                    metrics[f"{node.func.id}_calls"] += 1

    return {key: int(value) for key, value in metrics.items()}


def debt_metrics(metrics: Mapping[str, int]) -> dict[str, int]:
    """Return only the ratcheted metrics, sorted by name."""
    return {key: metrics[key] for key in sorted(metrics) if key not in INVENTORY_METRICS}


def _parse_baseline(text: str, origin: str) -> dict[str, int]:
    data: object = json.loads(text)
    if not isinstance(data, dict) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in data.values()
    ):
        raise ValueError(f"{origin} must contain a JSON object of integer metrics")
    return {str(key): int(value) for key, value in data.items()}


def _load_baseline(path: Path) -> dict[str, int]:
    return _parse_baseline(path.read_text(encoding="utf-8"), str(path))


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True)


def _load_baseline_at(ref: str, path: Path) -> dict[str, int] | None:
    """Return the baseline committed at *ref*, or ``None`` when it has none there.

    An unknown revision is an error rather than "no baseline": silently
    skipping the comparison would disable the guard it exists to provide.
    """
    if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise ValueError(f"--baseline-ref {ref!r} is not a commit in this repository")
    toplevel = _git("rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        raise ValueError("--baseline-ref needs a git working tree")
    relative = path.resolve().relative_to(Path(toplevel.stdout.strip()).resolve()).as_posix()
    committed = _git("show", f"{ref}:{relative}")
    if committed.returncode != 0:
        return None
    return _parse_baseline(committed.stdout, f"{ref}:{relative}")


def check(
    metrics: Mapping[str, int],
    baseline: Mapping[str, int],
    *,
    suppressions: list[ModuleSuppression],
    today: datetime.date,
    previous_baseline: Mapping[str, int] | None = None,
) -> list[str]:
    """Return every ratchet violation; an empty list means the audit passes."""
    problems: list[str] = []
    current = debt_metrics(metrics)
    for key, value in current.items():
        if key not in baseline:
            problems.append(f"{key}: missing from the baseline (current={value})")
        elif value > baseline[key]:
            problems.append(f"{key}: regressed (baseline={baseline[key]}, current={value})")
        elif value < baseline[key]:
            problems.append(
                f"{key}: baseline is stale (baseline={baseline[key]}, current={value}); "
                "lower it with --update"
            )
    for key in sorted(set(baseline) - set(current)):
        problems.append(f"{key}: not a ratcheted metric; remove it from the baseline")
    if previous_baseline is not None:
        for key, value in sorted(baseline.items()):
            before = previous_baseline.get(key)
            if before is not None and value > before:
                problems.append(
                    f"{key}: the baseline was raised from {before} to {value}; "
                    "raising a ratchet needs explicit owner approval"
                )
    for suppression in suppressions:
        problem = suppression.problem(today)
        if problem is not None:
            problems.append(
                f"{suppression.path}:{suppression.line}: '# mypy: {suppression.options}' {problem}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check", type=Path, metavar="BASELINE", help="fail on any ratchet violation"
    )
    action.add_argument(
        "--update", type=Path, metavar="BASELINE", help="write current debt metrics"
    )
    parser.add_argument(
        "--baseline-ref",
        metavar="REF",
        help="with --check: also fail if BASELINE raises a value committed at this git revision",
    )
    parser.add_argument("--root", type=Path, default=SOURCE_ROOT)
    parser.add_argument(
        "--today",
        type=datetime.date.fromisoformat,
        default=datetime.date.today(),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    metrics = collect_metrics(args.root)

    if args.update is not None:
        args.update.write_text(
            json.dumps(debt_metrics(metrics), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Wrote {len(debt_metrics(metrics))} debt metrics to {args.update}.")
        return 0

    if args.check is None:
        if args.baseline_ref is not None:
            parser.error("--baseline-ref requires --check")
        print(json.dumps(metrics, indent=2, sort_keys=True))
        return 0

    try:
        previous = (
            _load_baseline_at(args.baseline_ref, args.check)
            if args.baseline_ref is not None
            else None
        )
    except ValueError as exc:
        print(f"Type audit failed: {exc}", file=sys.stderr)
        return 1
    problems = check(
        metrics,
        _load_baseline(args.check),
        suppressions=collect_suppressions(args.root),
        today=args.today,
        previous_baseline=previous,
    )
    if problems:
        print("Type audit failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"Type audit OK — {len(debt_metrics(metrics))} debt metrics match {args.check}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
