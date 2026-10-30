"""Tabs, rows, groups and search for the agents hub (AGX-05, AGX-06).

Everything here is a pure function of records already read from the stores,
so grouping, ordering, tree nesting and search are tested without a terminal.
A row's ``key`` is its stable identity: the hub keeps the selection on the
same key when an update moves the row to another group.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import TYPE_CHECKING

from agenthicc.background.inbox import BackgroundInboxItem
from agenthicc.background.index import SessionFilter
from agenthicc.background.model import BackgroundSession, SessionStatus
from agenthicc.background.vocabulary import Group, status_look

if TYPE_CHECKING:
    from agenthicc.agents.definitions import AgentSpec
    from agenthicc.subagents.pool import SubagentPoolState, WorkerState

#: The Running tab's group of the session's subagent workers (in-session, AGX-08).
SUBAGENTS_GROUP = "This turn's subagents"

__all__ = [
    "RECENT_WINDOW_S",
    "SUBAGENTS_GROUP",
    "GroupBy",
    "HubRow",
    "LibraryEntry",
    "RowKind",
    "SavedSession",
    "ScheduledEntry",
    "Search",
    "SortKey",
    "Tab",
    "highlight_spans",
    "history_rows",
    "library_rows",
    "needs_you_rows",
    "parse_search",
    "project_name",
    "running_rows",
]

#: Finished agents stay in the Running tab this long (AGX-06 §5.3).
RECENT_WINDOW_S = 30 * 60

#: Statuses that always belong to the Running tab.
RUNNING_TAB_STATUSES = frozenset(
    {
        SessionStatus.QUEUED,
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.RETRYING,
        SessionStatus.CANCELLING,
        SessionStatus.WAITING_APPROVAL,
        SessionStatus.WAITING_INPUT,
        SessionStatus.ORPHANED,
    }
)
#: Finished statuses: shown in Running for a while, then in History.
FINISHED_STATUSES = frozenset(
    {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
)
HISTORY_STATUSES = FINISHED_STATUSES | {SessionStatus.ARCHIVED}
NEEDS_YOU_STATUSES = frozenset(
    {SessionStatus.WAITING_APPROVAL, SessionStatus.WAITING_INPUT, SessionStatus.ORPHANED}
)


class Tab(enum.Enum):
    """The hub's tabs; the value is the ``--tab`` name."""

    RUNNING = "running"
    NEEDS_YOU = "needs-you"
    HISTORY = "history"
    LIBRARY = "library"

    @property
    def label(self) -> str:
        return _TAB_LABELS[self]


_TAB_LABELS = {
    Tab.RUNNING: "Running",
    Tab.NEEDS_YOU: "Needs you",
    Tab.HISTORY: "History",
    Tab.LIBRARY: "Library",
}


class SortKey(enum.Enum):
    """Row order within a group; ``s`` cycles through them (AGX-06 §5.3)."""

    ATTENTION = "attention"
    RECENT = "recent"
    CREATED = "created"
    TITLE = "title"

    @property
    def label(self) -> str:
        return {
            SortKey.ATTENTION: "attention",
            SortKey.RECENT: "recent activity",
            SortKey.CREATED: "created",
            SortKey.TITLE: "title",
        }[self]

    def next(self) -> SortKey:
        members = list(SortKey)
        return members[(members.index(self) + 1) % len(members)]


class GroupBy(enum.Enum):
    """How rows are grouped; ``g`` toggles (AGX-06 §5.3)."""

    STATUS = "status"
    PROJECT = "project"

    def next(self) -> GroupBy:
        return GroupBy.PROJECT if self is GroupBy.STATUS else GroupBy.STATUS


class RowKind(enum.Enum):
    HEADER = "header"
    NOTE = "note"
    SESSION = "session"
    INBOX = "inbox"
    SAVED = "saved"
    DEFINITION = "definition"
    SCHEDULED = "scheduled"
    #: A subagent worker of the session's turn (in-session only, AGX-08).
    SUBAGENT = "subagent"


@dataclass(frozen=True, slots=True)
class SavedSession:
    """A saved foreground session (the old ``sessions list`` selector)."""

    session_id: str
    cwd: str = ""
    model: str = ""
    last_used: float = 0.0
    title: str = ""


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One agent definition in the Library tab."""

    name: str
    source: str
    description: str = ""
    tools: str = ""
    mode: str = ""
    model: str = ""
    state: str = "active"
    note: str = ""
    path: str = ""
    spec: AgentSpec | None = None
    default: bool = False


@dataclass(frozen=True, slots=True)
class ScheduledEntry:
    """The next occurrence of one enabled schedule."""

    schedule_id: str
    name: str
    project: str
    next_run_at: float


@dataclass(frozen=True, slots=True)
class HubRow:
    """One line of a hub list."""

    kind: RowKind
    key: str
    #: The group this row is listed under (a header's own title).
    group: str = ""
    depth: int = 0
    session: BackgroundSession | None = None
    item: BackgroundInboxItem | None = None
    saved: SavedSession | None = None
    entry: LibraryEntry | None = None
    scheduled: ScheduledEntry | None = None
    worker: WorkerState | None = None
    #: The pool a subagent row belongs to.
    pool_id: str = ""
    #: Descendants hidden while this tree row is collapsed (``(+N)``).
    hidden: int = 0
    #: A header's title or a note's text.
    text: str = ""

    @property
    def selectable(self) -> bool:
        return self.kind not in (RowKind.HEADER, RowKind.NOTE)

    @property
    def session_id(self) -> str | None:
        """The background session this row acts on, if any."""
        if self.session is not None:
            return self.session.session_id
        if self.item is not None:
            return self.item.session_id
        return None


def header(title: str) -> HubRow:
    return HubRow(RowKind.HEADER, f"header:{title}", group=title, text=title)


def note(group: str, text: str) -> HubRow:
    return HubRow(RowKind.NOTE, f"note:{group}:{text}", group=group, text=text)


def project_name(cwd: str) -> str:
    """The last component of a working directory, as the list shows it."""
    if not cwd:
        return ""
    # Either separator: a record may come from another platform.
    path = PurePath(cwd.replace("\\", "/"))
    return path.name or cwd


def _session_row(session: BackgroundSession, group: str, depth: int = 0, hidden: int = 0) -> HubRow:
    return HubRow(
        RowKind.SESSION,
        f"session:{session.session_id}",
        group=group,
        depth=depth,
        session=session,
        hidden=hidden,
    )


def ready_for_review(session: BackgroundSession) -> bool:
    """A finished agent whose worktree branch holds changes to review."""
    return session.status is SessionStatus.COMPLETED and bool(session.branch or session.worktree_id)


def running_group(session: BackgroundSession) -> Group:
    """The Running tab group a session belongs to."""
    if session.pinned:
        return Group.PINNED
    if ready_for_review(session):
        return Group.READY_FOR_REVIEW
    return status_look(session.status).group


def _sort(
    sessions: Iterable[BackgroundSession], sort: SortKey, *, oldest_first: bool = False
) -> list[BackgroundSession]:
    records = list(sessions)
    if sort is SortKey.TITLE:
        return sorted(records, key=lambda s: (s.title.casefold(), -s.last_active, s.session_id))
    if sort is SortKey.CREATED:
        return sorted(records, key=lambda s: (-s.created_at, s.session_id))
    if sort is SortKey.ATTENTION and oldest_first:
        # Whoever has waited longest comes first.
        return sorted(records, key=lambda s: (s.state_changed_at or s.last_active, s.session_id))
    return sorted(records, key=lambda s: (-s.last_active, s.session_id))


_RUNNING_GROUPS = (
    Group.PINNED,
    Group.NEEDS_YOU,
    Group.READY_FOR_REVIEW,
    Group.RUNNING,
    Group.QUEUED,
    Group.RECENTLY_FINISHED,
)


def running_rows(
    sessions: Sequence[BackgroundSession],
    *,
    sort: SortKey = SortKey.ATTENTION,
    group_by: GroupBy = GroupBy.STATUS,
    collapsed: frozenset[str] = frozenset(),
    scheduled: Sequence[ScheduledEntry] = (),
    show_empty_groups: bool = False,
    pin_order: Sequence[str] = (),
    subagents: SubagentPoolState | None = None,
) -> list[HubRow]:
    """Rows of the Running tab: grouped, ordered and nested (AGX-06 §5.3).

    A worker or subagent session is listed under its parent when the parent
    is in the tab, unless it needs the user: attention is never buried in a
    collapsed tree. ``collapsed`` holds the parents whose children are hidden.
    Pinned agents keep the user's order (``pin_order``, Shift+↑/↓) under the
    attention sort. In a session, ``subagents`` (the turn's pool) adds a group
    of its workers (AGX-08).
    """
    by_id = {session.session_id: session for session in sessions}
    children: dict[str, list[BackgroundSession]] = {}
    top: list[BackgroundSession] = []
    for session in sessions:
        parent = session.parent_session_id
        if (
            parent
            and parent in by_id
            and parent != session.session_id
            and session.status not in NEEDS_YOU_STATUSES
        ):
            children.setdefault(parent, []).append(session)
        else:
            top.append(session)

    def descendants(session_id: str, seen: frozenset[str]) -> int:
        return sum(
            1 + descendants(child.session_id, seen | {child.session_id})
            for child in children.get(session_id, ())
            if child.session_id not in seen
        )

    def tree(
        session: BackgroundSession, group: str, depth: int, seen: frozenset[str]
    ) -> list[HubRow]:
        if session.session_id in collapsed:
            hidden = descendants(session.session_id, seen)
            return [_session_row(session, group, depth, hidden)]
        rows = [_session_row(session, group, depth)]
        for child in _sort(children.get(session.session_id, ()), sort):
            if child.session_id not in seen:
                rows.extend(tree(child, group, depth + 1, seen | {child.session_id}))
        return rows

    rows: list[HubRow] = []
    if group_by is GroupBy.PROJECT:
        projects: dict[str, list[BackgroundSession]] = {}
        for session in top:
            projects.setdefault(project_name(session.cwd) or "—", []).append(session)
        for name in sorted(projects, key=str.casefold):
            rows.append(header(name))
            for session in _sort(projects[name], sort):
                rows.extend(tree(session, name, 0, frozenset({session.session_id})))
    else:
        grouped: dict[Group, list[BackgroundSession]] = {}
        for session in top:
            grouped.setdefault(running_group(session), []).append(session)
        for group in _RUNNING_GROUPS:
            members = grouped.get(group, [])
            if not members:
                if show_empty_groups:
                    rows.append(header(group.value))
                    rows.append(note(group.value, group.empty_hint))
                continue
            rows.append(header(group.value))
            ordered = _sort(members, sort, oldest_first=group is Group.NEEDS_YOU)
            if group is Group.PINNED and sort is SortKey.ATTENTION and pin_order:
                rank = {session_id: index for index, session_id in enumerate(pin_order)}
                ordered.sort(key=lambda s: rank.get(s.session_id, len(rank)))
            for session in ordered:
                rows.extend(tree(session, group.value, 0, frozenset({session.session_id})))
    workers = tuple(w for w in subagents.workers if not w.dismissed) if subagents else ()
    if subagents is not None and workers:
        rows.append(header(SUBAGENTS_GROUP))
        rows.extend(
            HubRow(
                RowKind.SUBAGENT,
                f"subagent:{subagents.pool_id}:{worker.worker_id}",
                group=SUBAGENTS_GROUP,
                worker=worker,
                pool_id=subagents.pool_id,
            )
            for worker in workers
        )
    if scheduled:
        rows.append(header("Scheduled"))
        for entry in sorted(scheduled, key=lambda e: (e.next_run_at, e.schedule_id)):
            rows.append(
                HubRow(
                    RowKind.SCHEDULED,
                    f"scheduled:{entry.schedule_id}",
                    group="Scheduled",
                    scheduled=entry,
                )
            )
    return rows


def needs_you_rows(
    items: Sequence[BackgroundInboxItem], sessions: Sequence[BackgroundSession]
) -> list[HubRow]:
    """Rows of the Needs you tab, oldest first (AGX-05 §5.1).

    Every pending inbox item is a row; a waiting or orphaned session without
    one (an approval recorded before the inbox, or a lost worker) is a row too.
    """
    covered = {item.session_id for item in items}
    entries: list[tuple[float, str, HubRow]] = []
    for item in items:
        entries.append(
            (
                item.created_at,
                item.item_id,
                HubRow(
                    RowKind.INBOX, f"inbox:{item.item_id}", group=Group.NEEDS_YOU.value, item=item
                ),
            )
        )
    for session in sessions:
        if session.status in NEEDS_YOU_STATUSES and session.session_id not in covered:
            entries.append(
                (
                    session.state_changed_at or session.last_active,
                    session.session_id,
                    _session_row(session, Group.NEEDS_YOU.value),
                )
            )
    return [row for _when, _key, row in sorted(entries, key=lambda e: (e[0], e[1]))]


def history_rows(
    sessions: Sequence[BackgroundSession],
    saved: Sequence[SavedSession],
    *,
    group_by: GroupBy = GroupBy.STATUS,
) -> list[HubRow]:
    """Rows of the History tab: finished agents and saved sessions, newest first.

    Both inputs must already be newest first; they are merged by recency.
    Grouping by project lists projects alphabetically.
    """
    merged: list[tuple[float, str, HubRow]] = [
        (session.last_active, session.session_id, _session_row(session, Group.HISTORY.value))
        for session in sessions
    ]
    merged.extend(
        (
            record.last_used,
            record.session_id,
            HubRow(RowKind.SAVED, f"saved:{record.session_id}", group="Saved", saved=record),
        )
        for record in saved
    )
    merged.sort(key=lambda entry: (-entry[0], entry[1]))
    rows = [row for _when, _key, row in merged]
    if group_by is not GroupBy.PROJECT:
        return rows
    projects: dict[str, list[HubRow]] = {}
    for row in rows:
        cwd = row.session.cwd if row.session is not None else row.saved.cwd if row.saved else ""
        projects.setdefault(project_name(cwd) or "—", []).append(row)
    grouped: list[HubRow] = []
    for name in sorted(projects, key=str.casefold):
        grouped.append(header(name))
        grouped.extend(
            HubRow(
                row.kind,
                row.key,
                group=name,
                session=row.session,
                saved=row.saved,
            )
            for row in projects[name]
        )
    return grouped


_SOURCE_ORDER = ("project", "user", "plugin", "builtin")
_SOURCE_TITLES = {"project": "Project", "user": "User", "plugin": "Plugin", "builtin": "Built-in"}


def library_rows(entries: Sequence[LibraryEntry]) -> list[HubRow]:
    """Rows of the Library tab: by source, then name (AGX-05 §5.1)."""
    rows: list[HubRow] = []
    by_source: dict[str, list[LibraryEntry]] = {}
    for entry in entries:
        by_source.setdefault(entry.source, []).append(entry)
    order = [*_SOURCE_ORDER, *sorted(set(by_source) - set(_SOURCE_ORDER))]
    for source in order:
        members = by_source.get(source)
        if not members:
            continue
        title = _SOURCE_TITLES.get(source, source.title())
        rows.append(header(title))
        rows.extend(
            HubRow(
                RowKind.DEFINITION,
                f"definition:{entry.source}:{entry.name}",
                group=title,
                entry=entry,
            )
            for entry in sorted(members, key=lambda e: e.name.casefold())
        )
    return rows


# ── search ────────────────────────────────────────────────────────────────────

_STATUS_WORDS: dict[str, frozenset[SessionStatus]] = {}
for _status in SessionStatus:
    _STATUS_WORDS[_status.value] = frozenset({_status})
    _STATUS_WORDS[_status.value.replace("_", "-")] = frozenset({_status})
for _status in SessionStatus:
    if _status is SessionStatus.DELETED:
        continue
    _label = status_look(_status).label.replace(" ", "-")
    _STATUS_WORDS[_label] = _STATUS_WORDS.get(_label, frozenset()) | {_status}
_STATUS_WORDS["needs-you"] = NEEDS_YOU_STATUSES
_STATUS_WORDS["active"] = RUNNING_TAB_STATUSES - NEEDS_YOU_STATUSES
_STATUS_WORDS["finished"] = FINISHED_STATUSES

_TOKENS = ("status", "project", "agent", "label", "run")


@dataclass(frozen=True, slots=True)
class Search:
    """A parsed search line: words that must all match, plus token filters."""

    text: str = ""
    words: tuple[str, ...] = ()
    statuses: frozenset[SessionStatus] | None = None
    project: str | None = None
    agent: str | None = None
    labels: frozenset[str] = frozenset()
    run_id: str | None = None
    errors: tuple[str, ...] = field(default=())

    @property
    def active(self) -> bool:
        return bool(
            self.words
            or self.statuses is not None
            or self.project
            or self.agent
            or self.labels
            or self.run_id
        )

    @property
    def has_tokens(self) -> bool:
        return bool(self.statuses is not None or self.agent or self.labels or self.run_id)

    def session_filter(self, base: SessionFilter) -> SessionFilter:
        """Narrow *base* by this search."""
        statuses = base.statuses
        if self.statuses is not None:
            statuses = self.statuses if statuses is None else statuses & self.statuses
        return SessionFilter(
            include_archived=base.include_archived,
            include_deleted=base.include_deleted,
            cwd=base.cwd,
            workflow_name=base.workflow_name,
            query=" ".join(self.words),
            status=base.status,
            statuses=statuses,
            idempotency_key=base.idempotency_key,
            project=self.project if self.project is not None else base.project,
            agent=self.agent if self.agent is not None else base.agent,
            run_id=self.run_id if self.run_id is not None else base.run_id,
            labels=base.labels | self.labels,
            active_since=base.active_since,
            pinned=base.pinned,
        )

    def matches_text(self, *values: str) -> bool:
        """Every word occurs in one of *values* (case-insensitively)."""
        haystack = " ".join(values).casefold()
        return all(word in haystack for word in self.words)

    def matches_session(self, session: BackgroundSession) -> bool:
        """The in-memory form of :meth:`session_filter`, for loaded rows."""
        if self.statuses is not None and session.status not in self.statuses:
            return False
        if self.project is not None and not _project_matches(session.cwd, self.project):
            return False
        if self.agent is not None and self.agent not in (session.workflow_name, session.role):
            return False
        if self.run_id is not None and session.run_id != self.run_id:
            return False
        if not self.labels <= set(session.labels):
            return False
        return self.matches_text(
            session.session_id,
            session.title,
            session.intent,
            " ".join(session.labels),
            session.latest_activity,
            session.cwd,
            session.workflow_name,
        )

    def matches_saved(self, record: SavedSession) -> bool:
        if self.has_tokens:
            return False  # saved sessions have no status, agent, labels or run
        if self.project is not None and not _project_matches(record.cwd, self.project):
            return False
        return self.matches_text(record.session_id, record.title, record.cwd, record.model)

    def matches_entry(self, entry: LibraryEntry) -> bool:
        if self.has_tokens or self.project is not None:
            return False
        return self.matches_text(entry.name, entry.source, entry.description, entry.tools)


def _project_matches(cwd: str, project: str) -> bool:
    return cwd == project or project_name(cwd) == project


def parse_search(text: str) -> Search:
    """Parse a search line (AGX-06 §5.4).

    Plain words must all match, in any order. ``status:``, ``project:``,
    ``agent:``, ``label:`` and ``run:`` narrow further; an unknown status is
    reported rather than silently matching nothing.
    """
    words: list[str] = []
    statuses: frozenset[SessionStatus] | None = None
    project: str | None = None
    agent: str | None = None
    labels: set[str] = set()
    run_id: str | None = None
    errors: list[str] = []
    for part in text.split():
        name, separator, value = part.partition(":")
        token = name.casefold()
        if not separator or token not in _TOKENS:
            words.append(part.casefold())
            continue
        if not value:
            errors.append(f"{token}: needs a value")
            continue
        if token == "status":
            matched = _STATUS_WORDS.get(value.casefold())
            if matched is None:
                errors.append(f"unknown status {value!r}")
                continue
            statuses = matched if statuses is None else statuses | matched
        elif token == "project":
            project = value
        elif token == "agent":
            agent = value
        elif token == "label":
            labels.add(value)
        else:
            run_id = value
    return Search(
        text=text,
        words=tuple(words),
        statuses=statuses,
        project=project,
        agent=agent,
        labels=frozenset(labels),
        run_id=run_id,
        errors=tuple(errors),
    )


def highlight_spans(text: str, words: Sequence[str]) -> list[tuple[int, int]]:
    """Character spans of *text* that match any search word, merged."""
    folded = text.casefold()
    spans: list[tuple[int, int]] = []
    # casefold() may change a string's length (ß → ss); only highlight when the
    # positions still line up.
    if len(folded) != len(text):
        return spans
    for word in words:
        if not word:
            continue
        start = folded.find(word)
        while start != -1:
            spans.append((start, start + len(word)))
            start = folded.find(word, start + 1)
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
