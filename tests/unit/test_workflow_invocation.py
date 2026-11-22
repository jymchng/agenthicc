"""Invocation declarations and their production tool schema."""

from __future__ import annotations

import hashlib
import json

import pytest

from agenthicc.workflows.checkpoint import workflow_fingerprint
from agenthicc.workflows.plugin import PhaseSpec, WorkflowInvocation, WorkflowPlugin
from agenthicc.workflows.registry import build_workflow_registry
from agenthicc.workflows.stack_tools import enter_workflow

pytestmark = pytest.mark.unit


def test_unmarked_workflows_keep_their_topology_fingerprint() -> None:
    class Legacy(WorkflowPlugin):
        name = "legacy"
        phases = [PhaseSpec("inspect")]

    prior = json.dumps(
        {"name": "legacy", "phases": [{"name": "inspect", "next": None, "on_reject": None}]},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert Legacy.invocation == WorkflowInvocation()
    assert workflow_fingerprint(Legacy) == hashlib.sha256(prior.encode()).hexdigest()

    class CallableLegacy(Legacy):
        invocation = WorkflowInvocation(agent_callable=True)

    assert workflow_fingerprint(CallableLegacy) != workflow_fingerprint(Legacy)


def test_builtin_invocation_capabilities_are_declared_individually() -> None:
    registry = build_workflow_registry(load_external=False)
    available = {workflow.name: workflow.invocation for workflow in registry.all()}
    assert available["create_workflow"].agent_callable
    assert available["create_workflow"].subagent_eligible
    assert available["create_workflow"].publishes == ("workflow",)
    assert available["create_tools"].agent_callable
    assert available["create_tools"].subagent_eligible
    assert available["create_tools"].publishes == ("tool",)
    assert available["code_plan"].agent_callable and available["code_plan"].interactive
    assert not available["code_plan"].subagent_eligible
    assert all(
        invocation == WorkflowInvocation()
        for name, invocation in available.items()
        if name not in {"create_workflow", "create_tools", "code_plan"}
    )


def test_enter_workflow_exposes_typed_optional_parameters() -> None:
    schema = vars(enter_workflow)["__lauren_ai_tool__"].parameters["input_schema"]
    assert schema["required"] == ["workflow", "intent"]
    assert schema["properties"]["params"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "default": None,
    }
