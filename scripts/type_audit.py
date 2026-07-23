#!/usr/bin/env python3
"""Measure and ratchet source typing hygiene.

This audit complements mypy. Mypy reports semantic type errors, while this
script keeps a small set of repository policy metrics visible even when a
dynamic construct happens to type-check. It intentionally has no third-party
dependencies so it can run during bootstrap and in Nox.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


SOURCE_ROOT = Path("src/agenthicc")


def _contains_name(node: ast.AST | None, name: str) -> bool:
    return node is not None and any(
        isinstance(part, ast.Name) and part.id == name for part in ast.walk(node)
    )


def _is_bare_annotation(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    annotations = [arg.annotation for arg in args if arg.annotation is not None]
    if node.args.vararg and node.args.vararg.annotation is not None:
        annotations.append(node.args.vararg.annotation)
    if node.args.kwarg and node.args.kwarg.annotation is not None:
        annotations.append(node.args.kwarg.annotation)
    if node.returns is not None:
        annotations.append(node.returns)
    return annotations


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
    }

    for path in sorted(root.rglob("*.py")):
        metrics["source_files"] += 1
        source = path.read_text(encoding="utf-8")
        metrics["type_ignore_comments"] += source.count("# type: ignore")
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


def _load_baseline(path: Path) -> dict[str, int]:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(value, int) for value in data.values()):
        raise ValueError(f"{path} must contain a JSON object of integer metrics")
    return {str(key): int(value) for key, value in data.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, metavar="BASELINE", help="fail if metrics regress")
    parser.add_argument("--root", type=Path, default=SOURCE_ROOT)
    args = parser.parse_args()
    metrics = collect_metrics(args.root)

    if args.check is None:
        print(json.dumps(metrics, indent=2, sort_keys=True))
        return 0

    baseline = _load_baseline(args.check)
    # File/function counts are inventory context, not debt: adding a typed
    # adapter or validator legitimately increases them.  The ratchet applies
    # only to practices that can erase contracts or hide checker failures.
    debt_metrics = {key for key in metrics if key not in {"source_files", "functions"}}
    regressions = {
        key: (baseline.get(key, 0), value)
        for key, value in metrics.items()
        if key in debt_metrics
        if value > baseline.get(key, 0)
    }
    if regressions:
        print("Type audit regression:", file=sys.stderr)
        for key, (before, after) in sorted(regressions.items()):
            print(f"  {key}: baseline={before}, current={after}", file=sys.stderr)
        return 1

    print(f"Type audit OK — {len(metrics)} metrics do not exceed {args.check}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
