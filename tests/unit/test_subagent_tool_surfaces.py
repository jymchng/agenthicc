"""SUBAGENT-18: shared tool inheritance and a capability-aware type catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.subagents.tool import SubagentToolSelection
from agenthicc.tools.surface import ToolSurface
from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.config import SubagentDepthTier, SubagentSettings
from agenthicc.plugins.registry import build_registry
from agenthicc.subagents.pool import _is_mutating_tool, tool_tags
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import DEFAULT_REGISTRY, _BUILTIN_REGISTRY
from agenthicc.tools.base import tool_name
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


@pytest.mark.parametrize("agent_type", _BUILTIN_REGISTRY.names())
def test_builtin_types_declare_no_default_tool_ceiling(agent_type: str) -> None:
    spec = DEFAULT_REGISTRY.get(agent_type)
    assert spec is not None
    assert spec.allowed_tools is None
    assert spec.capabilities is None


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
        SubagentToolSelection(ToolSurface(0, tuple(list(AGENT_TOOLS)))),
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

    for name in _BUILTIN_REGISTRY.names():
        assert f"{name} [edits files, runs commands]:" in catalog[name]


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


async def test_a_report_only_role_still_runs_in_plan_mode(tmp_path: Path) -> None:
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


async def test_a6_depth_ceiling_is_used_for_catalog_admission_and_dispatch(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("Reviewed the file."))
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        SubagentToolSelection(ToolSurface(0, tuple(AGENT_TOOLS)), build_registry()),
        parent_depth=1,
        subagent_settings=SubagentSettings(
            max_depth=2,
            depth_tiers={
                2: {
                    role: SubagentDepthTier(allowed_tools=("read_file",))
                    for role in ("researcher", "implementer")
                }
            },
        ),
    )
    assert "researcher [read-only]" in _catalog(spawn)["researcher"]
    assert "implementer [unavailable in this session" in _catalog(spawn)["implementer"]

    refusal = await spawn(tasks=[{"type": "implementer", "task": "Write the file."}])
    assert refusal["unavailable_types"] == ["implementer"]
    assert not transport.calls
    result = await spawn(tasks=[{"type": "researcher", "task": "Read the file."}])
    assert result["ok"] is True
    builtin_names = {tool_name(tool) for tool in AGENT_TOOLS}
    worker_schemas = {item["name"] for item in transport.calls[0].tools}
    assert worker_schemas & builtin_names == {"read_file"}


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], "no tools"),
        (["read_file"], "read-only"),
        (["git_add"], "changes git"),
        (["send_email"], "writes to external services"),
    ],
)
def test_a6_catalog_reports_narrow_workspace_or_remote_capabilities(
    names: list[str], expected: str
) -> None:
    assert tool_tags([_TOOLS[name] for name in names]) == (expected,)
