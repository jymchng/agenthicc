"""Interaction tests for manager-facing and existing prompt overlays."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.config import AgenthiccConfig
from agenthicc.tools.approval import ApprovalRequest
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay
from agenthicc.tui.workspace.overlays.config_menu import ConfigMenuOverlay
from agenthicc.tui.workspace.overlays.help import HelpOverlay
from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay
from agenthicc.tui.workspace.overlays.registry_list import (
    CommandListOverlay,
    RegistryMessageOverlay,
    SkillListOverlay,
    ToolListOverlay,
    WorkflowListOverlay,
    WorkflowRunsOverlay,
)
from agenthicc.commands.command import Command
from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.skills.loader import SkillDef
from agenthicc.workflows.registry import WorkflowRegistry
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

pytestmark = pytest.mark.unit


def _request(
    *, kind: str = "tool", questions: list[dict[str, object]] | None = None
) -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="ask_user" if kind == "questions" else "write_file",
        tool_use_id="tool-1",
        tool_input={"path": "/tmp/demo", "content": "secret"}
        if questions is None
        else {"questions": questions},
        capabilities=frozenset({"write"}),
        event=asyncio.Event(),
        kind=kind,
    )


def test_approval_overlay_render_and_shortcuts() -> None:
    responses: list[dict[str, object]] = []
    closed: list[bool] = []
    service = SimpleNamespace(respond=lambda **kwargs: responses.append(kwargs))
    overlay = ApprovalOverlay(_request(), service, lambda: closed.append(True))
    overlay.on_mount()
    assert overlay.render()
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")
    assert responses[-1]["allowed"] is True
    assert closed
    overlay = ApprovalOverlay(_request(), service, lambda: closed.append(True))
    overlay.handle_key(Key.CHAR, "n")
    assert responses[-1]["allowed"] is False
    overlay.handle_key(Key.ESC, "")
    assert responses[-1]["allowed"] is False


def test_questions_overlay_select_type_cancel_and_render() -> None:
    responses: list[dict[str, object]] = []
    closed: list[bool] = []
    service = SimpleNamespace(respond=lambda **kwargs: responses.append(kwargs))
    questions = [
        {"id": "lang", "text": "Language?", "options": ["Python", {"label": "Rust"}]},
        {"id": "why", "text": "Why?", "options": ["Fast"]},
    ]
    overlay = QuestionsOverlay(
        _request(kind="questions", questions=questions), service, lambda: closed.append(True)
    )
    overlay.on_mount()
    assert overlay.render()
    overlay.handle_key(Key.ENTER, "")
    assert overlay._current == 1
    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")  # choose the "Other" free-text option
    assert overlay.render()
    overlay.handle_key(Key.CHAR, "b")
    overlay.handle_key(Key.CHAR, "e")
    overlay.handle_key(Key.CHAR, "t")
    overlay.handle_key(Key.ENTER, "")
    assert closed
    assert responses[-1]["allowed"] is True
    assert "bet" in str(responses[-1]["message"])

    empty = QuestionsOverlay(_request(kind="questions", questions=[]), service, lambda: None)
    assert empty.render()
    empty.handle_key(Key.ESC, "")
    assert responses[-1]["allowed"] is False


def test_help_overlay_detail_and_config_editor() -> None:
    registry = UnifiedCommandRegistry()
    registry.register_many(
        [
            Command("/one", "A command", aliases=("/1",)),
            Command("/two", "Another command", group="Plugins", source_id="plugin:test"),
        ]
    )
    closed: list[bool] = []
    help_overlay = HelpOverlay(registry, lambda: closed.append(True), initial_query="/one")
    assert help_overlay.render()
    help_overlay.handle_key(Key.ESC, "")
    assert help_overlay.render()
    help_overlay.handle_key(Key.UP, "")
    help_overlay.handle_key(Key.DOWN, "")
    help_overlay.handle_key(Key.ENTER, "")
    assert help_overlay.render()
    help_overlay.handle_key(Key.ESC, "")
    help_overlay.handle_key(Key.ESC, "")
    assert closed
    assert HelpOverlay(None, lambda: None).render()

    cfg = AgenthiccConfig()
    menu = ConfigMenuOverlay(cfg, lambda: closed.append(True))
    assert menu.render()
    menu.handle_key(Key.DOWN, "")
    menu.handle_key(Key.ENTER, "")
    menu.handle_key(Key.CHAR, "9")
    menu.handle_key(Key.ENTER, "")
    menu.handle_key(Key.CHAR, "s")
    menu.handle_key(Key.LEFT, "")
    menu.handle_key(Key.RIGHT, "")
    menu.handle_key(Key.ESC, "")
    assert menu.render()
    empty = ConfigMenuOverlay(None, lambda: None)
    assert empty.render()
    empty.handle_key(Key.CHAR, "s")


def test_registry_list_overlays_render_details_and_close() -> None:
    from rich.console import Console

    closed: list[bool] = []
    command_overlay = CommandListOverlay(
        [Command("/deploy", "Deploy the application", group="Plugins", source_id="plugin:deploy")],
        lambda: closed.append(True),
    )
    command_output = Console(file=StringIO(), record=True, force_terminal=False)
    command_output.print(command_overlay.render())
    command_text = command_output.export_text()
    assert "/deploy" in command_text
    assert "plugin:deploy" in command_text
    command_overlay.handle_key(Key.ENTER, "")
    command_output = Console(file=StringIO(), record=True, force_terminal=False)
    command_output.print(command_overlay.render())
    assert "Deploy the application" in command_output.export_text()
    command_overlay.handle_key(Key.ESC, "")
    command_overlay.handle_key(Key.ESC, "")

    skill = SkillDef(
        "Review",
        "review",
        Path("."),
        description="Review project changes",
        aliases=("inspect",),
        source="project",
        _body="body",
    )
    skill_overlay = SkillListOverlay([skill], lambda: closed.append(True))
    skill_output = Console(file=StringIO(), record=True, force_terminal=False)
    skill_output.print(skill_overlay.render())
    skill_text = skill_output.export_text()
    assert "$review" in skill_text
    assert "$inspect" in skill_text
    skill_overlay.handle_key(Key.ENTER, "")
    assert skill_overlay.render()
    message_overlay = RegistryMessageOverlay("Reload result", "Reloaded 1 skill(s).", lambda: None)
    message_output = Console(file=StringIO(), record=True, force_terminal=False)
    message_output.print(message_overlay.render())
    assert "Reloaded 1 skill(s)." in message_output.export_text()
    message_overlay.handle_key(Key.ESC, "")
    assert closed


def test_tool_and_workflow_registry_overlays_render_details() -> None:
    from rich.console import Console

    def demo_tool() -> None:
        """A function-shaped tool used to verify callable metadata rendering."""

    class DemoWorkflow(WorkflowPlugin):
        name = "demo_workflow"
        description = "A demo workflow"
        phases = [PhaseSpec(name="start")]

    def builtin_tool() -> None:
        """A builtin-shaped tool used to verify source rendering."""

    workflow_registry = WorkflowRegistry()
    workflow_registry.register(DemoWorkflow, source="project")
    tool_overlay = ToolListOverlay(
        [builtin_tool, demo_tool],
        lambda: None,
        {"builtin_tool": "builtin", "demo_tool": "project-plugin"},
    )
    workflow_overlay = WorkflowListOverlay([DemoWorkflow], workflow_registry, lambda: None)
    tool_output = Console(file=StringIO(), record=True, force_terminal=False)
    tool_output.print(tool_overlay.render())
    tool_text = tool_output.export_text()
    assert "demo_tool" in tool_text
    assert "builtin" in tool_text
    assert "plugin" in tool_text
    assert "registered" not in tool_text.lower()
    assert "function objects" not in tool_text
    assert workflow_overlay.render()
    workflow_overlay.handle_key(Key.ENTER, "")
    detail_output = Console(file=StringIO(), record=True, force_terminal=False)
    detail_output.print(workflow_overlay.render())
    assert "demo_workflow" in detail_output.export_text()


def test_registry_detail_enter_inserts_commands_and_closes_overlay() -> None:
    from agenthicc.skills.loader import SkillDef

    selected: list[str] = []
    closed: list[bool] = []
    command_overlay = CommandListOverlay(
        [Command("/deploy", "Deploy the application")],
        lambda: closed.append(True),
        selected.append,
    )
    command_overlay.handle_key(Key.ENTER, "")
    command_overlay.handle_key(Key.ENTER, "")

    skill = SkillDef("Review", "review", Path("."), _body="body")
    skill_overlay = SkillListOverlay([skill], lambda: closed.append(True), selected.append)
    skill_overlay.handle_key(Key.ENTER, "")
    skill_overlay.handle_key(Key.ENTER, "")

    workflow_registry = WorkflowRegistry()

    class DemoWorkflow(WorkflowPlugin):
        name = "demo_workflow"
        description = "A demo workflow"
        phases = [PhaseSpec(name="start")]

    workflow_registry.register(DemoWorkflow, source="project")
    workflow_overlay = WorkflowListOverlay(
        [DemoWorkflow], workflow_registry, lambda: closed.append(True), selected.append
    )
    workflow_overlay.handle_key(Key.ENTER, "")
    workflow_overlay.handle_key(Key.ENTER, "")

    assert selected == ["/deploy", "$review", "/workflow demo_workflow"]
    assert len(closed) == 3


def test_workflow_runs_overlay_is_paginated_sorted_and_resumes_selected_run() -> None:
    from rich.console import Console

    records = [
        SimpleNamespace(
            run_id=f"run-{index}",
            workflow_name="code_plan",
            current_phase="implement",
            status="paused",
            intent=f"Intent {index}",
            checkpoint=SimpleNamespace(created_at=1000.0 + index),
        )
        for index in range(17)
    ]
    resumed: list[str] = []
    closed: list[bool] = []
    overlay = WorkflowRunsOverlay(
        records,
        lambda: closed.append(True),
        lambda run_id: resumed.append(run_id) or True,
    )

    output = Console(file=StringIO(), record=True, force_terminal=False)
    output.print(overlay.render())
    output_text = output.export_text()
    assert "page 1/3" in output_text
    assert "run-16" in output_text  # newest checkpoint is first

    overlay.handle_key(Key.PAGE_DOWN, "")
    assert overlay._selected == 8
    overlay.handle_key(Key.PAGE_DOWN, "")
    assert overlay._selected == 16
    overlay.handle_key(Key.PAGE_UP, "")
    assert overlay._selected == 8
    overlay.handle_key(Key.HOME, "")
    assert overlay._selected == 0
    overlay.handle_key(Key.END, "")
    assert overlay._selected == 16
    overlay.handle_key(Key.HOME, "")
    overlay.handle_key(Key.ENTER, "")

    assert resumed == ["run-16"]
    assert closed == [True]
