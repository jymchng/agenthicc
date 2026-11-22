"""SUBAGENT-12: a worker's success is decided from evidence the runtime observed."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.subagents.types import (
    DEFAULT_REGISTRY,
    SubagentTypeRegistry,
    SubagentTypeSpec,
)
from agenthicc.tools.approval import ApprovalRequest, ApprovalResponse
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def _final(text: str, identifier: str = "final", stop_reason: str = "end_turn") -> Completion:
    return Completion(
        id=identifier,
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=TokenUsage(input_tokens=3, output_tokens=5),
    )


class _Broker:
    """An approval broker that answers every request the same way."""

    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.asked: list[str] = []

    async def request_approval(self, req: ApprovalRequest) -> ApprovalResponse:
        self.asked.append(req.tool_name)
        return ApprovalResponse(allowed=self.allow, outcome="answered")

    def reset_turn_memory(self) -> None:
        return None


async def _spawn(
    agent_type: str,
    transport: MockTransport,
    root: Path,
    *,
    mode: str = "Yolo",
    broker: _Broker | None = None,
    registry: SubagentTypeRegistry | None = None,
    task: str = "Write out.txt containing hello.",
) -> dict[str, object]:
    state = AppState.create()
    state.active_mode.set(build_default_registry().get(mode))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(root), mode_provider=state.active_mode)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        list(AGENT_TOOLS),
        app_state=state,
        workspace_access=policy,
        approval_svc=broker,
        registry=registry,
    )
    token = set_current_workspace_access(policy)
    try:
        return await spawn(tasks=[{"type": agent_type, "task": task}], timeout_s=30)  # type: ignore[no-any-return]
    finally:
        reset_current_workspace_access(token)


def _write_then(text: str) -> Callable[[MockTransport], None]:
    def script(transport: MockTransport) -> None:
        transport.queue_tool_use(
            "write_file", {"path": "out.txt", "content": "hello"}, tool_use_id="w1"
        )
        transport.queue_response(_final(text, "after-write"))

    return script


def _results(result: dict[str, object]) -> str:
    return str(result["results"])


# ── criterion 1: a mutating role that only narrates fails ────────────────────


@pytest.mark.parametrize(
    ("agent_type", "stop_reason"),
    [
        ("implementer", "missing_mutation"),
        ("tester", "missing_action"),
        ("executor", "missing_action"),
    ],
)
async def test_prose_only_mutating_role_fails_with_a_named_reason(
    agent_type: str, stop_reason: str, tmp_path: Path
) -> None:
    transport = MockTransport()
    transport.queue_response(_final("Done. Everything looks good.", "claim"))

    result = await _spawn(agent_type, transport, tmp_path)

    assert result["ok"] is False and result["failed"] == 1
    assert f"stop reason: {stop_reason}" in _results(result)
    assert len(transport.calls) == 1  # no hidden second chance


# ── criterion 3: refused calls fail with the real cause and no nudge ─────────


@pytest.mark.parametrize("agent_type", ["implementer", "tester", "executor", "documenter"])
async def test_a_denied_write_fails_with_the_runtime_cause(agent_type: str, tmp_path: Path) -> None:
    transport = MockTransport()
    _write_then("I wrote out.txt with hello.")(transport)
    broker = _Broker(allow=False)

    result = await _spawn(agent_type, transport, tmp_path, mode="Safe", broker=broker)

    assert result["ok"] is False and result["failed"] == 1
    text = _results(result)
    assert "write_file was refused (approval_denied); no change was made" in text
    assert broker.asked == ["write_file"]
    assert not (tmp_path / "out.txt").exists()
    assert len(transport.calls) == 2  # the write attempt and the answer; no nudge


async def test_a_write_blocked_by_plan_mode_fails_with_the_runtime_cause(tmp_path: Path) -> None:
    transport = MockTransport()
    _write_then("I wrote out.txt with hello.")(transport)

    result = await _spawn("documenter", transport, tmp_path, mode="Plan")

    assert result["ok"] is False
    assert "write_file was refused (blocked_by_mode)" in _results(result)
    assert not (tmp_path / "out.txt").exists()


async def test_an_allowed_write_in_safe_mode_still_succeeds(tmp_path: Path) -> None:
    transport = MockTransport()
    _write_then("Wrote out.txt.")(transport)

    result = await _spawn("implementer", transport, tmp_path, mode="Safe", broker=_Broker(True))

    assert result["ok"] is True
    assert (tmp_path / "out.txt").exists()


# ── criteria 4 and 5: no turn limit; an output cut-off is a failure ──────────


async def test_a_worker_is_not_cut_off_by_a_turn_limit(tmp_path: Path) -> None:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="scout",
            allowed_tools=frozenset({"list_directory"}),
            system_prompt="Look around.",
        )
    )
    transport = MockTransport()
    for index in range(40):
        transport.queue_tool_use("list_directory", {"path": "."}, tool_use_id=f"l{index}")
    transport.queue_response(_final("Surveyed the whole tree.", "done"))

    result = await _spawn("scout", transport, tmp_path, registry=registry, task="Survey it.")

    assert result["ok"] is True
    assert "Surveyed the whole tree." in _results(result)
    assert len(transport.calls) == 41  # forty tool turns and the answer; nothing cut it short


async def test_a_reply_cut_off_by_the_output_limit_is_a_failure(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("The answer begins with", "cut", stop_reason="max_tokens"))

    result = await _spawn("researcher", transport, tmp_path, task="Explain the design.")

    assert result["ok"] is False
    assert "stop reason: output_truncated" in _results(result)
    assert "output-token limit" in _results(result)
    assert len(transport.calls) == 1


# ── long output: a cut-off reply is saved, and workers are told to use a file ─


def _saved_replies(root: Path) -> list[Path]:
    return sorted((root / ".agenthicc" / "subagent-output").glob("*/*.md"))


async def test_a_cut_off_reply_is_saved_to_a_file_the_parent_can_read(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("The answer begins with", "cut", stop_reason="max_tokens"))

    result = await _spawn("researcher", transport, tmp_path, task="Explain the design.")

    assert result["ok"] is False  # an incomplete reply is still a failure
    (saved,) = _saved_replies(tmp_path)
    assert saved.read_text(encoding="utf-8") == "The answer begins with"
    relative = saved.relative_to(tmp_path).as_posix()
    assert f"the partial reply is saved at {relative}" in _results(result)
    # Nothing in the directory can be committed by accident.
    guard = tmp_path / ".agenthicc" / "subagent-output" / ".gitignore"
    assert guard.read_text(encoding="utf-8") == "*\n"


async def test_a_cut_off_reply_is_not_saved_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (workspace / ".agenthicc").symlink_to(elsewhere, target_is_directory=True)
    transport = MockTransport()
    transport.queue_response(_final("The answer begins with", "cut", stop_reason="max_tokens"))

    result = await _spawn("researcher", transport, workspace, task="Explain the design.")

    assert result["ok"] is False
    assert list(elsewhere.rglob("*")) == []
    assert "output-token limit" in _results(result)
    assert "is saved at" not in _results(result)


async def test_an_empty_cut_off_reply_saves_nothing(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("", "cut", stop_reason="max_tokens"))

    result = await _spawn("researcher", transport, tmp_path, task="Explain the design.")

    assert result["ok"] is False
    assert _saved_replies(tmp_path) == []
    assert "is saved at" not in _results(result)


async def test_a_failure_to_save_leaves_the_failure_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".agenthicc").write_text("a file where the directory should be", encoding="utf-8")
    transport = MockTransport()
    transport.queue_response(_final("The answer begins with", "cut", stop_reason="max_tokens"))

    result = await _spawn("researcher", transport, tmp_path, task="Explain the design.")

    assert result["ok"] is False
    assert "stop reason: output_truncated" in _results(result)
    assert "is saved at" not in _results(result)


async def test_a_worker_that_can_write_files_is_told_to_put_long_output_in_one(
    tmp_path: Path,
) -> None:
    from agenthicc.subagents.pool import _worker_output_tokens

    transport = MockTransport()
    _write_then("Wrote out.txt.")(transport)

    result = await _spawn("implementer", transport, tmp_path)

    assert result["ok"] is True
    system = str(transport.calls[0].system)
    assert "## LONG OUTPUT" in system
    assert f"about {_worker_output_tokens(None):,} tokens" in system
    assert "write it to a file in the workspace" in system


async def test_a_read_only_worker_is_not_told_to_write_a_file(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("It is 42.", "answer"))

    result = await _spawn("researcher", transport, tmp_path, task="What is the answer?")

    assert result["ok"] is True
    assert "## LONG OUTPUT" not in str(transport.calls[0].system)


# ── criterion 6 and 7: empty answers ─────────────────────────────────────────


async def test_a_verifier_with_no_verdict_is_not_a_success(tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_tool_use("read_file", {"path": "missing.py"}, tool_use_id="r1")
    transport.queue_response(_final("", "empty-1"))
    transport.queue_response(_final("", "empty-2"))

    result = await _spawn("verifier", transport, tmp_path, task="Verify resize rejects negatives.")

    assert result["ok"] is False
    text = _results(result)
    assert "no final answer" in text
    assert "stop reason: empty_response" in text
    assert "Executed tool call(s): read_file." in text  # kept as partial output


async def test_an_implementer_that_wrote_and_then_said_nothing_still_succeeds(
    tmp_path: Path,
) -> None:
    transport = MockTransport()
    transport.queue_tool_use(
        "write_file", {"path": "out.txt", "content": "hello"}, tool_use_id="w1"
    )
    transport.queue_response(_final("", "empty-1"))
    transport.queue_response(_final("", "empty-2"))

    result = await _spawn("implementer", transport, tmp_path)

    assert result["ok"] is True
    assert "Executed tool call(s): write_file. Affected path(s): out.txt." in _results(result)


# ── criterion 8: the rule belongs to the spec, not the name ──────────────────


def test_builtin_types_declare_their_evidence_rule() -> None:
    declared = {
        name: DEFAULT_REGISTRY.get(name).completion_evidence  # type: ignore[union-attr]
        for name in DEFAULT_REGISTRY.names()
    }
    assert declared["implementer"] == "mutation"
    assert declared["executor"] == "action"
    assert declared["tester"] == "action"
    assert {declared[n] for n in ("explorer", "planner", "reviewer", "verifier", "researcher")} == {
        "none"
    }


async def test_a_custom_type_defaults_to_no_evidence_requirement(tmp_path: Path) -> None:
    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="implementer",  # the name alone no longer implies a rule
            allowed_tools=frozenset({"write_file"}),
            system_prompt="Describe changes.",
        )
    )
    transport = MockTransport()
    transport.queue_response(_final("Described the change.", "prose"))

    result = await _spawn("implementer", transport, tmp_path, registry=registry)

    assert result["ok"] is True
    assert len(transport.calls) == 1


async def test_a_type_that_needs_a_write_tool_is_refused_when_policy_blocks_it(
    tmp_path: Path,
) -> None:
    transport = MockTransport()

    result = await _spawn("implementer", transport, tmp_path, mode="Plan")

    assert result["ok"] is False
    assert result["status"] == "refused"
    assert "implementer needs a file-changing tool but the Plan policy blocks it" in str(
        result["error"]
    )
    assert transport.calls == []  # no provider call was spent


@pytest.mark.parametrize("agent_type", ["implementer", "executor", "tester", "documenter"])
def test_working_roles_inherit_every_tool(agent_type: str) -> None:
    spec = DEFAULT_REGISTRY.get(agent_type)
    assert spec is not None
    assert spec.allowed_tools is None


async def test_a_working_role_receives_every_tool_the_parent_exposes(tmp_path: Path) -> None:
    from lauren_ai._tools import tool

    from agenthicc.tools.capabilities import tool_read

    @tool_read
    @tool()
    async def project_probe(topic: str) -> dict[str, object]:
        """A project-specific tool no built-in role lists."""
        return {"ok": True, "topic": topic}

    transport = MockTransport()
    transport.queue_tool_use("write_file", {"path": "a.txt", "content": "x"}, tool_use_id="w1")
    transport.queue_response(_final("Wrote a.txt.", "done"))
    explorer = MockTransport()
    explorer.queue_response(_final("Nothing found.", "explored"))

    def surface(call: object) -> set[str]:
        return {
            str(item.get("name") if isinstance(item, dict) else getattr(item, "name", ""))
            for item in (getattr(call, "tools", None) or [])
        }

    async def run(agent_type: str, mock: MockTransport) -> None:
        state = AppState.create()
        state.active_mode.set(build_default_registry().get("Yolo"))
        policy = WorkspaceAccessPolicy(
            WorkspaceScope.create(tmp_path), mode_provider=state.active_mode
        )
        spawn = make_spawn_subagents_tool(
            AgentRunnerBase(transport=mock),
            "mock-model",
            [*AGENT_TOOLS, project_probe],
            app_state=state,
            workspace_access=policy,
        )
        token = set_current_workspace_access(policy)
        try:
            await spawn(tasks=[{"type": agent_type, "task": "Do it."}], timeout_s=30)
        finally:
            reset_current_workspace_access(token)

    await run("implementer", transport)
    await run("explorer", explorer)

    inherited = surface(transport.calls[0])
    assert "project_probe" in inherited  # not on any built-in list
    assert {"write_file", "run_bash", "apply_diff", "git_status"} <= inherited
    restricted = surface(explorer.calls[0])
    assert "project_probe" not in restricted
    assert not restricted & {"write_file", "patch_file", "run_bash"}  # read-only by definition


async def test_a_working_role_does_not_inherit_the_parents_control_plane(
    tmp_path: Path,
) -> None:
    """Phase transitions, ask_user and the parent's own spawn tool stay with the parent."""
    from lauren_ai._tools import tool

    from agenthicc.tools.capabilities import tool_control, tool_read

    @tool_control
    @tool()
    async def close_phase(summary: str) -> dict[str, object]:
        """Advance the parent workflow to its next phase."""
        return {"ok": True}

    @tool_control
    @tool()
    async def ask_user(question: str) -> dict[str, object]:
        """Ask the human directly."""
        return {"ok": True}

    @tool_read
    @tool()
    async def spawn_subagents(tasks: str) -> dict[str, object]:
        """The parent's own spawn tool."""
        return {"ok": True}

    transport = MockTransport()
    transport.queue_response(_final("Nothing to change.", "done"))
    state = AppState.create()
    state.active_mode.set(build_default_registry().get("Yolo"))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(tmp_path), mode_provider=state.active_mode)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        [*AGENT_TOOLS, close_phase, ask_user, spawn_subagents],
        app_state=state,
        workspace_access=policy,
    )
    token = set_current_workspace_access(policy)
    try:
        await spawn(tasks=[{"type": "documenter", "task": "Do it."}], timeout_s=30)
    finally:
        reset_current_workspace_access(token)

    shown = {
        str(item.get("name") if isinstance(item, dict) else getattr(item, "name", ""))
        for item in (getattr(transport.calls[0], "tools", None) or [])
    }
    assert {"write_file", "run_bash"} <= shown
    assert not shown & {"close_phase", "ask_user", "spawn_subagents"}


async def test_a_working_role_is_not_held_to_the_session_turn_limit(tmp_path: Path) -> None:
    from agenthicc.config import ExecutionSettings

    registry = SubagentTypeRegistry()
    registry.register(
        SubagentTypeSpec(
            name="builder",
            allowed_tools=None,
            system_prompt="Build it.",
        )
    )
    transport = MockTransport()
    for index in range(12):
        transport.queue_tool_use("list_directory", {"path": "."}, tool_use_id=f"l{index}")
    transport.queue_response(_final("Built it.", "built"))
    state = AppState.create()
    state.active_mode.set(build_default_registry().get("Yolo"))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(tmp_path), mode_provider=state.active_mode)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        list(AGENT_TOOLS),
        app_state=state,
        workspace_access=policy,
        registry=registry,
        execution_settings=ExecutionSettings(max_agent_turns=5),
    )
    token = set_current_workspace_access(policy)
    try:
        result = await spawn(tasks=[{"type": "builder", "task": "Survey it."}], timeout_s=30)
    finally:
        reset_current_workspace_access(token)

    assert result["ok"] is True
    assert "Built it." in _results(result)
    assert len(transport.calls) == 13  # twelve tool turns past the session's limit of five


def test_no_worker_logic_keys_on_the_implementer_name() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "agenthicc" / "subagents" / "pool.py"
    assert 'name == "implementer"' not in source.read_text(encoding="utf-8")
