"""Static audit for cache-contract boundaries in built-in workflow runners."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_WORKFLOW_ROOT = Path(__file__).resolve().parents[2] / "src" / "agenthicc" / "workflows"


def _calls(tree: ast.AST, method_name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
    ]


def test_every_builtin_workflow_turn_uses_the_cache_contract_boundary() -> None:
    """No built-in runner may silently bypass stable/dynamic prompt separation."""

    runner_paths = sorted(_WORKFLOW_ROOT.glob("*/runner.py"))
    assert runner_paths

    for path in runner_paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        phase_calls = _calls(tree, "run_phase")

        for call in phase_calls:
            stable_keyword = next(
                (keyword for keyword in call.keywords if keyword.arg == "stable_system_prompt"),
                None,
            )
            assert stable_keyword is not None, f"{path}: run_phase() lacks stable prompt"
            assert isinstance(stable_keyword.value, ast.Name), (
                f"{path}: stable_system_prompt must use a module-level contract constant"
            )

        direct_turns = _calls(tree, "_run_agent_turn")
        if direct_turns:
            # The two runners with direct calls build the same structured
            # contract explicitly before invoking the low-level turn API.
            assert "build_workflow_prompt_contract" in source, (
                f"{path}: direct agent turns must build a prompt contract"
            )
            assert "prompt_contract" in source, (
                f"{path}: direct agent turns must pass/retain prompt-contract state"
            )

        if path.parent.name in {"default", "code_plan", "create_workflow"}:
            assert "build_workflow_prompt_contract" in source, (
                f"{path}: built-in runner must use the shared prompt composer"
            )

        if path.parent.name == "code_plan":
            assert "stable_system_prefix=stable_system_prompt or CACHE_CONTRACT" in source
        elif path.parent.name == "create_workflow":
            assert "stable_system_prefix=CACHE_CONTRACT" in source
