"""Every workflow turn takes queued input (operator request).

A workflow can run for hours, so a message typed meanwhile must reach it at a
tool boundary. The claim hook travels in ``WorkflowConfig.next_queued_message``
and every workflow starts its turns through ``_run_agent_turn``; a call that
leaves the hook out would silently strand the queue until the run ends.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_WORKFLOWS = Path(__file__).resolve().parents[2] / "src" / "agenthicc" / "workflows"
#: Names that start a turn: the function and the options dict a runner spreads into it.
_TURN_STARTERS = {"_run_agent_turn"}


def _turn_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _TURN_STARTERS
    ]


def _mentions_claim(node: ast.AST) -> bool:
    return any(
        (isinstance(child, ast.keyword) and child.arg == "next_queued_message")
        or (isinstance(child, ast.Constant) and child.value == "next_queued_message")
        for child in ast.walk(node)
    )


def test_every_workflow_turn_carries_the_queued_input_hook() -> None:
    offenders: list[str] = []
    started = 0
    for path in sorted(_WORKFLOWS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = _turn_calls(tree)
        if not calls:
            continue
        started += len(calls)
        # A call may spread an options dict built in the same module.
        module_has_claim = _mentions_claim(tree)
        for call in calls:
            spreads = any(keyword.arg is None for keyword in call.keywords)
            if not (_mentions_claim(call) or (spreads and module_has_claim)):
                offenders.append(f"{path.relative_to(_WORKFLOWS)}:{call.lineno}")
    assert started >= 3, "the scan found no workflow turns; the test is stale"
    assert offenders == [], f"workflow turns without next_queued_message: {offenders}"


def test_every_built_in_workflow_runs_its_turns_through_a_checked_runner() -> None:
    """Runners that do not call ``_run_agent_turn`` must inherit ``run_phase``."""
    from agenthicc.workflows.base_runner import BaseWorkflowRunner
    from agenthicc.workflows.code_plan.runner import CodePlanRunner

    runner_modules = {
        path.parent.name
        for path in _WORKFLOWS.rglob("runner.py")
        if "_run_agent_turn" in path.read_text(encoding="utf-8")
    }
    # code_plan, default and create_workflow start turns themselves; the rest
    # delegate to a CodePlanRunner (inheriting or composing it).
    assert {"code_plan", "default", "create_workflow"} <= runner_modules
    for path in sorted(_WORKFLOWS.rglob("runner.py")):
        if path.parent.name in runner_modules:
            continue
        source = path.read_text(encoding="utf-8")
        assert any(
            name in source for name in ("CodePlanRunner", "PhaseRunner", "run_authoring_phase")
        ), path
    assert issubclass(CodePlanRunner, BaseWorkflowRunner)
