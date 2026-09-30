"""Coverage for the PRD-204 command/control adapters."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession
from agenthicc.cli.context import CLIContext
from agenthicc.runs.model import GoalRun, GoalRunStatus
from agenthicc.runs.store import RunNotFound, RunStore


def _ctx() -> CLIContext:
    return CLIContext()


class _GoalManager:
    def __init__(self, root: Path) -> None:
        self.store = RunStore(root)
        self.run = self.store.create(
            GoalRun.create("Goal from CLI", repository="/repo", run_id="run_entry")
        )
        self.created = False
        self.detached = False
        self.detached_kwargs: dict[str, object] = {}

    def create(self, goal: str, **_kwargs: object) -> GoalRun:
        self.created = True
        return self.run.evolve(goal=goal)

    def start_detached(self, goal: str, **_kwargs: object) -> GoalRun:
        self.detached = True
        self.detached_kwargs = dict(_kwargs)
        return self.run.evolve(goal=goal, status=GoalRunStatus.RUNNING)


class _Manager:
    def __init__(self, root: Path) -> None:
        self.store = RunStore(root)
        self.run = self.store.create(
            GoalRun.create("Implement the API", repository="/repo", run_id="run_cli")
        )
        self.cancelled = False
        self.resumed = False

    def projection(self, run_id: str) -> GoalRun:
        if run_id != self.run.run_id:
            raise RunNotFound(run_id)
        return self.store.get(run_id)

    def cancel(self, run_id: str) -> GoalRun:
        if run_id != self.run.run_id:
            raise RunNotFound(run_id)
        self.cancelled = True
        self.run = self.store.update(run_id, status=GoalRunStatus.CANCELLED)
        return self.run

    def resume(self, run_id: str) -> GoalRun:
        if run_id != self.run.run_id:
            raise RunNotFound(run_id)
        self.resumed = True
        self.run = self.store.update(run_id, status=GoalRunStatus.RUNNING)
        return self.run

    def attach(self, run_id: str) -> BackgroundSession:
        if run_id != self.run.run_id:
            raise RunNotFound(run_id)
        return BackgroundSession.create(
            "session-cli", title="Goal", cwd="/repo", workflow_name="goal_flow", intent="goal"
        )


def test_goal_run_output_and_command_handlers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.runs import cli
    from agenthicc.cli.commands import runs as commands

    manager = _Manager(tmp_path / "runs")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(commands, "_manager", lambda _ctx: manager)
    try:
        cli._json_or_text(manager.run, json_output=True, detached=True)
        detached_payload = json.loads(capsys.readouterr().out)
        assert detached_payload["detached"] is True
        assert detached_payload["pid"] is None
        cli._json_or_text(manager.run, json_output=False, detached=True)
        output = capsys.readouterr().out
        assert "Attach:  agenthicc attach --run run_cli" in output
        pid_run = manager.run.evolve(worker_pid=12345)
        cli._json_or_text(pid_run, json_output=True, detached=True)
        assert json.loads(capsys.readouterr().out)["pid"] == 12345
        pid_run = pid_run.evolve(main_session_id="session-cli")
        cli._json_or_text(pid_run, json_output=False, detached=True)
        output = capsys.readouterr().out
        assert "PID:     12345" in output
        assert "Session ID: session-cli" in output
        assert "Attach:  agenthicc attach session-cli" in output
        assert "Attach:  agenthicc attach run_cli" not in output
        cli._json_or_text(manager.run, json_output=False, detached=False)
        assert "Run ID" in capsys.readouterr().out

        commands.runs(_ctx(), json=True)
        assert json.loads(capsys.readouterr().out)[0]["run_id"] == "run_cli"
        commands.runs_list(_ctx(), json=False)
        assert "run_cli" in capsys.readouterr().out
        commands.runs_show(_ctx(), "run_cli", json=True)
        assert json.loads(capsys.readouterr().out)["goal"] == "Implement the API"
        commands.runs_show(_ctx(), "run_cli", json=False)
        assert "Implement the API" in capsys.readouterr().out

        commands.runs_cancel(_ctx(), "run_cli")
        assert manager.cancelled
        commands.runs_resume(_ctx(), "run_cli")
        assert manager.resumed
    finally:
        monkeypatch.undo()


def test_goal_run_command_errors_and_manager_construction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.runs import cli
    from agenthicc.cli.commands import runs as commands

    manager = _Manager(tmp_path / "runs")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(commands, "_manager", lambda _ctx: manager)
    try:
        with pytest.raises(SystemExit) as status:
            commands.runs(_ctx(), status="not-a-status")
        assert status.value.code == 2
        assert "Invalid run status" in capsys.readouterr().out

        with pytest.raises(SystemExit):
            commands.runs_show(_ctx(), "missing")
        assert "Run not found" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            commands.runs_cancel(_ctx(), "missing")
        with pytest.raises(SystemExit):
            commands.runs_resume(_ctx(), "missing")
        assert "Unable to resume" in capsys.readouterr().out

        # Exercise the production manager factory with an isolated HOME and
        # the current configuration snapshot. It must not create a run.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        production_manager = cli._manager(CLIContext())
        assert production_manager.store.root == tmp_path / "home" / ".agenthicc" / "runs"
    finally:
        monkeypatch.undo()


def test_goal_cli_routes_goal_flow_for_attached_and_detached_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.runs import cli

    manager = _GoalManager(tmp_path / "runs")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cli, "_manager", lambda _ctx: manager)
    attached: list[str] = []

    async def fake_attached(_ctx: CLIContext, _manager: object, run: GoalRun) -> None:
        attached.append(run.goal)

    monkeypatch.setattr(cli, "_run_attached_goal", fake_attached)
    try:
        cli.run_goal_cli(CLIContext(goal="attached goal"))
        assert manager.created
        assert attached == ["attached goal"]
        assert "Agenthicc goal run started" not in capsys.readouterr().out

        cli.run_goal_cli(
            CLIContext(goal="detached goal", detach=True, mode_name="Yolo", json_output=False)
        )
        assert manager.detached
        assert manager.detached_kwargs["mode_name"] == "Yolo"
        output = capsys.readouterr().out
        assert "Agenthicc detached run started" in output

        cli.run_goal_cli(CLIContext(goal="json goal", detach=True, json_output=True))
        assert json.loads(capsys.readouterr().out)["detached"] is True
    finally:
        monkeypatch.undo()


def test_goal_cli_reports_validation_and_runtime_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenthicc.runs import cli
    from agenthicc.runs.manager import RepositoryValidationError

    class _Failing:
        def create(self, *_args: object, **_kwargs: object) -> GoalRun:
            raise RepositoryValidationError("bad repository")

        def start_detached(self, *_args: object, **_kwargs: object) -> GoalRun:
            raise RuntimeError("provider unavailable")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cli, "_manager", lambda _ctx: _Failing())
    try:
        with pytest.raises(SystemExit) as attached:
            cli.run_goal_cli(CLIContext(goal="goal"))
        assert attached.value.code == 3
        assert "Unable to start goal run" in capsys.readouterr().out

        with pytest.raises(SystemExit) as detached:
            cli.run_goal_cli(CLIContext(goal="goal", detach=True, json_output=True))
        assert detached.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_attached_goal_adapter_submits_once_and_records_completion(tmp_path: Path) -> None:
    from agenthicc.runners import tui_session as tui_module
    from agenthicc.runs import cli
    from agenthicc.tui.runtime.commands import SendMessageCommand

    manager = _GoalManager(tmp_path / "runs")
    sent: list[str] = []

    class _Session:
        def __init__(self) -> None:
            self._ctx = SimpleNamespace(
                session_id="attached-session",
                command_bus=SimpleNamespace(_handlers={SendMessageCommand: object()}),
                mode_manager=SimpleNamespace(active_name="Safe"),
            )

        async def run(self) -> None:
            return None

        async def handle_send(self, command: SendMessageCommand) -> None:
            sent.append(command.text)

    # The adapter's original task must receive the same fake session that the
    # TUI bootstrap creates, so make the bootstrap expose it explicitly.
    session = _Session()

    async def bootstrap(**_kwargs: object) -> None:
        await session.run()

    original_class = tui_module.TUISession
    original_bootstrap = tui_module._run_tui_session
    tui_module.TUISession = _Session  # type: ignore[assignment]
    tui_module._run_tui_session = bootstrap  # type: ignore[assignment]
    try:
        await cli._run_attached_goal(CLIContext(goal="attached"), manager, manager.run)
    finally:
        tui_module.TUISession = original_class
        tui_module._run_tui_session = original_bootstrap

    assert sent == [manager.run.goal]
    saved = manager.store.get(manager.run.run_id)
    assert saved.status is GoalRunStatus.COMPLETED
    assert saved.main_session_id == "attached-session"


@pytest.mark.asyncio
async def test_attached_goal_adapter_records_provider_failure(tmp_path: Path) -> None:
    from agenthicc.runners import tui_session as tui_module
    from agenthicc.runs import cli
    from agenthicc.tui.runtime.commands import SendMessageCommand

    manager = _GoalManager(tmp_path / "runs")

    class _Session:
        def __init__(self) -> None:
            self._ctx = SimpleNamespace(
                session_id="failed-session",
                command_bus=SimpleNamespace(_handlers={SendMessageCommand: object()}),
                mode_manager=SimpleNamespace(active_name="Safe"),
            )

        async def run(self) -> None:
            raise RuntimeError("TUI failed")

        async def handle_send(self, _command: SendMessageCommand) -> None:
            return None

    session = _Session()

    async def bootstrap(**_kwargs: object) -> None:
        await session.run()

    original_class = tui_module.TUISession
    original_bootstrap = tui_module._run_tui_session
    tui_module.TUISession = _Session  # type: ignore[assignment]
    tui_module._run_tui_session = bootstrap  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="TUI failed"):
            await cli._run_attached_goal(CLIContext(goal="failed"), manager, manager.run)
    finally:
        tui_module.TUISession = original_class
        tui_module._run_tui_session = original_bootstrap

    saved = manager.store.get(manager.run.run_id)
    assert saved.status is GoalRunStatus.FAILED
    assert "TUI failed" in saved.failure_reason
