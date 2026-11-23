"""Validate workflow-worker admission before allocating or calling a worker."""

from __future__ import annotations

import pytest
from lauren_ai._tools import TOOL_META

from agenthicc.config import AgenthiccConfig
from agenthicc.subagents.tool import SubagentToolSelection, make_spawn_subagents_tool
from agenthicc.subagents.workflow_task import (
    WorkflowTaskRefusal,
    WorkflowTaskSelection,
    workflow_task_admission,
)
from agenthicc.tools.surface import ToolSurface
from agenthicc.workflows.registry import build_workflow_registry

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "params", "blocked", "can_write", "code"),
    [
        ("missing", None, frozenset(), True, "unknown_workflow"),
        ("make_book", None, frozenset(), True, "workflow_not_eligible"),
        ("code_plan", None, frozenset(), True, "workflow_requires_human_gate"),
        ("create_tools", None, frozenset({"write"}), True, "ceiling_denied"),
        ("create_tools", None, frozenset(), False, "ceiling_denied"),
        ("create_tools", {"model": 7}, frozenset(), True, "invalid_params"),
    ],
)
def test_each_ineligible_workflow_has_a_deterministic_refusal(
    name: str,
    params: object,
    blocked: frozenset[str],
    can_write: bool,
    code: str,
) -> None:
    registry = build_workflow_registry(load_external=False)
    result = workflow_task_admission(
        name, params, registry, AgenthiccConfig(), blocked, can_write=can_write
    )
    assert isinstance(result, WorkflowTaskRefusal)
    assert result.code == code


def test_workflow_task_pins_an_eligible_definition_and_bound_parameters() -> None:
    registry = build_workflow_registry(load_external=False)
    result = workflow_task_admission(
        "create_tools",
        {"validate_model": "reviewer"},
        registry,
        AgenthiccConfig(),
        frozenset(),
        can_write=True,
    )
    assert isinstance(result, WorkflowTaskSelection)
    assert result.definition is registry.get("create_tools")
    assert result.params.model_for_phase("validate", "") == "reviewer"
    assert result.requires_worktree


def test_workflow_task_schema_is_optional_and_uses_a_string_mapping() -> None:
    spawn = make_spawn_subagents_tool(None, "offline", SubagentToolSelection(ToolSurface(0, ())))
    schema = vars(spawn)[TOOL_META].parameters["input_schema"]["properties"]["tasks"]["items"]
    assert set(schema["required"]) == {"type", "task"}
    assert schema["properties"]["params"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
