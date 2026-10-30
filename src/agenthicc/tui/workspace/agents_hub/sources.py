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

from agenthicc.tui.workspace.agents_hub.model import (
    LibraryEntry,
    SavedSession,
    ScheduledEntry,
    project_name,
)

__all__ = [
    "HubSources",
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


@dataclass(frozen=True)
class HubSources:
    """The blocking loaders for saved sessions, definitions and schedules."""

    saved: Callable[[], list[SavedSession]] = field(default=_none_saved)
    library: Callable[[], list[LibraryEntry]] = field(default=_none_library)
    scheduled: Callable[[], list[ScheduledEntry]] = field(default=_none_scheduled)

    @classmethod
    def default(cls, cwd: Path) -> HubSources:
        """The user's real stores, with project definitions from *cwd*."""

        def library() -> list[LibraryEntry]:
            return load_library(cwd / ".agenthicc", Path.home() / ".agenthicc")

        return cls(saved=load_saved_sessions, library=library, scheduled=load_scheduled)


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


def load_library(project_dir: Path, user_dir: Path) -> list[LibraryEntry]:
    """Every catalog candidate, including definitions that need attention."""
    from agenthicc.agents.registry import build_agents_registry
    from agenthicc.agents.selection import project_default

    registry = build_agents_registry(project_dir, user_dir, load_external=True)
    default_name = project_default(project_dir.parent)
    entries: list[LibraryEntry] = []
    for candidate in registry.catalog:
        spec = candidate.spec
        if spec is None or spec.tools is None and spec.capabilities is None:
            tools = "all tools"
        elif spec.tools is not None:
            tools = " ".join(spec.tools) or "no tools"
        else:
            tools = " ".join(spec.capabilities or ()) or "no tools"

        if candidate.error is not None:
            note = str(candidate.error)
        elif candidate.state == "shadowed":
            note = f"shadowed by {candidate.shadowed_by}"
        elif candidate.state == "untrusted":
            note = "requires explicit trust before use"
        elif candidate.kept_previous:
            note = "previous valid version remains active"
        else:
            note = ""

        entries.append(
            LibraryEntry(
                name=candidate.name,
                source=candidate.source,
                description=spec.description if spec is not None else "",
                tools=tools,
                mode=spec.mode if spec is not None else "",
                model=spec.model if spec is not None else "",
                state=candidate.state,
                note=note,
                path=str(candidate.path) if candidate.path is not None else "",
                spec=spec,
                default=(
                    candidate.state == "active" and spec is not None and spec.name == default_name
                ),
            )
        )
    return entries


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
