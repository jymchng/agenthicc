"""Additional branch coverage for built-in slash-command handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.commands.builtins import (
    _cmd_cancel,
    _cmd_ps,
    _cmd_replay,
    _cmd_skills,
    _cmd_stop,
    _cmd_tools,
    _cmd_workflows,
    _mcp_busy_policy,
    _read_only_without_args,
    _reloadable_list_policy,
    _workflows_busy_policy,
)
from agenthicc.commands.command import CommandContext, UsageSnapshot
from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.config import AgenthiccConfig
from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult, SkillDiagnostic

pytestmark = pytest.mark.unit


def _ctx(tmp_path: Path) -> CommandContext:
    return CommandContext(
        text="",
        args="",
        model="model",
        console=Console(record=True),
        config=AgenthiccConfig(),
        session_id="session",
        command_registry=UnifiedCommandRegistry(),
        skills={},
        tools=[],
        tool_sources={},
        workflow_registry=None,
    )


def test_builtin_busy_policy_helpers() -> None:
    from agenthicc.commands.command import BusyPolicy

    assert _read_only_without_args("") is BusyPolicy.IMMEDIATE_READ_ONLY
    assert _read_only_without_args("reload") is BusyPolicy.QUEUE
    assert _mcp_busy_policy("status") is BusyPolicy.IMMEDIATE_READ_ONLY
    assert _mcp_busy_policy("connect") is BusyPolicy.QUEUE
    assert _reloadable_list_policy("") is BusyPolicy.IMMEDIATE_READ_ONLY
    assert _reloadable_list_policy("reload") is BusyPolicy.QUEUE
    assert _workflows_busy_policy("") is BusyPolicy.IMMEDIATE_READ_ONLY
    assert _workflows_busy_policy("runs") is BusyPolicy.IMMEDIATE_READ_ONLY
    assert _workflows_busy_policy("reload") is BusyPolicy.QUEUE


def test_cancel_replay_and_tools_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    assert _cmd_cancel(ctx)
    ctx.cancel_active = lambda: True
    assert _cmd_cancel(ctx)

    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log.find_latest_session_for_cwd", lambda: "session"
    )
    ctx.args = ""
    assert _cmd_replay(ctx)
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log.find_latest_session_for_cwd", lambda: "other"
    )
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        "agenthicc.tui.runtime.session_log.get_session_log_path", lambda _sid: missing
    )
    assert _cmd_replay(ctx)

    ctx.args = "reload"
    ctx.reload_tools = None
    assert _cmd_tools(ctx)
    ctx.reload_tools = lambda: (_ for _ in ()).throw(RuntimeError("tools failed"))
    assert _cmd_tools(ctx)
    ctx.args = "unknown"
    assert _cmd_tools(ctx)


def test_terminal_ps_and_stop_handlers_cover_manager_and_overlay_paths(tmp_path: Path) -> None:
    from agenthicc.background.terminals import TerminalRecord, TerminalState

    ctx = _ctx(tmp_path)
    ctx.args = ""
    assert _cmd_ps(ctx)
    assert _cmd_stop(ctx)

    record = TerminalRecord(
        terminal_id="term-1",
        session_id="session",
        project_root=str(tmp_path),
        cwd=str(tmp_path),
        kind="exec",
        command="sleep",
        label="sleep",
        state=TerminalState.RUNNING,
        created_at=1.0,
    )
    calls: list[tuple[object, ...]] = []
    manager = SimpleNamespace(
        list_records=lambda: [record],
        request_stop_all=lambda **kwargs: calls.append(("all", kwargs)) or 1,
        request_stop=lambda target, **kwargs: calls.append((target, kwargs)) or target == "term-1",
    )
    ctx.terminal_manager = manager
    ctx.args = "--json"
    assert _cmd_ps(ctx)
    ctx.args = ""
    assert _cmd_ps(ctx)
    ctx.args = "all"
    assert _cmd_stop(ctx)
    ctx.args = "all --confirm --force"
    assert _cmd_stop(ctx)
    ctx.args = "term-1"
    assert _cmd_stop(ctx)
    ctx.args = "missing"
    assert _cmd_stop(ctx)

    overlays: list[object] = []
    ctx.set_pending_menu = overlays.append
    ctx.close_overlay = lambda: None
    ctx.args = "term-1"
    assert _cmd_ps(ctx)
    assert overlays


def test_skill_reload_diagnostics_and_permission_filter(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    skill = SkillDef("Allowed", "allowed", tmp_path, _body="body")
    denied = SkillDef("Denied", "denied", tmp_path, allowed_agents=("other",), _body="no")
    ctx.skills = {"allowed": skill, "denied": denied}
    ctx.reload_skills = lambda: SkillDiscoveryResult(
        {"allowed": skill, "denied": denied},
        (SkillDiagnostic(path=tmp_path, code="warning", message="warn", severity="warning"),),
    )
    ctx.args = "reload"
    assert _cmd_skills(ctx)
    assert "warning" in ctx.console.export_text().lower()


def test_workflow_reload_and_tool_metadata_rendering(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.reload_workflows = lambda: (False, "workflow reload failed")
    ctx.args = "reload"
    assert _cmd_workflows(ctx)
    ctx.reload_workflows = lambda: (_ for _ in ()).throw(RuntimeError("workflow broke"))
    assert _cmd_workflows(ctx)
    ctx.args = "wrong"
    assert _cmd_workflows(ctx)

    async def read_tool() -> dict[str, object]:
        return {}

    read_tool.__name__ = "read_tool"
    ctx.args = ""
    ctx.tools = [read_tool]
    ctx.tool_sources = {"read_tool": "plugin"}
    assert _cmd_tools(ctx)


def test_workflow_runs_uses_recovery_callbacks_and_overlay(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.overlays.registry_list import WorkflowRunsOverlay

    ctx = _ctx(tmp_path)
    selected: list[str] = []
    closed: list[bool] = []
    ctx.args = "runs"
    ctx.list_workflow_runs = lambda: [
        SimpleNamespace(
            run_id="paused-run",
            workflow_name="code_plan",
            current_phase="implement",
            status="paused",
            intent="Implement the change",
            checkpoint=SimpleNamespace(created_at=10.0),
        )
    ]
    ctx.resume_workflow = lambda run_id: selected.append(run_id) or True
    ctx.set_pending_menu = lambda overlay: (closed.append(False), setattr(ctx, "_overlay", overlay))
    ctx.close_overlay = lambda: closed.append(True)

    assert _cmd_workflows(ctx)
    overlay = getattr(ctx, "_overlay")
    assert isinstance(overlay, WorkflowRunsOverlay)
    overlay.handle_key(Key.ENTER, "")
    assert selected == ["paused-run"]
    assert closed[-1] is True


def test_usage_command_renders_unavailable_and_available_snapshots(tmp_path: Path) -> None:
    from agenthicc.commands.builtins import _cmd_usage

    ctx = _ctx(tmp_path)
    ctx.usage_snapshot = lambda: None
    assert _cmd_usage(ctx)
    ctx.usage_snapshot = lambda: UsageSnapshot(
        input_tokens=1234,
        output_tokens=56,
        cost_usd=0.12,
        active_run=True,
        queue_depth=2,
        usage_status="unavailable",
        cost_status="unavailable",
    )
    assert _cmd_usage(ctx)
    assert "unknown" in ctx.console.export_text().lower()
