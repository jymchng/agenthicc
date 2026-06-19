"""Session management commands — sessions list, sessions show."""
from __future__ import annotations

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
