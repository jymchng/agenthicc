"""CLI entry points for PRD-204 goal runs."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.background import BackgroundStore, BackgroundSupervisor, load_background_settings
from agenthicc.cli.context import CLIContext

from .manager import GoalRunManager, RepositoryValidationError
from .model import GoalRun, GoalRunStatus, RunAgentRecord
from .store import RunStore

if TYPE_CHECKING:
    from agenthicc.runners.tui_session import TUISession


def _manager(ctx: CLIContext) -> GoalRunManager:
    from agenthicc.config import load_config  # noqa: PLC0415

    cfg = ctx.config or load_config(
        cli_overrides=list(ctx.set_overrides),
        cli_secret_overrides=list(ctx.set_secret_overrides),
        config_path=ctx.config_path,
    )
    settings = load_background_settings(
        config_path=ctx.config_path,
        overrides=ctx.set_overrides,
        config=vars(cfg).get("background"),
    )
    root = Path(settings.store_path).expanduser() if settings.store_path else None
    background_store = BackgroundStore(root)
    supervisor = BackgroundSupervisor(
        background_store,
        max_workers=settings.max_workers,
        max_workers_per_project=settings.max_workers_per_project,
        cancel_grace_s=settings.cancel_grace_s,
        wall_timeout_s=settings.wall_timeout_s,
        max_activity_bytes=settings.max_activity_bytes,
        trash_retention_days=settings.trash_retention_days,
    )
    return GoalRunManager(
        store=RunStore(),
        supervisor=supervisor,
        require_git=True,
    )


def _json_or_text(run: GoalRun, *, json_output: bool, detached: bool = False) -> None:
    if json_output:
        payload = run.to_dict()
        payload["detached"] = detached
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("Agenthicc detached run started" if detached else "Agenthicc goal run started")
    print(f"\nRun ID:  {run.run_id}")
    print(f"Goal:    {run.goal}")
    print(f"Status:  {run.status.value}")
    if run.main_session_id:
        print(f"Session ID: {run.main_session_id}")
    if detached:
        print(f"PID:     {run.worker_pid if run.worker_pid is not None else 'unknown'}")
        print(f"\nTrack:   agenthicc agents --run {run.run_id}")
        if run.main_session_id:
            print(f"Attach:  agenthicc attach {run.main_session_id}")
        else:
            print(f"Attach:  agenthicc attach --run {run.run_id}")


def run_goal_cli(ctx: CLIContext) -> None:
    """Dispatch the global ``--goal`` entry point."""

    assert ctx.goal is not None
    # ``--goal`` is intentionally a stable shorthand for the same operation
    # as selecting ``goal_flow`` in the TUI and submitting the goal text. The
    # parser rejects a competing --workflow so detached and attached starts
    # cannot silently select different workflow semantics.
    workflow_name = "goal_flow"
    try:
        manager = _manager(ctx)
        if ctx.detach:
            run = manager.start_detached(
                ctx.goal,
                repository=Path.cwd(),
                workflow_name=workflow_name,
                config_path=ctx.config_path,
                set_overrides=ctx.set_overrides,
                set_secret_overrides=ctx.set_secret_overrides,
                dangerously_skip_permissions=ctx.flags.dangerously_skip_permissions,
            )
            _json_or_text(run, json_output=ctx.json_output, detached=True)
            return
        run = manager.create(
            ctx.goal,
            repository=Path.cwd(),
            workflow_name=workflow_name,
            detached=False,
        )
        asyncio.run(_run_attached_goal(ctx, manager, run))
    except (RepositoryValidationError, ValueError) as exc:
        if ctx.json_output:
            print(json.dumps({"status": "error", "message": str(exc)}, sort_keys=True))
        else:
            print(f"Unable to start goal run: {exc}")
        raise SystemExit(3) from exc
    except (RuntimeError, OSError) as exc:
        if ctx.json_output:
            print(json.dumps({"status": "error", "message": str(exc)}, sort_keys=True))
        else:
            print(f"Unable to start goal run: {exc}")
        raise SystemExit(1) from exc


async def _run_attached_goal(ctx: CLIContext, manager: GoalRunManager, run: GoalRun) -> None:
    """Run an attached goal through the normal TUI and submit it exactly once."""

    # TUISession.run is the existing owner of command registration and first
    # frame startup.  The small adapter schedules the initial command only
    # after that boundary, avoiding a second agent loop or direct provider call.
    from agenthicc.runners import tui_session as tui_module  # noqa: PLC0415
    from agenthicc.tui.runtime.commands import SendMessageCommand  # noqa: PLC0415

    original_run = tui_module.TUISession.run

    async def run_with_goal(session: "TUISession") -> None:
        original_task = asyncio.create_task(original_run(session))
        session_id = ""
        submitted = False
        try:
            command_bus = session._ctx.command_bus
            for _ in range(2_000):
                handlers = vars(command_bus).get("_handlers", {})
                if SendMessageCommand in handlers:
                    session_ctx = session._ctx
                    session_id = str(session_ctx.session_id)
                    if session_id:
                        setattr(session_ctx, "goal_run_id", run.run_id)
                        manager.store.update(
                            run.run_id,
                            status=GoalRunStatus.RUNNING,
                            started_at=run.created_at,
                            main_session_id=session_id,
                            main_agent_id=session_id,
                        )
                        manager.store.link_agent(
                            run.run_id,
                            RunAgentRecord(
                                agent_id=session_id,
                                run_id=run.run_id,
                                role="main",
                                session_id=session_id,
                                status="running",
                                started_at=run.created_at,
                                last_heartbeat_at=run.created_at,
                                last_activity_at=run.created_at,
                            ),
                        )
                    await session.handle_send(SendMessageCommand(text=run.goal, source="goal"))
                    submitted = True
                    break
                await asyncio.sleep(0.01)
            await original_task
            if submitted:
                completed_at = time.time()
                manager.store.update(
                    run.run_id,
                    status=GoalRunStatus.COMPLETED,
                    completed_at=completed_at,
                    exit_code=0,
                    result_summary="Attached TUI session exited.",
                )
                manager.store.link_agent(
                    run.run_id,
                    RunAgentRecord(
                        agent_id=session_id,
                        run_id=run.run_id,
                        role="main",
                        session_id=session_id,
                        status="completed",
                        completed_at=completed_at,
                        last_activity_at=completed_at,
                    ),
                )
            return None
        except asyncio.CancelledError:
            if submitted:
                manager.store.update(run.run_id, status=GoalRunStatus.CANCELLED)
            raise
        except Exception as exc:
            if submitted:
                manager.store.update(
                    run.run_id,
                    status=GoalRunStatus.FAILED,
                    failure_reason=f"{type(exc).__name__}: {exc}",
                    exit_code=1,
                )
            raise
        finally:
            if not original_task.done():
                original_task.cancel()
                await asyncio.gather(original_task, return_exceptions=True)

    tui_module.TUISession.run = run_with_goal  # type: ignore[assignment]
    try:
        from agenthicc.runners.tui_session import _run_tui_session  # noqa: PLC0415

        await _run_tui_session(
            cli_overrides=list(ctx.set_overrides),
            cli_secret_overrides=list(ctx.set_secret_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
            config_path=ctx.config_path,
            mode_name=ctx.mode_name,
            workflow_name=run.workflow_name,
            config=ctx.config,
            cwd=run.repository_root,
        )
    finally:
        tui_module.TUISession.run = original_run  # type: ignore[method-assign]
