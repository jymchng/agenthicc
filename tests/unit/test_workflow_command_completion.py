"""Clean-slate coverage for ``/workflow`` argument completion."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.commands import Command, build_builtin_registry
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerManager, TriggerResult
from agenthicc.tui.triggers.slash_command import SlashCommandTrigger
from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay
from agenthicc.workflows.plugin import WorkflowPlugin
from agenthicc.workflows.registry import WorkflowRegistry

pytestmark = pytest.mark.unit


class _CodeWorkflow(WorkflowPlugin):
    name = "code_review"
    description = "Review a code change"


class _DocsWorkflow(WorkflowPlugin):
    name = "docs"
    description = "Build documentation"


def _workflow_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    registry.register(_CodeWorkflow)
    registry.register(_DocsWorkflow)
    return registry


def _trigger() -> SlashCommandTrigger:
    return SlashCommandTrigger(build_builtin_registry(), _workflow_registry())


def test_workflow_argument_completion_lists_registered_names() -> None:
    matches = _trigger().get_matches("workflow ", TriggerContext(cwd=Path(".")))

    assert [match.value for match in matches] == [
        "/workflow code_review",
        "/workflow docs",
    ]
    assert matches[0].label == "/workflow code_review"
    assert matches[0].detail == "Review a code change"


def test_workflow_argument_completion_filters_by_name_prefix() -> None:
    matches = _trigger().get_matches("workflow co", TriggerContext(cwd=Path(".")))

    assert [match.value for match in matches] == ["/workflow code_review"]


def test_workflow_selection_produces_executable_command() -> None:
    trigger = _trigger()
    item = trigger.get_matches("workflow co", TriggerContext(cwd=Path(".")))[0]

    result = trigger.on_select(item, "workflow co", list("please "))

    assert result == TriggerResult(buffer=list("please /workflow code_review"))


def test_workflow_completion_tracks_registry_replacement() -> None:
    workflows = _workflow_registry()
    trigger = SlashCommandTrigger(build_builtin_registry(), workflows)
    replacement = WorkflowRegistry()
    replacement.register(_DocsWorkflow)
    workflows.replace_with(replacement)

    values = [
        match.value for match in trigger.get_matches("workflow ", TriggerContext(cwd=Path(".")))
    ]

    assert values == ["/workflow docs"]


def test_picker_space_enters_workflow_name_completion() -> None:
    manager = TriggerManager()
    trigger = _trigger()
    manager.register(trigger)
    completed: list[TriggerResult | None] = []
    overlay = TriggerPickerOverlay(
        initial_buf=list("/workflow"),
        registry=manager,
        cwd=Path("."),
        on_complete=completed.append,
    )

    overlay.handle_key(Key.CHAR, " ")

    assert completed == []
    assert overlay._trigger is not None
    assert overlay._trigger.fragment == "workflow "
    assert [match.value for match in overlay._matches] == [
        "/workflow code_review",
        "/workflow docs",
    ]

    overlay.handle_key(Key.CHAR, "c")
    assert [match.value for match in overlay._matches] == ["/workflow code_review"]

    overlay.handle_key(Key.TAB, "")
    assert completed == [TriggerResult(buffer=list("/workflow code_review"))]


def test_workflow_argument_completion_requires_a_live_registry() -> None:
    trigger = SlashCommandTrigger(build_builtin_registry())

    assert trigger.get_matches("workflow ", TriggerContext(cwd=Path("."))) == []


def test_command_completion_factory_uses_the_same_picker_continuation() -> None:
    registry = build_builtin_registry()
    registry.register(
        Command(
            "/env",
            "Select an environment",
            completions_factory=lambda prefix: [
                value for value in ("dev", "prod") if value.startswith(prefix)
            ],
        )
    )
    trigger = SlashCommandTrigger(registry)
    item = trigger.get_matches("env", TriggerContext(cwd=Path(".")))[0]

    assert trigger.has_argument_completions(item)
    matches = trigger.get_matches("env p", TriggerContext(cwd=Path(".")))
    assert [match.value for match in matches] == ["/env prod"]


def test_argument_match_items_are_not_treated_as_command_prefixes() -> None:
    trigger = _trigger()
    item = MatchItem(display="/workflow docs", value="/workflow docs")

    assert trigger.has_argument_completions(item) is False
