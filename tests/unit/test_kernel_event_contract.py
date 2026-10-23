"""Every kernel event the product emits is declared, and every declaration is emitted.

The reducer applies the types in ``REDUCED_EVENT_TYPES``; subscribers consume
the projection-only types in ``PROJECTION_EVENT_TYPES``; anything else is
rejected at runtime. This test reads the source and resolves the event type of
every ``Event.create(...)`` call, so an undeclared type fails here instead of
being rejected in a live session, and a handler or projection type that
nothing emits is reported as dead.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from agenthicc.kernel import PROJECTION_EVENT_TYPES, REDUCED_EVENT_TYPES

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "agenthicc"

#: Emitted by the processor itself, not through ``Event.create`` in a producer.
PROCESSOR_OWNED = frozenset({"KernelEventRejected"})


@dataclass(frozen=True)
class EmitSite:
    path: Path
    line: int
    event_types: frozenset[str]

    def __str__(self) -> str:
        return f"{self.path.relative_to(SRC.parent)}:{self.line}"


def _string_options(node: ast.expr) -> frozenset[str] | None:
    """The string values *node* can take, if it is built only from literals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset({node.value})
    if isinstance(node, ast.IfExp):
        body, orelse = _string_options(node.body), _string_options(node.orelse)
        if body is not None and orelse is not None:
            return body | orelse
    return None


def _is_event_create(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "create"
        and isinstance(func.value, ast.Name)
        and func.value.id == "Event"
    )


def _event_type_argument(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    return next((kw.value for kw in call.keywords if kw.arg == "event_type"), None)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _resolve_name(
    name: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    tree: ast.Module,
) -> frozenset[str] | None:
    """Resolve a local assigned from literals, or a parameter forwarded by callers."""
    assigned: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            options = _string_options(node.value)
            if options is None:
                return None
            assigned |= options
    if assigned:
        return frozenset(assigned)

    positional = [arg.arg for arg in function.args.args]
    if name not in positional:
        return None
    index = positional.index(name)
    if positional and positional[0] in {"self", "cls"}:
        index -= 1
    forwarded: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if callee != function.name:
            continue
        if len(node.args) > index:
            argument: ast.expr | None = node.args[index]
        else:
            argument = next((kw.value for kw in node.keywords if kw.arg == name), None)
        options = _string_options(argument) if argument is not None else None
        if options is None:
            return None
        forwarded |= options
    return frozenset(forwarded) if forwarded else None


def _emit_sites() -> tuple[list[EmitSite], list[str]]:
    sites: list[EmitSite] = []
    unresolved: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "Event.create" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_event_create(node)):
                continue
            argument = _event_type_argument(node)
            options = _string_options(argument) if argument is not None else None
            if options is None and isinstance(argument, ast.Name):
                function = _enclosing_function(node, parents)
                if function is not None:
                    options = _resolve_name(argument.id, function, tree)
            location = f"{path.relative_to(SRC.parent)}:{node.lineno}"
            if options is None:
                unresolved.append(location)
            else:
                sites.append(EmitSite(path, node.lineno, options))
    return sites, unresolved


SITES, UNRESOLVED = _emit_sites()
EMITTED = frozenset().union(*(site.event_types for site in SITES))


def test_every_emit_site_has_a_statically_known_event_type() -> None:
    assert SITES, "the scan found no Event.create calls"
    assert not UNRESOLVED, (
        "Event.create must receive a literal, a conditional of literals, a local "
        "assigned from literals, or a parameter its callers pass literals to: "
        + ", ".join(UNRESOLVED)
    )


def test_every_emitted_event_type_is_declared() -> None:
    declared = REDUCED_EVENT_TYPES | PROJECTION_EVENT_TYPES
    undeclared = {
        str(site): sorted(site.event_types - declared)
        for site in SITES
        if site.event_types - declared
    }
    assert not undeclared, undeclared


def test_no_declared_event_type_is_dead() -> None:
    assert not (REDUCED_EVENT_TYPES - EMITTED), "reducer handlers nothing emits"
    assert not (PROJECTION_EVENT_TYPES - EMITTED - PROCESSOR_OWNED), (
        "projection-only types nothing emits"
    )


def test_reduced_and_projection_types_are_disjoint() -> None:
    assert not (REDUCED_EVENT_TYPES & PROJECTION_EVENT_TYPES)
