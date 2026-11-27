"""Records the hub lists besides background sessions (AGX-05 §5.4).

Saved foreground sessions (History), agent definitions (Library) and the next
scheduled occurrences (Running) come from their own stores. Each loader is a
blocking call the hub runs off the event loop; :class:`HubSources` bundles
them so tests substitute fixed lists for the user's real stores.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.tui.workspace.agents_hub.model import (
    LibraryEntry,
    SavedSession,
    ScheduledEntry,
    project_name,
)

if TYPE_CHECKING:
    from agenthicc.agents.definitions import AgentSpec
    from agenthicc.agents.registry import AgentsRegistry

__all__ = [
    "INTERACTIVE_SOURCE",
    "HubSources",
    "library_entries",
    "load_interactive",
    "load_library",
    "load_saved_sessions",
    "load_scheduled",
]

#: Upcoming schedule occurrences listed at the end of the Running tab.
SCHEDULED_LIMIT = 5


def _none_saved() -> list[SavedSession]:
    return []


def _none_library() -> list[LibraryEntry]:
    return []


def _none_scheduled() -> list[ScheduledEntry]:
    return []


def _none_interactive() -> list[BackgroundSession]:
    return []


@dataclass(frozen=True)
class HubSources:
    """The blocking loaders for saved sessions, definitions and schedules."""

    saved: Callable[[], list[SavedSession]] = field(default=_none_saved)
    library: Callable[[], list[LibraryEntry]] = field(default=_none_library)
    scheduled: Callable[[], list[ScheduledEntry]] = field(default=_none_scheduled)
    interactive: Callable[[], list[BackgroundSession]] = field(default=_none_interactive)

    @classmethod
    def default(cls, cwd: Path) -> HubSources:
        """The user's real stores, with project definitions from *cwd*."""

        def library() -> list[LibraryEntry]:
            return load_library(cwd / ".agenthicc", Path.home() / ".agenthicc")

        return cls(
            saved=load_saved_sessions,
            library=library,
            scheduled=load_scheduled,
            interactive=load_interactive,
        )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


#: The last saved-session list and the index files' identities it was read
#: from. The user-wide index can hold thousands of sessions, and a session
#: reads it each time the hub opens.
_saved_cache: tuple[tuple[object, ...], list[SavedSession]] | None = None


def _file_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def load_saved_sessions() -> list[SavedSession]:
    """Saved foreground sessions, most recently used first."""
    global _saved_cache
    from agenthicc.sessions import (
        _CANONICAL_SESSION_INDEX,
        _SESSION_INDEX,
        _ordered_session_records,
    )

    key = (
        str(Path.cwd()),
        _file_identity(_SESSION_INDEX),
        _file_identity(_CANONICAL_SESSION_INDEX),
    )
    cached = _saved_cache
    if cached is not None and cached[0] == key:
        return list(cached[1])
    records: list[SavedSession] = []
    for session_id, data in _ordered_session_records():
        records.append(
            SavedSession(
                session_id=session_id,
                cwd=_text(data.get("cwd")),
                model=_text(data.get("model")),
                last_used=_number(data.get("last_used", data.get("last_active"))),
                title=_text(data.get("title")),
            )
        )
    _saved_cache = (key, records)
    return list(records)


#: ``BackgroundSession.source`` of a hosted interactive session the hub found
#: running before it was ever detached. It has no background record.
INTERACTIVE_SOURCE = "interactive"
#: Bytes of a session's journal read to find what the operator first asked.
_TITLE_READ_BYTES = 64 * 1024
_TITLE_CHARS = 80


def _first_request(journal: Path) -> str:
    """The operator's first message in a session, for its row's title."""
    import json

    try:
        with journal.open("rb") as handle:
            head = handle.read(_TITLE_READ_BYTES)
    except OSError:
        return ""
    for raw in head.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("kind") != "user_message":
            continue
        payload = event.get("payload")
        text = payload.get("text") if isinstance(payload, dict) else None
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())[:_TITLE_CHARS]
    return ""


def load_interactive() -> list[BackgroundSession]:
    """Hosted interactive sessions running now, as agents that are open in a terminal.

    The session has no background record before it is detached, so the hub
    would otherwise know it only as a saved session: a static row that reads
    as finished while the agent works, or idles for its subagents. The row's
    journal is the session's own, so its transcript follows the live agent.
    """
    from agenthicc.runners.session_lease import discover_interactive_runtimes

    sessions: list[BackgroundSession] = []
    for live in discover_interactive_runtimes():
        journal = live.session_dir / "conversation.jsonl"
        title = _first_request(journal) or "Interactive session"
        try:
            last_active = journal.stat().st_mtime
        except OSError:
            last_active = live.started_at
        record = BackgroundSession.create(
            live.session_id,
            title=title,
            cwd=live.cwd,
            workflow_name="",
            intent=title,
            artifact_dir=str(live.session_dir),
            now=live.started_at,
        )
        sessions.append(
            record.evolve(
                status=SessionStatus.ATTACHED,
                source=INTERACTIVE_SOURCE,
                worker_pid=live.pid,
                started_at=live.started_at,
                last_active=max(last_active, live.started_at),
                latest_activity="Open in a terminal",
            )
        )
    return sessions


def _tools_summary(spec: AgentSpec) -> str:
    if spec.tools is not None:
        return " ".join(spec.tools) if spec.tools else "no tools"
    if spec.capabilities is not None:
        return " ".join(spec.capabilities) if spec.capabilities else "no tools"
    return "all tools"


def library_entries(registry: AgentsRegistry, *, project_root: Path) -> list[LibraryEntry]:
    """The registry's catalog as Library rows (AGX-09 §5.2)."""
    from agenthicc.agents.selection import project_default

    default = project_default(project_root) or "default"
    entries: list[LibraryEntry] = []
    for item in registry.catalog:
        spec = item.spec
        if item.state == "shadowed":
            note = f"shadowed by the {item.shadowed_by} definition"
        elif item.state == "invalid" and item.error is not None:
            note = f"invalid: {item.error.message} (line {item.error.line})"
            if item.kept_previous:
                note += "; the last valid version is in use"
        elif item.state == "untrusted":
            note = "untrusted: review it with agenthicc trust add"
        else:
            note = ""
        entries.append(
            LibraryEntry(
                name=item.name,
                source=item.source,
                description=spec.description[:200] if spec is not None else "",
                tools=_tools_summary(spec) if spec is not None else "",
                mode=spec.mode if spec is not None else "",
                model=spec.model if spec is not None else "",
                state=item.state,
                note=note,
                path=str(item.path) if item.path is not None else "",
                default=item.state == "active" and item.name == default,
                spec=spec,
            )
        )
    return entries


def load_library(project_dir: Path, user_dir: Path) -> list[LibraryEntry]:
    """Every agent definition with its state; untrusted project files are not loaded."""
    from agenthicc.agents.registry import build_agents_registry

    registry = build_agents_registry(project_dir, user_dir, load_external=True)
    return library_entries(registry, project_root=project_dir.parent)


def load_scheduled(limit: int = SCHEDULED_LIMIT) -> list[ScheduledEntry]:
    """The next occurrence of each enabled schedule, soonest first."""
    from agenthicc.schedules.store import ScheduleStore, ScheduleStoreError

    try:
        schedules = ScheduleStore().list()
    except (OSError, ScheduleStoreError):
        return []
    upcoming = sorted(
        (item for item in schedules if item.enabled and item.next_run_at is not None),
        key=lambda item: (item.next_run_at or 0.0, item.schedule_id),
    )
    entries: list[ScheduledEntry] = []
    for item in upcoming[:limit]:
        prompt = next((line.strip() for line in item.prompt.splitlines() if line.strip()), "")
        name = f"{item.workflow_name}: {prompt}" if item.workflow_name else prompt
        entries.append(
            ScheduledEntry(
                schedule_id=item.schedule_id,
                name=name[:120],
                project=project_name(item.repository_root),
                next_run_at=item.next_run_at or 0.0,
            )
        )
    return entries
