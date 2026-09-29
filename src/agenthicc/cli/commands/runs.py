"""CLI control plane for durable PRD-204 goal runs."""

from __future__ import annotations

import json as json_module

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group
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
        resumed = manager.resume(run_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to resume {run_id}: {exc}")
        raise SystemExit(1) from exc
    print(f"resume: {resumed.main_session_id} → {resumed.status.value}")


@command("attach", help="Attach the TUI to a goal run")
async def attach(ctx: CLIContext, run_id: str) -> None:
    manager = _manager(ctx)
    try:
        foreground = manager.attach(run_id)
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
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"Unable to attach {run_id}: {exc}")
        raise SystemExit(1) from exc
