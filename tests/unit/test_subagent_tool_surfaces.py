"""SUBAGENT-13: tool surfaces that match the role and a capability-aware type catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.subagents.pool import _is_mutating_tool
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import DEFAULT_REGISTRY
from agenthicc.tools.base import tool_name
from agenthicc.tools.capabilities import get_tool_capabilities
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit

_TOOLS = {tool_name(tool): tool for tool in AGENT_TOOLS}
_INVESTIGATIVE = ("explorer", "planner", "reviewer", "verifier", "researcher")
_WORKING = ("implementer", "executor", "tester", "documenter")

#: SUBAGENT-13 §5.1: what each investigative role gained.
_ADDITIONS = {
    "explorer": {"batch_read", "git_diff", "git_status"},
    "planner": {"batch_read", "get_file_info", "git_log", "git_status"},
    "reviewer": {"batch_read", "get_file_info", "git_show", "git_blame", "git_status"},
    "verifier": {"batch_read", "get_file_info", "git_show", "git_status"},
    "researcher": {"batch_read", "get_file_info"},
}

#: The only execute-capability tools a read-only role may hold (they run checks).
_CHECK_TOOLS = {"run_python_expr", "run_tests"}


def _allowed(agent_type: str) -> frozenset[str]:
    spec = DEFAULT_REGISTRY.get(agent_type)
    assert spec is not None and spec.allowed_tools is not None
    return spec.allowed_tools


@pytest.mark.parametrize("agent_type", _INVESTIGATIVE)
def test_an_investigative_list_names_only_registered_tools(agent_type: str) -> None:
    assert _allowed(agent_type) <= set(_TOOLS)


@pytest.mark.parametrize("agent_type", _INVESTIGATIVE)
def test_an_investigative_role_holds_no_write_tool(agent_type: str) -> None:
    for name in _allowed(agent_type):
        capabilities = {capability.value for capability in get_tool_capabilities(_TOOLS[name])}
        assert not capabilities & {"write", "git_write", "network"}, name
        assert not _is_mutating_tool(_TOOLS[name]), name
        if "execute" in capabilities:
            assert name in _CHECK_TOOLS, name


@pytest.mark.parametrize(("agent_type", "added"), sorted(_ADDITIONS.items()))
def test_investigative_roles_gained_the_read_tools_their_prompts_need(
    agent_type: str, added: set[str]
) -> None:
    assert added <= _allowed(agent_type)


@pytest.mark.parametrize("agent_type", _WORKING)
def test_working_roles_declare_no_list(agent_type: str) -> None:
    spec = DEFAULT_REGISTRY.get(agent_type)
    assert spec is not None
    assert spec.allowed_tools is None


def test_a_network_write_tool_is_not_a_workspace_mutation() -> None:
    for name in ("send_email", "reply_email", "move_email", "create_event"):
        assert not _is_mutating_tool(_TOOLS[name]), name
    for name in ("write_file", "patch_file", "apply_diff", "move_file", "delete_file"):
        assert _is_mutating_tool(_TOOLS[name]), name


# ── the catalog and admission ────────────────────────────────────────────────


def _final(text: str) -> Completion:
    return Completion(
        id="final",
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def _spawn_tool(
    mode: str, transport: MockTransport, root: Path
) -> tuple[object, WorkspaceAccessPolicy]:
    state = AppState.create()
    state.active_mode.set(build_default_registry().get(mode))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(root), mode_provider=state.active_mode)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        list(AGENT_TOOLS),
        app_state=state,
        workspace_access=policy,
    )
    return spawn, policy


def _catalog(spawn: object) -> dict[str, str]:
    description = str(spawn.__lauren_ai_tool__.parameters["description"])  # type: ignore[attr-defined]
    lines = description.split("Available subagent types:\n", 1)[1].splitlines()
    return {line[2:].split(" ", 1)[0]: line for line in lines if line.startswith("- ")}


def test_the_catalog_tells_the_parent_what_each_type_can_do(tmp_path: Path) -> None:
    spawn, _policy = _spawn_tool("Yolo", MockTransport(), tmp_path)

    catalog = _catalog(spawn)

    for name in ("explorer", "planner", "researcher"):
        assert f"{name} [read-only]:" in catalog[name]
    # reviewer and verifier run checks (run_python_expr / run_tests) but cannot edit files
    for name in ("reviewer", "verifier"):
        assert f"{name} [runs commands]:" in catalog[name]
        assert "edits files" not in catalog[name]
    assert "[edits files, runs commands]" in catalog["implementer"]
    assert "[edits files, runs commands]" in catalog["executor"]


def test_the_catalog_marks_a_type_unavailable_when_the_policy_blocks_it(tmp_path: Path) -> None:
    spawn, _policy = _spawn_tool("Plan", MockTransport(), tmp_path)

    catalog = _catalog(spawn)

    assert "[unavailable in this session:" in catalog["implementer"]
    assert "the Plan policy blocks it" in catalog["implementer"]
    assert "explorer [read-only]:" in catalog["explorer"]
    # Plan blocks `execute`, so the checks a reviewer runs are not offered either
    assert "reviewer [read-only]:" in catalog["reviewer"]


async def test_a_type_that_cannot_work_is_refused_before_any_provider_call(
    tmp_path: Path,
) -> None:
    transport = MockTransport()
    spawn, policy = _spawn_tool("Plan", transport, tmp_path)

    token = set_current_workspace_access(policy)
    try:
        result = await spawn(  # type: ignore[operator]
            tasks=[
                {"type": "reviewer", "task": "Review it."},
                {"type": "implementer", "task": "Change it."},
            ],
            timeout_s=30,
        )
    finally:
        reset_current_workspace_access(token)

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert result["unavailable_types"] == ["implementer"]
    assert "implementer needs a file-changing tool" in str(result["error"])
    assert transport.calls == []  # no worker, no pool, no provider call
    assert "pool_id" not in result


async def test_a_read_only_type_still_runs_in_plan_mode(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("No issues found."))
    spawn, policy = _spawn_tool("Plan", transport, tmp_path)

    token = set_current_workspace_access(policy)
    try:
        result = await spawn(  # type: ignore[operator]
            tasks=[{"type": "reviewer", "task": "Review it."}], timeout_s=30
        )
    finally:
        reset_current_workspace_access(token)

    assert result["ok"] is True
    assert "No issues found." in str(result["results"])
