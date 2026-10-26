"""The agents hub's pure model: groups, trees, tabs and search (AGX-05, AGX-06)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenthicc.background import BackgroundSession, SessionFilter, SessionStatus
from agenthicc.background.inbox import BackgroundInboxItem
from agenthicc.tui.workspace.agents_hub.layout import COLUMNS, GUTTER, SEPARATOR, layout
from agenthicc.tui.workspace.agents_hub.model import (
    GroupBy,
    LibraryEntry,
    RowKind,
    SavedSession,
    ScheduledEntry,
    SortKey,
    highlight_spans,
    history_rows,
    library_rows,
    needs_you_rows,
    parse_search,
    project_name,
    running_rows,
)

pytestmark = pytest.mark.unit


def _session(
    session_id: str, status: SessionStatus, last_active: float, **changes: object
) -> BackgroundSession:
    session = BackgroundSession.create(
        session_id,
        title=f"title {session_id}",
        cwd="/work/app-0",
        workflow_name="code_plan",
        intent=f"intent {session_id}",
        now=last_active,
    )
    return session.evolve(status=status, last_active=last_active, **changes)


def _layout(rows: list[object]) -> list[str]:
    out: list[str] = []
    for row in rows:
        assert hasattr(row, "kind")
        if row.kind is RowKind.HEADER:  # type: ignore[attr-defined]
            out.append(f"# {row.text}")  # type: ignore[attr-defined]
        elif row.kind is RowKind.NOTE:  # type: ignore[attr-defined]
            out.append(f"  ({row.text})")  # type: ignore[attr-defined]
        else:
            out.append("  " * row.depth + row.key)  # type: ignore[attr-defined]
    return out


def test_running_rows_are_grouped_in_attention_order() -> None:
    rows = running_rows(
        [
            _session("run", SessionStatus.RUNNING, 50.0),
            _session("queue", SessionStatus.QUEUED, 60.0),
            _session("ask", SessionStatus.WAITING_INPUT, 40.0),
            _session("done", SessionStatus.COMPLETED, 70.0),
            _session("review", SessionStatus.COMPLETED, 65.0, branch="agx/review"),
            _session("pin", SessionStatus.FAILED, 10.0, pinned=True),
            _session("lost", SessionStatus.ORPHANED, 30.0),
        ]
    )
    assert _layout(rows) == [
        "# Pinned",
        "session:pin",
        "# Needs you",
        "session:lost",  # waited longest first
        "session:ask",
        "# Ready for review",
        "session:review",
        "# Running",
        "session:run",
        "# Queued",
        "session:queue",
        "# Recently finished",
        "session:done",
    ]


def test_an_empty_running_tab_describes_every_group() -> None:
    rows = running_rows([], show_empty_groups=True)
    assert [row.text for row in rows if row.kind is RowKind.HEADER][:2] == ["Pinned", "Needs you"]
    assert "Agents waiting for your answer appear here" in [row.text for row in rows]
    assert running_rows([]) == []


def test_workers_nest_under_their_run_and_collapse_with_a_count() -> None:
    sessions = [
        _session("main", SessionStatus.RUNNING, 50.0, run_id="r1"),
        _session("w1", SessionStatus.RUNNING, 40.0, parent_session_id="main", run_id="r1"),
        _session("w2", SessionStatus.COMPLETED, 45.0, parent_session_id="main", run_id="r1"),
        _session("w1a", SessionStatus.RUNNING, 30.0, parent_session_id="w1", run_id="r1"),
        # A worker that needs the user is never buried in the tree.
        _session("w3", SessionStatus.WAITING_APPROVAL, 20.0, parent_session_id="main"),
    ]
    assert _layout(running_rows(sessions)) == [
        "# Needs you",
        "session:w3",
        "# Running",
        "session:main",
        "  session:w2",
        "  session:w1",
        "    session:w1a",
    ]
    collapsed = running_rows(sessions, collapsed=frozenset({"main"}))
    main = next(row for row in collapsed if row.key == "session:main")
    assert main.hidden == 3
    assert "session:w1" not in [row.key for row in collapsed]


def test_project_grouping_and_sort_keys() -> None:
    sessions = [
        _session("b", SessionStatus.RUNNING, 20.0, cwd="/x/zeta", title="Beta"),
        _session("a", SessionStatus.RUNNING, 10.0, cwd="/x/alpha", title="alpha"),
        _session("c", SessionStatus.QUEUED, 30.0, cwd="/x/alpha", title="Charlie"),
    ]
    rows = running_rows(sessions, group_by=GroupBy.PROJECT, sort=SortKey.TITLE)
    assert _layout(rows) == ["# alpha", "session:a", "session:c", "# zeta", "session:b"]
    assert SortKey.ATTENTION.next() is SortKey.RECENT
    assert SortKey.TITLE.next() is SortKey.ATTENTION


def test_scheduled_occurrences_follow_the_groups() -> None:
    rows = running_rows(
        [_session("run", SessionStatus.RUNNING, 1.0)],
        scheduled=[
            ScheduledEntry("s2", "nightly", "app", 200.0),
            ScheduledEntry("s1", "hourly", "app", 100.0),
        ],
    )
    assert _layout(rows)[-3:] == ["# Scheduled", "scheduled:s1", "scheduled:s2"]


def _item(item_id: str, session_id: str, created_at: float) -> BackgroundInboxItem:
    return BackgroundInboxItem(
        item_id=item_id,
        session_id=session_id,
        attempt=1,
        request_id=f"req-{item_id}",
        kind="approval",
        created_at=created_at,
        expires_at=None,
        target={"tool_name": "bash"},
        status="pending",
        version=1,
        required_scope="",
        project_root="/work/app-0",
        workspace="app-0",
    )


def test_needs_you_lists_inbox_items_and_uncovered_waits_oldest_first() -> None:
    rows = needs_you_rows(
        [_item("i2", "s2", 20.0), _item("i1", "s1", 5.0)],
        [
            _session("s1", SessionStatus.WAITING_APPROVAL, 5.0),
            _session("s3", SessionStatus.ORPHANED, 10.0),
            _session("s4", SessionStatus.RUNNING, 1.0),
        ],
    )
    assert [row.key for row in rows] == ["inbox:i1", "session:s3", "inbox:i2"]
    assert rows[0].session_id == "s1"


def test_history_merges_agents_and_saved_sessions_by_recency() -> None:
    rows = history_rows(
        [_session("b2", SessionStatus.FAILED, 30.0), _session("b1", SessionStatus.COMPLETED, 10.0)],
        [SavedSession("f1", cwd="/x/app", last_used=20.0)],
    )
    assert [row.key for row in rows] == ["session:b2", "saved:f1", "session:b1"]
    by_project = history_rows(
        [_session("b1", SessionStatus.COMPLETED, 10.0, cwd="/x/zz")],
        [SavedSession("f1", cwd="/x/aa", last_used=20.0)],
        group_by=GroupBy.PROJECT,
    )
    assert _layout(by_project) == ["# aa", "saved:f1", "# zz", "session:b1"]


def test_library_lists_definitions_by_source_then_name() -> None:
    rows = library_rows(
        [
            LibraryEntry("planner", "builtin"),
            LibraryEntry("reviewer", "project"),
            LibraryEntry("Auto", "builtin"),
            LibraryEntry("docs", "user"),
        ]
    )
    assert _layout(rows) == [
        "# Project",
        "definition:project:reviewer",
        "# User",
        "definition:user:docs",
        "# Built-in",
        "definition:builtin:Auto",
        "definition:builtin:planner",
    ]


@pytest.mark.parametrize(
    ("text", "statuses"),
    [
        ("status:failed", {SessionStatus.FAILED}),
        ("status:needs-approval", {SessionStatus.WAITING_APPROVAL}),
        ("status:waiting_input", {SessionStatus.WAITING_INPUT}),
        ("status:done", {SessionStatus.COMPLETED}),
        ("status:worker-lost", {SessionStatus.ORPHANED}),
        (
            "status:needs-you",
            {SessionStatus.WAITING_APPROVAL, SessionStatus.WAITING_INPUT, SessionStatus.ORPHANED},
        ),
        ("status:failed status:cancelled", {SessionStatus.FAILED, SessionStatus.CANCELLED}),
    ],
)
def test_status_tokens_accept_states_and_labels(text: str, statuses: set[SessionStatus]) -> None:
    assert parse_search(text).statuses == statuses


def test_search_words_and_tokens_become_one_filter() -> None:
    search = parse_search(
        "Flaky  project:app-0 agent:reviewer label:release run:r1 sched status:bogus x:y"
    )
    assert search.words == ("flaky", "sched", "x:y")
    assert (search.project, search.agent, search.run_id) == ("app-0", "reviewer", "r1")
    assert search.labels == {"release"}
    assert search.errors == ("unknown status 'bogus'",)
    narrowed = search.session_filter(SessionFilter(statuses=frozenset({SessionStatus.FAILED})))
    assert narrowed.query == "flaky sched x:y"
    assert narrowed.project == "app-0" and narrowed.labels == {"release"}
    assert narrowed.statuses == {SessionStatus.FAILED}
    assert not parse_search("").active


def test_loaded_rows_match_the_same_search_in_memory() -> None:
    session = _session(
        "abc", SessionStatus.FAILED, 1.0, title="Fix flaky scheduler", labels=("release",)
    )
    assert parse_search("scheduler fix").matches_session(session)
    assert parse_search("status:failed project:app-0 label:release").matches_session(session)
    assert not parse_search("status:done").matches_session(session)
    assert not parse_search("label:other").matches_session(session)
    saved = SavedSession("f1", cwd="/x/app-0", model="claude")
    assert parse_search("claude project:app-0").matches_saved(saved)
    assert not parse_search("status:failed").matches_saved(saved)
    assert parse_search("plan").matches_entry(LibraryEntry("planner", "builtin"))


def test_matches_are_highlighted() -> None:
    assert highlight_spans("Fix flaky scheduler", ["sched", "fla"]) == [(4, 7), (10, 15)]
    assert highlight_spans("aaaa", ["aa"]) == [(0, 4)]
    assert highlight_spans("Straße", ["ss"]) == []  # positions would not line up


def test_project_names_use_either_separator() -> None:
    assert project_name("/work/app-0") == "app-0"
    assert project_name("C:\\work\\app-1") == "app-1"
    assert project_name("") == ""


# ── responsive columns ───────────────────────────────────────────────────────

_OPTIONAL = ("activity", "project", "agent", "cost")


@settings(deadline=None)  # a loaded CI host must not make a pure property flaky
@given(
    width=st.integers(min_value=GUTTER + 16, max_value=400),
    present=st.frozensets(st.sampled_from(_OPTIONAL)),
)
def test_a_shown_column_is_never_narrower_than_its_minimum(
    width: int, present: frozenset[str]
) -> None:
    """AGX-06 §5.2: a column is shown at or above its minimum width, or dropped."""
    result = layout(width, present=present)
    minimum = {column.name: column.minimum for column in COLUMNS}
    from_width = {column.name: column.from_width for column in COLUMNS}
    assert "status" in result.columns
    for name, value in zip(result.columns, result.widths, strict=True):
        assert value >= minimum[name]
        assert width >= from_width[name]
        assert name in present or name in ("status", "title", "age")
    used = GUTTER + sum(result.widths) + SEPARATOR * (len(result.columns) - 1)
    assert used <= width
    if "title" in result.columns:
        assert used == width  # the flexible columns take the rest of the line
