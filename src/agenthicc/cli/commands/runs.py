"""CLI control plane for durable PRD-204 goal runs."""

from __future__ import annotations

import json as json_module

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group, optional_positionals
from agenthicc.runs.cli import _manager


@group("runs", help="Inspect and control durable goal runs")
def _runs_group() -> None: ...


def _payload(ctx: CLIContext, run_id: str) -> dict[str, object]:
    run = _manager(ctx).projection(run_id)
    return run.to_dict()


@command("runs", help="List durable goal runs")
def runs(ctx: CLIContext, status: str = "", json: bool = False) -> None:
    """List goal runs; ``runs`` is an alias of ``runs list``."""

    store = _manager(ctx).store
    try:
        runs_value = store.list(status=status or None)
    except ValueError as exc:
        print(f"Invalid run status: {exc}")
        raise SystemExit(2) from exc
    manager = _manager(ctx)
    payload = [manager.projection(item.run_id).to_dict() for item in runs_value]
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload:
        print("No goal runs.")
        return
    for item in payload:
        print(
            f"{str(item.get('run_id', ''))[:24]}  "
            f"{str(item.get('status', 'unknown')):16}  "
            f"{str(item.get('workflow_name', '')):16}  "
            f"{str(item.get('goal', ''))[:64]}"
        )


@command("runs", "list", help="List durable goal runs")
def runs_list(ctx: CLIContext, status: str = "", json: bool = False) -> None:
    runs(ctx, status=status, json=json)


@command("runs", "show", help="Show one durable goal run")
def runs_show(ctx: CLIContext, run_id: str, json: bool = False) -> None:
    try:
        payload = _payload(ctx, run_id)
    except KeyError:
        print(f"Run not found: {run_id}")
        raise SystemExit(1) from None
    if json:
        print(json_module.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json_module.dumps(payload, indent=2, sort_keys=True))


@command("runs", "cancel", help="Cancel a goal run and its active main session")
def runs_cancel(ctx: CLIContext, run_id: str) -> None:
    try:
        run = _manager(ctx).cancel(run_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to cancel {run_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"cancel: {run.run_id} → {run.status.value}")


@command("runs", "resume", help="Resume a recoverable goal run")
def runs_resume(ctx: CLIContext, run_id: str) -> None:
    manager = _manager(ctx)
    try:
        resumed = (
            manager.resume(run_id)
            if ctx.mode_name is None
            else manager.resume(run_id, mode_name=ctx.mode_name)
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to resume {run_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"resume: {resumed.main_session_id} → {resumed.status.value}")


@command("attach", help="Attach the TUI to one exact background session")
@optional_positionals("session_id")
async def attach(ctx: CLIContext, session_id: str = "", run: str = "", json: bool = False) -> None:
    """Attach an exact session; goal runs require the explicit ``--run`` form."""

    if run:
        await _attach_goal_run(ctx, run, json_output=json)
        return
    if not session_id:
        message = "attach requires an exact SESSION_ID (or explicit --run RUN_ID)"
        if json:
            print(json_module.dumps({"status": "error", "error": message}))
        else:
            print(message)
        raise SystemExit(2)
    if session_id.startswith("run_"):
        message = (
            f"{session_id} is a goal-run ID, not a session ID; use "
            f"agenthicc agents --run {session_id} or agenthicc attach SESSION_ID --run {session_id}"
        )
        if json:
            print(json_module.dumps({"status": "error", "error": message}))
        else:
            print(message)
        raise SystemExit(2)
    from agenthicc.cli.commands.background import attach_background_session  # noqa: PLC0415

    await attach_background_session(ctx, session_id, json_output=json)


async def _attach_goal_run(ctx: CLIContext, run_id: str, *, json_output: bool = False) -> None:
    """Retain goal-run attachment behind an explicit, unambiguous option."""

    manager = _manager(ctx)
    try:
        foreground = manager.attach(run_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        message = f"Unable to attach goal run {run_id}: {exc}"
        if json_output:
            print(json_module.dumps({"run_id": run_id, "status": "error", "error": str(exc)}))
        else:
            print(message)
        raise SystemExit(1) from exc

    if json_output:
        print(
            json_module.dumps(
                {
                    "run_id": run_id,
                    "session_id": foreground.session_id,
                    "status": "handoff_ready",
                },
                sort_keys=True,
            )
        )
        return

    try:
        from agenthicc.runners.tui_session import _run_tui_session  # noqa: PLC0415

        await _run_tui_session(
            resume_id=foreground.session_id,
            cli_overrides=list(ctx.set_overrides),
            cli_secret_overrides=list(ctx.set_secret_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
            config_path=ctx.config_path,
            cwd=foreground.cwd,
            config=ctx.config,
            mode_name=ctx.mode_name,
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to attach goal run {run_id}: {exc}")
        raise SystemExit(1) from exc
