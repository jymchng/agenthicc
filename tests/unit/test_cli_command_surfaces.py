"""Direct tests for the script-facing CLI command handlers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.cli.context import CLIContext

pytestmark = pytest.mark.unit


def test_config_init_show_and_project_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.cli.commands import config, init

    monkeypatch.chdir(tmp_path)
    ctx = CLIContext(set_overrides=("execution.max_agent_turns=3",))
    config.config_init(ctx)
    config.config_init(ctx)
    config.config_init(ctx, force=True)
    config.config_show(ctx)

    plan = SimpleNamespace(changed=False, exists=False, preview=lambda: "")
    monkeypatch.setattr(init, "build_bootstrap_plan", lambda cwd: plan)
    init.init_project(ctx)
    plan.changed = True
    init.init_project(ctx)
    plan.exists = True
    init.init_project(ctx, write=True)
    monkeypatch.setattr(init, "write_bootstrap_plan", lambda plan, force: tmp_path / "AGENTS.md")
    init.init_project(ctx, write=True, force=True)


def test_sessions_trust_and_inspection_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.cli.commands import sessions, trust

    monkeypatch.chdir(tmp_path)
    ctx = CLIContext()
    monkeypatch.setattr(sessions, "_do_sessions", lambda: None, raising=False)
    monkeypatch.setattr("agenthicc.sessions._do_sessions", lambda: None)
    sessions.sessions_list(ctx)
    sessions.sessions_show(ctx, "missing")
    sessions.sessions_export(ctx, "missing")
    sessions.sessions_inspect(ctx, "missing")
    summary = {
        "session_id": "s",
        "metadata": {"model": "m", "cwd": ".", "created_at": 1, "last_active": 2},
        "artifacts": {"conversation": {"present": True, "records": 2, "skipped_lines": 1}},
        "kernel": {"events": 1},
        "conversation": {
            "events": 2,
            "tool_calls": 1,
            "errors": 0,
            "tokens": {"input": 2, "output": 3, "cost_usd": 0.1},
        },
        "workflows": {
            "total": 1,
            "complete": 1,
            "failed": 0,
            "incomplete": 0,
            "runs": [{"workflow_name": "demo", "status": "complete", "phases_run": 1}],
        },
        "resume": {"incomplete": True, "turn_id": "t", "tool_records": 1},
        "redactions": 1,
    }
    monkeypatch.setattr("agenthicc.tui.runtime.session_export.inspect_session", lambda sid: summary)
    sessions.sessions_inspect(ctx, "s")
    sessions.sessions_inspect(ctx, "s", json=True)

    trust.trust_cli(ctx)
    cli_dir = tmp_path / ".agenthicc" / "cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "plugin.py").write_text("# plugin\n", encoding="utf-8")
    (cli_dir / "_private.py").write_text("# private\n", encoding="utf-8")
    trust.trust_cli(ctx)
    assert (tmp_path / ".agenthicc" / "trusted_cli.json").exists()


def test_workflow_list_and_run_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    from agenthicc.cli.commands import workflows

    class Demo:
        name = "demo"
        description = "Demo workflow"
        mode_bindings = ["Plan"]
        phases = [
            SimpleNamespace(
                name="plan", agent_type="planner", next="execute", on_reject=None, parallel_with=()
            )
        ]

    entry = SimpleNamespace(source="project")
    registry = SimpleNamespace(all=lambda: [Demo], get_entry=lambda name: entry)
    monkeypatch.setattr(workflows, "_workflow_registry", lambda: registry)
    workflows.workflows_list(CLIContext())
    workflows.workflows_list(CLIContext(), json=True)

    result = SimpleNamespace(
        workflow_name="demo",
        status="complete",
        session_id="s",
        run_id="r",
        phases=["plan"],
        error=None,
        to_dict=lambda: {"status": "complete"},
    )

    async def run(*args: object) -> object:
        return result

    monkeypatch.setattr("agenthicc.runners.headless.run_headless_workflow", run)
    import asyncio

    asyncio.run(workflows.workflows_run(CLIContext(), "demo", "intent"))
    asyncio.run(workflows.workflows_run(CLIContext(), "demo", "intent", json=True))

    async def fail(*args: object) -> object:
        raise ValueError("bad workflow")

    monkeypatch.setattr("agenthicc.runners.headless.run_headless_workflow", fail)
    asyncio.run(workflows.workflows_run(CLIContext(), "demo", "intent"))
    asyncio.run(workflows.workflows_run(CLIContext(), "demo", "intent", json=True))
