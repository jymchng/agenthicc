"""Unit coverage for the bounded generated-workflow smoke contract."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities
from agenthicc.tools.base import tool_name
from agenthicc.workflows.create_workflow.inspection_tools import _RUNNER_EXAMPLE
from agenthicc.workflows.create_workflow.smoke import _fake_config, run_generated_workflow_smoke
from agenthicc.workflows.loader import load_python_workflows

pytestmark = pytest.mark.unit


def test_custom_runner_smoke_exercises_fake_runtime_and_checkpoint(tmp_path: Path) -> None:
    workflow = tmp_path / "release_check"
    workflow.mkdir()
    (workflow / "runner.py").write_text(_RUNNER_EXAMPLE, encoding="utf-8")

    report = run_generated_workflow_smoke(
        workflow,
        expected_name="release_check",
        root=tmp_path,
    )
    assert report.ok is True, report.render()
    statuses = {check.category: check.status for check in report.checks}
    assert statuses["fake_runtime"] == "pass"
    assert statuses["failure_finalization"] == "pass"
    assert statuses["no_external_calls"] == "pass"
    assert statuses["record_only_transition"] == "pass"


async def test_multiroute_smoke_uses_selected_tool_outcome(tmp_path: Path) -> None:
    workflow = tmp_path / "release_check"
    workflow.mkdir()
    (workflow / "runner.py").write_text(_RUNNER_EXAMPLE, encoding="utf-8")
    plugins = load_python_workflows(workflow, source="test")
    plugin = next(item for item in plugins if item.name == "release_check")
    config = _fake_config()
    runner = plugin.build_runner(config, None)
    calls: dict[str, int] = {}

    async def fake_run_phase(**kwargs: object) -> None:
        prompt = str(kwargs.get("system_prompt", "")).lower()
        phase = next(
            (
                name
                for name in ("plan", "verify", "report")
                if f"you are in the {name} phase" in prompt
            ),
            "phase",
        )
        calls[phase] = calls.get(phase, 0) + 1
        if calls[phase] == 1:
            return
        tools = kwargs.get("tools")
        if not isinstance(tools, list):
            return
        controls = [
            candidate
            for candidate in tools
            if ToolCapability.CONTROL in get_tool_capabilities(candidate)
            and tool_name(candidate) != "ask_user"
        ]
        if phase == "verify":
            selected = next(item for item in controls if tool_name(item) == "release_blocked")
        elif controls:
            selected = controls[0]
        else:
            return
        result = selected(summary="Selected blocked route")
        if inspect.isawaitable(result):
            await result

    runner.run_phase = fake_run_phase
    context = await runner.run("exercise the blocked release route")

    assert getattr(getattr(context, "state", None), "name", "") == "COMPLETE"
    assert getattr(context, "blocker", "") == "Selected blocked route"
    assert getattr(context, "verdict", "") == ""


def test_smoke_rejects_external_service_imports(tmp_path: Path) -> None:
    workflow = tmp_path / "unsafe"
    workflow.mkdir()
    (workflow / "runner.py").write_text(
        "import socket\n\nfrom agenthicc.workflows.plugin import WorkflowPlugin\n",
        encoding="utf-8",
    )
    report = run_generated_workflow_smoke(workflow, root=tmp_path)
    assert report.ok is False
    assert any(
        check.category == "no_external_calls" and check.status == "fail" for check in report.checks
    )
