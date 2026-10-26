"""Session management commands — list, show, inspect, and export sessions."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from agenthicc.cli.context import CLIContext
from agenthicc.runners.session_lease import (
    SessionAlreadyActiveError,
    SessionStorageError,
    format_session_conflict,
)


def sessions_list(ctx: CLIContext, page: int = 1, page_size: int = 0) -> None:
    """Open saved sessions in the agents hub, or print a list when piped.

    On a terminal this is ``agenthicc agents --tab history`` (AGX-05 §5.5):
    saved sessions are listed with finished background agents, newest first,
    and Enter resumes one. Piped output keeps the deterministic page listing.
    """
    from agenthicc.sessions import _do_sessions
    from agenthicc.tui.terminal.backend import get_backend

    if not get_backend().is_interactive():
        _do_sessions(page=page, page_size=page_size or 50)
        return
    import asyncio

    from agenthicc.cli.commands.background import _open_hub

    asyncio.run(_open_hub(ctx, tab="history"))


async def open_saved_session(ctx: CLIContext, session_id: str) -> None:
    """Resume a saved foreground session in its own project directory."""
    import asyncio

    from agenthicc.runners.tui_session import _run_tui_session
    from agenthicc.sessions import _load_all_session_indexes

    record = _load_all_session_indexes().get(session_id, {})
    cwd_value = record.get("cwd")
    cwd = (
        cwd_value
        if isinstance(cwd_value, str) and await asyncio.to_thread(os.path.isdir, cwd_value)
        else None
    )
    try:
        await _run_tui_session(
            resume_id=session_id,
            cli_overrides=list(ctx.set_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
            config_path=ctx.config_path,
            cli_secret_overrides=list(ctx.set_secret_overrides),
            cwd=cwd,
            config=ctx.config,
        )
    except SessionAlreadyActiveError as exc:
        print(format_session_conflict(exc), file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc
    except SessionStorageError as exc:
        print(f"error: {exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def sessions_show(ctx: CLIContext, session_id: str) -> None:
    """Print stored events for SESSION_ID."""
    import json
    from agenthicc.sessions import _get_session_log_path

    log_path = _get_session_log_path(session_id)
    if log_path is None or not log_path.exists():
        print(f"Session not found: {session_id}")
        return
    for line in log_path.read_text().splitlines():
        try:
            ev = json.loads(line)
            print(f"  {ev.get('event_type', '?'):30} {ev.get('timestamp', '')}")
        except Exception:  # noqa: BLE001
            print(f"  {line}")


def sessions_export(ctx: CLIContext, session_id: str, output: str = "") -> None:
    """Export SESSION_ID and its durable artifacts to OUTPUT or SESSION_ID.json."""

    from agenthicc.tui.runtime.session_export import export_session

    destination = Path(output) if output else Path(f"{session_id}.json")
    try:
        exported = export_session(session_id, destination)
    except (FileNotFoundError, ValueError, IsADirectoryError) as exc:
        print(str(exc))
        return
    print(f"Exported session {session_id} to {exported}")


def sessions_inspect(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    """Summarize SESSION_ID, including artifacts, usage, workflows, and resume state."""
    import json as json_module

    from agenthicc.tui.runtime.session_export import inspect_session

    try:
        summary = inspect_session(session_id)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc))
        return
    if json:
        print(json_module.dumps(summary, indent=2, sort_keys=True))
        return
    _print_session_inspection(summary)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _print_session_inspection(summary: dict[str, object]) -> None:
    """Print the stable, human-readable form of an inspection summary."""
    metadata = _mapping(summary.get("metadata"))
    session_id = summary.get("session_id", "<unknown>")
    print(f"Session: {session_id}")
    for key, label in (
        ("model", "Model"),
        ("cwd", "CWD"),
        ("created_at", "Created"),
        ("last_active", "Last active"),
    ):
        if key in metadata:
            print(f"{label}: {metadata[key]}")

    print("Artifacts:")
    for name, value in _mapping(summary.get("artifacts")).items():
        artifact = _mapping(value)
        state = "present" if artifact.get("present") else "missing"
        records = _integer(artifact.get("records"))
        skipped = _integer(artifact.get("skipped_lines"))
        suffix = f", {skipped} corrupt" if skipped else ""
        print(f"  {name}: {state} ({records} records{suffix})")

    kernel = _mapping(summary.get("kernel"))
    conversation = _mapping(summary.get("conversation"))
    tokens = _mapping(conversation.get("tokens"))
    print(
        "Events: "
        f"{_integer(kernel.get('events'))} kernel, "
        f"{_integer(conversation.get('events'))} conversation, "
        f"{_integer(conversation.get('tool_calls'))} tool calls, "
        f"{_integer(conversation.get('errors'))} errors"
    )
    usage_status = tokens.get("status", "unavailable")
    cost_status = tokens.get("cost_status", "unavailable")
    input_text = "unknown" if usage_status == "unavailable" else str(_integer(tokens.get("input")))
    output_text = (
        "unknown" if usage_status == "unavailable" else str(_integer(tokens.get("output")))
    )
    cost_text = (
        "unknown" if cost_status == "unavailable" else f"${_number(tokens.get('cost_usd')):.4f}"
    )
    print(
        "Tokens: "
        f"{input_text} input, "
        f"{output_text} output, "
        f"{cost_text} (status={usage_status}, cost_status={cost_status})"
    )

    workflows = _mapping(summary.get("workflows"))
    print(
        "Workflows: "
        f"{_integer(workflows.get('total'))} total, "
        f"{_integer(workflows.get('complete'))} complete, "
        f"{_integer(workflows.get('failed'))} failed, "
        f"{_integer(workflows.get('incomplete'))} incomplete"
    )
    owner = _mapping(summary.get("owner"))
    owner_text = str(owner.get("state", "unknown"))
    if "pid" in owner:
        owner_text += f" (pid={owner.get('pid')}, host={owner.get('host', 'unknown')})"
    print(f"Owner: {owner_text}")
    runs = workflows.get("runs", [])
    for run in runs if isinstance(runs, list) else []:
        run_summary = _mapping(run)
        print(
            "  - "
            f"{run_summary.get('workflow_name', '<unknown>')}: "
            f"{run_summary.get('status', 'incomplete')} "
            f"({_integer(run_summary.get('phases_run'))} phases)"
        )

    resume = _mapping(summary.get("resume"))
    if resume.get("incomplete"):
        print(
            "Resume: required "
            f"(turn {resume.get('turn_id', '<unknown>')}, "
            f"{_integer(resume.get('tool_records'))} recorded tool results)"
        )
    else:
        print("Resume: clean")
    print(f"Redactions: {_integer(summary.get('redactions'))} detected")
