"""Pure foreground workflow stack transitions and bounds."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenthicc.config import load_config
from agenthicc.runners.workflow_execution_context import bind_workflow_execution
from agenthicc.runners.workflow_stack import (
    WorkflowCall,
    WorkflowDelivery,
    WorkflowReturn,
    WorkflowStack,
    WorkflowStackError,
    limit_workflow_mode,
)
from agenthicc.subagents.policy import SubagentExecutionPolicy
from agenthicc.tools.capabilities import ToolCapability
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, WorkspaceScope
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import ModeManager, phase_mode
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

pytestmark = pytest.mark.unit


class Inspect(WorkflowPlugin):
    name = "inspect"
    phases = [PhaseSpec("work", allowed_capabilities=frozenset({ToolCapability.READ}))]


class Edit(WorkflowPlugin):
    name = "edit"
    phases = [PhaseSpec("edit", allowed_capabilities=frozenset({ToolCapability.WRITE}))]


def _stack(mode: str = "Safe", *, max_depth: int = 4) -> WorkflowStack:
    app = AppState.create()
    ModeManager(app_state=app, default_name=mode)
    policy = SubagentExecutionPolicy.from_app_state(app)
    assert policy is not None
    return WorkflowStack(policy, max_depth=max_depth)


def test_stack_suspends_pauses_resumes_and_returns_to_base() -> None:
    stack = _stack()
    parent = stack.base
    child = stack.push(WorkflowCall(Inspect, "Review inputs.", "user"))
    assert stack.depth == 1
    assert stack.base.status == "suspended"
    assert stack.ancestry(child.frame_id) == (stack.base,)
    stack.pause(child.frame_id)
    assert stack.top.status == "paused"
    assert stack.base.status == "suspended"
    result = WorkflowReturn(child.frame_id, child.workflow_name, "completed", "Finished")
    with pytest.raises(WorkflowStackError) as caught:
        stack.complete(child.frame_id, result)
    assert caught.value.code == "frame_not_running"
    stack.resume(child.frame_id)
    assert stack.complete(child.frame_id, result) == parent
    assert stack.depth == 0
    assert stack.top == parent
    delivery = WorkflowDelivery(parent.frame_id, child.run_id, result)
    assert stack.queue_return(delivery)
    assert stack.take_return() == delivery
    assert not stack.queue_return(delivery)
    assert stack.take_return() is None
    with pytest.raises(WorkflowStackError) as duplicate:
        stack.complete(child.frame_id, result)
    assert duplicate.value.code == "frame_not_current"


def test_stack_rejects_capability_depth_and_reentrancy_without_changing_frames() -> None:
    stack = _stack("Plan", max_depth=4)
    with pytest.raises(WorkflowStackError) as blocked:
        stack.push(WorkflowCall(Edit, "Try edits.", "user"))
    assert blocked.value.code == "capability_denied"
    assert stack.depth == 0

    stack.push(WorkflowCall(Inspect, "First.", "user"))
    with pytest.raises(WorkflowStackError) as unapproved:
        stack.push(WorkflowCall(Inspect, "Again.", "user"))
    assert unapproved.value.code == "workflow_reentrant"
    assert stack.depth == 1

    for _ in range(3):
        stack.push(WorkflowCall(Inspect, "Revisit.", "user", reentrant=True))
    top = stack.top
    with pytest.raises(WorkflowStackError) as limit:
        stack.push(WorkflowCall(Inspect, "Beyond the limit.", "user", reentrant=True))
    assert limit.value.code == "stack_depth_exceeded"
    assert stack.top == top
    assert stack.depth == 4


def test_stack_abandon_resumes_base_and_selection_stays_locked_until_pop() -> None:
    stack = _stack()
    stack.set_base("base-demo")
    child = stack.push(WorkflowCall(Inspect, "Review inputs.", "user"))
    with pytest.raises(WorkflowStackError) as select:
        stack.set_base("another")
    assert select.value.code == "child_active"
    with pytest.raises(WorkflowStackError) as live:
        stack.abandon(child.frame_id)
    assert live.value.code == "frame_not_paused"
    stack.pause(child.frame_id)
    result = stack.abandon(child.frame_id)
    assert result.status == "abandoned"
    assert result.frame_id == child.frame_id
    assert stack.top.workflow_name == "base-demo"
    stack.clear_base()
    assert stack.base.workflow_name == "default"


def test_workflow_return_bounds_and_freezes_all_output() -> None:
    original = {f"phase-{index}": "x" * 2_000 for index in range(30)}
    result = WorkflowReturn("frame", "inspection", "completed", "s" * 4_000, original)
    original["phase-0"] = "edited"
    assert len(result.summary) == 2_000
    assert result.summary.endswith("[truncated]")
    assert len(result.outputs) == 16
    assert len(result.outputs["phase-0"]) == 1_000
    assert result.outputs["phase-0"].endswith("[truncated]")
    assert result.as_system_block().startswith("[WORKFLOW RETURN]\n")


def test_mode_selection_preserves_the_suspended_parent_and_caps() -> None:
    stack = _stack("Safe")
    stack.refresh_mode("original", stack.base.policy)
    child = stack.push(WorkflowCall(Inspect, "Review.", "user"))
    yolo = _stack("Yolo").base.policy
    stack.refresh_mode("replacement", yolo)

    assert stack.top.policy.mode_name == "Safe"
    assert stack.base_dispatch_name == "original"
    stack.complete(child.frame_id, WorkflowReturn(child.frame_id, "inspect", "completed", "Done"))
    assert stack.top.workflow_name == "original"
    stack.refresh_mode("replacement", yolo)
    assert stack.top.workflow_name == "replacement"
    stack.set_base("operator-choice")
    stack.refresh_mode(None, yolo)
    assert stack.base_dispatch_name == "operator-choice"


def test_phase_mode_keeps_parent_workspace_and_approval_restrictions() -> None:
    yolo = _stack("Yolo").base.policy.runtime_mode()
    safe = _stack("Safe").base.policy
    plan = _stack("Plan").base.policy
    safe_phase = limit_workflow_mode(yolo, safe)
    assert safe_phase.name == "Safe"
    assert "execute" in safe_phase.approval_required
    plan_phase = limit_workflow_mode(yolo, plan)
    assert plan_phase.name == "Plan"
    assert "write" in plan_phase.blocked_capabilities
    assert "execute" in plan_phase.blocked_capabilities


def test_child_scope_applies_after_a_session_mode_change(tmp_path: Path) -> None:
    app = AppState.create()
    manager = ModeManager(app_state=app, default_name="Yolo")
    access = WorkspaceAccessPolicy(WorkspaceScope.create(tmp_path), mode_provider=app.active_mode)
    with bind_workflow_execution("child-run", policy=_stack("Plan").base.policy):
        assert phase_mode(app.active_mode(), manager, "Yolo").name == "Plan"
        assert access.mode_name == "Plan"
    assert access.mode_name == "Yolo"


def test_workflow_stack_limit_is_configured_alongside_workflow_params(tmp_path: Path) -> None:
    path = tmp_path / "agenthicc.toml"
    path.write_text('[workflows]\nmax_stack_depth = 2\n[workflows.inspect]\nmodel = "offline"\n')
    cfg = load_config(config_path=path, env_overrides=False)
    assert cfg.workflow_stack_max_depth == 2
    assert cfg.workflows == {"inspect": {"model": "offline"}}

    path.write_text("[workflows]\nmax_stack_depth = true\n")
    with pytest.raises(ValueError, match="workflows.max_stack_depth"):
        load_config(config_path=path, env_overrides=False)
