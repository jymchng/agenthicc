"""WFC-02 and WFC-04 spike for lauren-ai's model-facing schemas."""

from __future__ import annotations

from typing import TypedDict

import pytest
from lauren_ai._tools import tool

pytestmark = pytest.mark.unit


# Lauren reads TypedDict.__annotations__ directly. The functional declaration
# gives it the concrete nested types even with deferred module annotations.
_WorkflowTask = TypedDict(
    "_WorkflowTask",
    {
        "type": str,
        "task": str,
        "context": str | None,
        "workflow": str | None,
        "params": dict[str, str] | None,
    },
)


@tool()
def _enter_workflow(workflow: str, intent: str, params: dict[str, str] | None = None) -> str:
    """Enter a workflow with optional parameters."""
    return intent


@tool()
def _spawn_workflows(tasks: list[_WorkflowTask]) -> int:
    """Start one worker for each task."""
    return len(tasks)


def test_direct_workflow_params_schema_supports_optional_string_mapping() -> None:
    schema = vars(_enter_workflow)["__lauren_ai_tool__"].parameters["input_schema"]

    assert schema["required"] == ["workflow", "intent"]
    assert schema["properties"]["params"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "default": None,
    }


def test_nested_workflow_params_schema_requires_optional_keys_in_raw_schema() -> None:
    schema = vars(_spawn_workflows)["__lauren_ai_tool__"].parameters["input_schema"]
    item_schema = schema["properties"]["tasks"]["items"]

    # The spawn tool already normalizes nested required keys before registration.
    assert item_schema["required"] == ["type", "task", "context", "workflow", "params"]
    assert item_schema["properties"]["params"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
