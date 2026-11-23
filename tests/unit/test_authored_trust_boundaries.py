"""Keep authored trust mutations off the model's tool surface."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
_SOURCE = Path(__file__).resolve().parents[2] / "src" / "agenthicc"
_MUTATORS = frozenset({"trust", "revoke", "restore_records"})


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_model_tool_modules_do_not_reference_extension_trust_mutators() -> None:
    files = set((_SOURCE / "tools").rglob("*.py"))
    files.update(_SOURCE.rglob("*tools*.py"))
    for entry in sorted(files):
        for node in ast.walk(_tree(entry)):
            if isinstance(node, ast.ImportFrom) and node.module == "agenthicc.plugins.trust":
                prohibited = {symbol.name for symbol in node.names} & (
                    _MUTATORS | {"ExtensionTrust"}
                )
                assert not prohibited, (entry, node.lineno, prohibited)
            if isinstance(node, ast.Attribute):
                assert node.attr not in _MUTATORS, (entry, node.lineno, node.attr)


def test_extension_trust_writes_are_owned_by_runtime_and_explicit_operator_actions() -> None:
    permitted = {
        "agents/library.py",
        "cli/commands/trust.py",
        "plugins/authored.py",
        "tui/workspace/agents_hub/hub.py",
        "workflows/create_workflow/authoring.py",
    }
    for entry in sorted(_SOURCE.rglob("*.py")):
        for node in ast.walk(_tree(entry)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "trust":
                continue
            receiver = node.func.value
            if (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Name)
                and receiver.func.id == "EnvironmentTrustStore"
            ):
                continue
            assert entry.relative_to(_SOURCE).as_posix() in permitted, (entry, node.lineno)


def test_only_the_authored_store_finalizes_and_trusts_workflow_packages() -> None:
    caller_paths = {
        path.relative_to(_SOURCE).as_posix()
        for path in _SOURCE.rglob("*.py")
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "finalize_authoring"
    }
    assert caller_paths == {"plugins/authored.py"}

    authoring = _tree(_SOURCE / "workflows/create_workflow/authoring.py")
    calling_functions = {
        function.name
        for function in authoring.body
        if isinstance(function, ast.FunctionDef)
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "trust"
            for node in ast.walk(function)
        )
    }
    assert calling_functions == {"finalize_authoring"}
