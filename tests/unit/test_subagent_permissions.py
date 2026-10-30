from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from lauren_ai import ToolCallContext

from agenthicc.agents.definitions import (
    AgentSpec,
    DefinitionError,
    parse_definition,
    render_definition,
)
from agenthicc.agents.registry import AgentsRegistry
from agenthicc.plugins.registry import ToolRegistry
from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.security import AgentCapabilityScope
from agenthicc.subagents.policy import SubagentExecutionPolicy, child_app_state
from agenthicc.subagents.pool import SubagentTask, SubagentWorker
from agenthicc.subagents.types import SubagentPermissionConstraints, SubagentTypeSpec
from agenthicc.tools.capabilities import CAPABILITIES_KEY, ToolCapability
from agenthicc.tools.capability_gate import ToolCapabilityGate
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspacePathStatus,
    WorkspaceScope,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase

    from agenthicc.kernel.processor import EventProcessor

pytestmark = pytest.mark.unit


def _set_mode(app_state: AppState, name: str) -> None:
    mode = build_default_registry().get(name)
    assert mode is not None
    app_state.active_mode.set(mode)


def test_definition_permissions_parse_and_render_round_trip() -> None:
    source = """---
name: reviewer
description: Reviews changes.
mode: subagent
permissions:
  deny_tools: [outlook_*]
  deny_capabilities: [write]
  require_approval: [execute]
  workspace_paths: [docs, docs/reference]
---
Review carefully.
"""

    spec = parse_definition(source)
    rendered = render_definition(spec)

    assert spec.permissions == SubagentPermissionConstraints(
        deny_tools=frozenset({"outlook_*"}),
        deny_capabilities=frozenset({"write"}),
        require_approval=frozenset({"execute"}),
        workspace_paths=("docs", "docs/reference"),
    )
    assert parse_definition(rendered).permissions == spec.permissions


def test_definition_permissions_reject_unknown_keys_and_capabilities() -> None:
    prefix = "---\nname: reviewer\ndescription: Reviews changes.\npermissions:\n"

    with pytest.raises(DefinitionError, match="unknown permissions key"):
        parse_definition(prefix + "  grant_tools: [run_bash]\n---\n")
    with pytest.raises(DefinitionError, match="is not a capability"):
        parse_definition(prefix + "  deny_capabilities: [unknown]\n---\n")


def test_permissions_are_rejected_for_primary_only_definitions() -> None:
    with pytest.raises(DefinitionError, match="only to subagent definitions"):
        parse_definition(
            "---\nname: reviewer\ndescription: Reviews changes.\nmode: primary\n"
            "permissions:\n  deny_tools: [outlook_*]\n---\n"
        )


def test_tool_scope_denials_apply_wildcards_after_allow_list() -> None:
    inherited = AgentCapabilityScope(allowed_tools=frozenset({"outlook_send", "read_file"}))
    narrowed = inherited.restrict(AgentCapabilityScope(denied_tools=frozenset({"outlook_*"})))

    assert not narrowed.is_tool_allowed("outlook_send")
    assert narrowed.is_tool_allowed("read_file")
    assert narrowed.denied_tools == frozenset({"outlook_*"})


def test_worker_excludes_later_tools_matching_type_deny(tmp_path: Path) -> None:
    late_registered_tool = SimpleNamespace(name="outlook_send_new")
    read_tool = SimpleNamespace(name="read_file")
    spec = SubagentTypeSpec(
        name="reviewer",
        allowed_tools=frozenset({"outlook_*", "read_file"}),
        max_turns=1,
        system_prompt="Review.",
        permissions=SubagentPermissionConstraints(deny_tools=frozenset({"outlook_*"})),
    )
    worker = SubagentWorker(
        task=SubagentTask("task-1", "reviewer", "Review the workspace."),
        spec=spec,
        index=1,
        parent_runner=cast("AgentRunnerBase", object()),
        parent_model="test-model",
        all_tools=[late_registered_tool, read_tool],
    )

    assert worker._filtered_tools() == [read_tool]


def _tool_context() -> ToolCallContext:
    return ToolCallContext(
        agent_context=None,
        tool_use_id="subagent-permission-test",
        turn=1,
        tool_name="write_file",
        tool_input={},
        metadata={CAPABILITIES_KEY: frozenset({ToolCapability.WRITE})},
    )


@pytest.mark.asyncio
async def test_type_capability_denial_reaches_runtime_gate_without_widening_advice() -> None:
    app_state = AppState.create()
    _set_mode(app_state, "Yolo")
    permissions = SubagentPermissionConstraints(deny_capabilities=frozenset({"write"}))
    policy = SubagentExecutionPolicy.from_app_state(
        app_state,
        type_permissions=permissions,
        type_name="reviewer",
    )
    assert policy is not None
    child_state = child_app_state(policy)
    assert child_state is not None

    decision = await ToolCapabilityGate(
        child_state,
        type_name=policy.type_name,
        type_denied_capabilities=policy.type_denied_capabilities,
    ).before_tool_call(_tool_context())

    assert bool(getattr(decision, "_aborted", False))
    message = str(getattr(decision, "value", decision))
    assert "reviewer" in message
    assert "permissions.deny_capabilities" in message
    assert "cannot be widened" in message
    assert "Switch to Yolo" not in message


def test_approval_constraint_is_local_to_its_type() -> None:
    app_state = AppState.create()
    _set_mode(app_state, "Yolo")
    restricted = SubagentExecutionPolicy.from_app_state(
        app_state,
        type_permissions=SubagentPermissionConstraints(require_approval=frozenset({"execute"})),
        type_name="executor",
    )
    sibling = SubagentExecutionPolicy.from_app_state(app_state, type_name="reviewer")

    assert restricted is not None
    assert sibling is not None
    assert "execute" in restricted.runtime_mode().approval_required
    assert "execute" not in sibling.runtime_mode().approval_required


def test_worker_fails_closed_when_type_approval_has_no_service() -> None:
    app_state = AppState.create()
    _set_mode(app_state, "Yolo")
    spec = SubagentTypeSpec(
        name="executor",
        allowed_tools=frozenset({"run_bash"}),
        max_turns=1,
        system_prompt="Execute.",
        permissions=SubagentPermissionConstraints(require_approval=frozenset({"execute"})),
    )

    with pytest.raises(
        ValueError, match="permissions.require_approval requires an approval service"
    ):
        SubagentWorker(
            task=SubagentTask("task-1", "executor", "Run tests."),
            spec=spec,
            index=1,
            parent_runner=cast("AgentRunnerBase", object()),
            parent_model="test-model",
            all_tools=[],
            app_state=app_state,
        )


def test_workspace_scope_restriction_is_a_subset_and_empty_scope_denies_all(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    parent = WorkspaceScope.create(tmp_path)

    child = parent.restrict(("docs",))
    assert child.allowed_roots == (tmp_path / "docs",)
    assert child.resolve("README.md").status is WorkspacePathStatus.IN_SCOPE
    assert child.resolve("../README.md").status is WorkspacePathStatus.OUTSIDE_SCOPE

    empty = parent.restrict(())
    assert empty.allowed_roots == ()
    assert empty.resolve("README.md").status is WorkspacePathStatus.OUTSIDE_SCOPE
    with pytest.raises(ValueError, match="outside the inherited workspace scope"):
        parent.restrict(("../outside",))


def _worker(tmp_path: Path, workspace_paths: tuple[str, ...]) -> SubagentWorker:
    root = WorkspaceScope.create(tmp_path)
    workspace_access = WorkspaceAccessPolicy(root)
    spec = SubagentTypeSpec(
        name="reviewer",
        allowed_tools=frozenset(),
        max_turns=1,
        system_prompt="Review.",
        permissions=SubagentPermissionConstraints(workspace_paths=workspace_paths),
    )
    return SubagentWorker(
        task=SubagentTask("task-1", "reviewer", "Review the workspace."),
        spec=spec,
        index=1,
        parent_runner=cast("AgentRunnerBase", object()),
        parent_model="test-model",
        all_tools=[],
        workspace_access=workspace_access,
    )


def test_worker_revalidates_workspace_paths_against_inherited_scope(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()

    worker = _worker(tmp_path, ("docs",))

    assert worker._workspace_scope is not None
    assert worker._workspace_scope.allowed_roots == (tmp_path / "docs",)
    with pytest.raises(ValueError, match="outside the inherited workspace scope"):
        _worker(tmp_path, ("../outside",))


def test_definition_registry_rejects_out_of_scope_workspace_paths(tmp_path: Path) -> None:
    definition_path = tmp_path / "workspace-checker.md"
    definition = parse_definition(
        """---
name: workspace-checker
description: Reviews changes.
mode: subagent
permissions:
  workspace_paths: [../outside]
---
Review carefully.
""",
        path=definition_path,
        source="project",
    )
    definitions = AgentsRegistry()
    definitions.register(definition)
    context = AgentTurnContext(
        text="",
        runner=cast("AgentRunnerBase", object()),
        processor=cast("EventProcessor", object()),
        app_state=AppState.create(),
        agents_registry=definitions,
        workspace_access=WorkspaceAccessPolicy(WorkspaceScope.create(tmp_path)),
    )
    runner = AgentTurnRunner(context)

    registry = runner._build_definition_subagent_registry(ToolRegistry(), [])

    assert registry.get("workspace-checker") is None
    error = runner._definition_agent_errors["workspace-checker"]
    assert str(definition_path) in error
    assert "permissions.workspace_paths" in error


def test_permission_changes_change_revision_but_absence_is_stable() -> None:
    app_state = AppState.create()
    baseline = SubagentExecutionPolicy.from_app_state(app_state)
    unchanged = SubagentExecutionPolicy.from_app_state(app_state, type_permissions=None)
    narrowed = SubagentExecutionPolicy.from_app_state(
        app_state,
        type_permissions=SubagentPermissionConstraints(deny_tools=frozenset({"run_bash"})),
    )

    assert baseline is not None
    assert unchanged is not None
    assert narrowed is not None
    from agenthicc.subagents.tool import _tasks_fingerprint, _type_policy_revision

    task = [SubagentTask("task-1", "reviewer", "Review the change.")]
    baseline_spec = SubagentTypeSpec(
        "reviewer", frozenset({"read_file"}), 1, "Review.", policy=baseline
    )
    narrowed_spec = SubagentTypeSpec(
        "reviewer",
        frozenset({"read_file"}),
        1,
        "Review.",
        policy=narrowed,
        permissions=SubagentPermissionConstraints(deny_tools=frozenset({"run_bash"})),
    )
    baseline_fingerprint = _tasks_fingerprint(
        task,
        policy_revision=baseline.policy_revision,
        type_policy_revisions={"reviewer": _type_policy_revision(baseline_spec)},
    )
    narrowed_fingerprint = _tasks_fingerprint(
        task,
        policy_revision=baseline.policy_revision,
        type_policy_revisions={"reviewer": _type_policy_revision(narrowed_spec)},
    )

    assert baseline.policy_revision == unchanged.policy_revision
    assert baseline.policy_revision != narrowed.policy_revision
    assert baseline_fingerprint != narrowed_fingerprint


def test_policy_diagnostics_expose_type_workspace_constraints() -> None:
    permissions = SubagentPermissionConstraints(workspace_paths=("docs",))
    policy = SubagentExecutionPolicy.from_app_state(
        AppState.create(),
        type_permissions=permissions,
        type_name="documenter",
    )

    assert policy is not None
    diagnostic = policy.to_dict()
    assert diagnostic["type_name"] == "documenter"
    assert diagnostic["type_workspace_paths"] == ["docs"]
    assert diagnostic["type_permissions_fingerprint"]


def test_agent_spec_with_no_permission_block_remains_optional() -> None:
    spec = AgentSpec(name="reviewer", description="Reviews changes")

    assert spec.permissions is None
    assert parse_definition(render_definition(spec)).permissions is None
