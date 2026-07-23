"""Session management commands — list, show, inspect, and export sessions."""

from __future__ import annotations

from collections.abc import Mapping

from agenthicc.cli.context import CLIContext
from agenthicc.cli.registry import command, group


@group("sessions", help="Manage saved sessions")
def _() -> None: ...


@command("sessions", "list", help="List all sessions for the current directory")
def sessions_list(ctx: CLIContext) -> None:
    """List saved sessions, most recent first. Sessions for the current directory are marked with *."""
    from agenthicc.sessions import _do_sessions  # noqa: PLC0415

    _do_sessions()


@command("sessions", "show", help="Show detail for one session")
def sessions_show(ctx: CLIContext, session_id: str) -> None:
    """Print stored events for SESSION_ID."""
    import json  # noqa: PLC0415
    from agenthicc.sessions import _get_session_log_path  # noqa: PLC0415

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


@command("sessions", "export", help="Export one session as a redacted JSON document")
def sessions_export(ctx: CLIContext, session_id: str, output: str = "") -> None:
    """Export SESSION_ID and its durable artifacts to OUTPUT or SESSION_ID.json."""
    from pathlib import Path  # noqa: PLC0415

    from agenthicc.tui.runtime.session_export import export_session  # noqa: PLC0415

    destination = Path(output) if output else Path(f"{session_id}.json")
    try:
        exported = export_session(session_id, destination)
    except (FileNotFoundError, ValueError, IsADirectoryError) as exc:
        print(str(exc))
        return
    print(f"Exported session {session_id} to {exported}")


@command("sessions", "inspect", help="Inspect one session's durable state")
def sessions_inspect(ctx: CLIContext, session_id: str, json: bool = False) -> None:
    """Summarize SESSION_ID, including artifacts, usage, workflows, and resume state."""
    import json as json_module  # noqa: PLC0415

    from agenthicc.tui.runtime.session_export import inspect_session  # noqa: PLC0415

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
    print(
        "Tokens: "
        f"{_integer(tokens.get('input'))} input, "
        f"{_integer(tokens.get('output'))} output, "
        f"${_number(tokens.get('cost_usd')):.4f}"
    )

    workflows = _mapping(summary.get("workflows"))
    print(
        "Workflows: "
        f"{_integer(workflows.get('total'))} total, "
        f"{_integer(workflows.get('complete'))} complete, "
        f"{_integer(workflows.get('failed'))} failed, "
        f"{_integer(workflows.get('incomplete'))} incomplete"
    )
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
