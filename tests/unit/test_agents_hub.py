"""The agents hub controller: tabs, selection, search, actions and drawing (AGX-05–07)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Literal

import pytest
from rich.console import Console

from agenthicc.agents.definitions import AgentSpec
from agenthicc.background import (
    BackgroundInteractionInbox,
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)
from agenthicc.background.settings import AgentsHubSettings
from agenthicc.tui.cbreak_reader import Key, MouseEvent
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.keymap_service import KeymapService
from agenthicc.tui.workspace.agents_hub import AgentsHub, HubResult, Tab
from agenthicc.tui.workspace.agents_hub.hub import parse_dispatch
from agenthicc.tui.workspace.agents_hub.model import SavedSession
from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs
from agenthicc.tui.workspace.agents_hub.sources import HubSources

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0


def _create(
    store: BackgroundStore,
    session_id: str,
    status: SessionStatus,
    ago: float,
    **changes: object,
) -> BackgroundSession:
    title = str(changes.pop("title", session_id))
    cwd = str(changes.pop("cwd", "/work/app-0"))
    session = BackgroundSession.create(
        session_id,
        title=title,
        cwd=cwd,
        workflow_name=str(changes.pop("workflow_name", "code_plan")),
        intent=f"intent of {session_id}",
        now=NOW - ago,
    )
    record = session.evolve(status=status, last_active=NOW - ago, **changes)
    store.create(record)
    return record


def _text(hub: AgentsHub, width: int = 120, height: int = 30) -> str:
    console = Console(width=width, record=True, color_system=None, force_terminal=False)
    console.print(hub.render(width, height))
    return console.export_text()


@pytest.fixture
def store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(tmp_path / "background")


async def _open(
    store: BackgroundStore,
    tmp_path: Path,
    *,
    tab: Tab = Tab.RUNNING,
    sources: HubSources | None = None,
    app_state: AppState | None = None,
    in_session: bool = False,
    run_id: str = "",
    effective_tools: Callable[[AgentSpec, AppState], tuple[str, ...]] | None = None,
    external_editor: Callable[[Path, Literal["project", "user"]], Awaitable[None]] | None = None,
) -> AgentsHub:
    hub = AgentsHub(
        Console(width=120, color_system=None),
        store=store,
        supervisor=BackgroundSupervisor(store),
        settings=AgentsHubSettings(),
        sources=sources,
        app_state=app_state,
        prefs=HubPrefs(path=tmp_path / "hub-prefs.json"),
        tab=tab,
        cwd=tmp_path,
        in_session=in_session,
        run_id=run_id,
        effective_tools=effective_tools,
        external_editor=external_editor,
        clock=lambda: NOW,
    )
    await hub.start()
    await hub.settle()
    return hub


@pytest.fixture
async def seeded(store: BackgroundStore, tmp_path: Path) -> AsyncIterator[AgentsHub]:
    _create(store, "ask", SessionStatus.WAITING_INPUT, 2_400, title="Investigate memory growth")
    _create(
        store,
        "fix",
        SessionStatus.WAITING_APPROVAL,
        600,
        title="Fix flaky scheduler test",
        approval_request="bash",
    )
    _create(store, "docs", SessionStatus.RUNNING, 120, title="Document the CLI reference")
    _create(store, "deps", SessionStatus.QUEUED, 3_000, title="Upgrade dependencies")
    _create(store, "auth", SessionStatus.COMPLETED, 1_200, title="Refactor auth", branch="agx/auth")
    _create(store, "old", SessionStatus.COMPLETED, 86_400 * 3, title="Old work")
    hub = await _open(store, tmp_path)
    try:
        yield hub
    finally:
        await hub.stop()


# ── tabs, groups and counts ──────────────────────────────────────────────────


async def test_running_tab_groups_agents_and_counts_every_tab(seeded: AgentsHub) -> None:
    keys = [row.key for row in seeded.rows]
    assert keys.index("header:Needs you") < keys.index("session:ask") < keys.index("session:fix")
    assert keys.index("header:Ready for review") < keys.index("session:auth")
    assert "session:old" not in keys  # finished three days ago: History only
    # The shell reads agent definitions when the Library is first shown.
    assert seeded.counts == {Tab.RUNNING: 5, Tab.NEEDS_YOU: 2, Tab.HISTORY: 2}
    text = _text(seeded)
    assert "Running 5 · Needs you 2 · History 2 · Library" in text
    assert "◆ needs input" in text and "◆ needs approval" in text
    seeded.switch_tab(Tab.LIBRARY)
    await seeded.settle()
    assert seeded.counts[Tab.LIBRARY] == 0
    assert "Library 0" in _text(seeded)


async def test_history_lists_finished_agents_and_saved_sessions_by_recency(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _create(store, "recent", SessionStatus.FAILED, 60, error="boom")
    _create(store, "older", SessionStatus.COMPLETED, 7_200)
    saved = [SavedSession("fg-1", cwd="/work/app-0", last_used=NOW - 3_600)]
    hub = await _open(
        store, tmp_path, tab=Tab.HISTORY, sources=HubSources(saved=lambda: list(saved))
    )
    try:
        assert [row.key for row in hub.rows] == ["session:recent", "saved:fg-1", "session:older"]
        hub.handle_key(Key.DOWN, "")
        assert hub.handle_key(Key.ENTER, "") == HubResult("open_saved", "fg-1")
    finally:
        await hub.stop()


async def test_tabs_keep_their_own_selection(seeded: AgentsHub) -> None:
    seeded.handle_key(Key.DOWN, "")
    selected = seeded.selected_row()
    assert selected is not None
    seeded.handle_key(Key.CHAR, "3")
    await seeded.settle()
    assert seeded.tab is Tab.HISTORY
    seeded.handle_key(Key.TAB, "")
    assert seeded.tab is Tab.LIBRARY
    seeded.handle_key(Key.CHAR, "1")
    assert seeded.selected_row() == selected


async def test_selection_follows_the_agent_when_it_changes_group(
    seeded: AgentsHub, store: BackgroundStore
) -> None:
    while (row := seeded.selected_row()) is not None and row.key != "session:docs":
        seeded.handle_key(Key.DOWN, "")
    store.update("docs", latest_activity="Now waiting", status=SessionStatus.WAITING_INPUT)
    seeded._request_load("test", force=True)
    await seeded.settle()
    row = seeded.selected_row()
    assert row is not None and row.key == "session:docs"
    assert row.group == "Needs you"


# ── search ───────────────────────────────────────────────────────────────────


async def test_search_filters_as_you_type_and_esc_restores_the_selection(
    seeded: AgentsHub,
) -> None:
    seeded.handle_key(Key.DOWN, "")
    before = seeded.selected_row()
    seeded.handle_key(Key.CHAR, "/")
    for ch in "scheduler fix":
        seeded.handle_key(Key.CHAR, ch)
    assert [row.key for row in seeded.rows if row.selectable] == ["session:fix"]
    assert "scheduler" in _text(seeded)
    seeded.handle_key(Key.ESC, "")
    assert not seeded.search.active
    assert seeded.selected_row() == before


async def test_history_search_queries_the_index_with_tokens(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _create(store, "a", SessionStatus.FAILED, 60, title="Release notes", labels=("release",))
    _create(store, "b", SessionStatus.COMPLETED, 120, title="Release build")
    hub = await _open(store, tmp_path, tab=Tab.HISTORY)
    try:
        hub.set_search("release status:failed")
        await hub.settle()
        assert [row.key for row in hub.rows] == ["session:a"]
        hub.set_search("label:release")
        await hub.settle()
        assert [row.key for row in hub.rows] == ["session:a"]
        hub.set_search("nothing-matches")
        await hub.settle()
        assert "No agents match `nothing-matches`. Esc clears the search." in _text(hub)
    finally:
        await hub.stop()


# ── approvals, input and lifecycle ───────────────────────────────────────────


def _register_approval(
    store: BackgroundStore, session_id: str, target: dict[str, object]
) -> BackgroundInteractionInbox:
    """Raise an approval the way a worker does: register, wait, publish."""
    inbox = BackgroundInteractionInbox(store)
    current = store.update(
        session_id,
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token="lease",
        attempt=1,
    )
    item = inbox.register(
        session_id=session_id,
        attempt=current.attempt,
        request_id="request-1",
        kind="approval",
        target=target,
        expires_at=None,
    )
    store.transition(
        session_id,
        SessionStatus.WAITING_APPROVAL,
        expected_status=SessionStatus.RUNNING,
        approval_item_id=item.item_id,
    )
    inbox.activate(item.item_id)
    return inbox


async def test_y_approves_the_exact_inbox_request(store: BackgroundStore, tmp_path: Path) -> None:
    _create(store, "fix", SessionStatus.RUNNING, 60, title="Fix the test")
    inbox = _register_approval(
        store,
        "fix",
        {"tool_name": "bash", "tool_input": {"command": "pytest -k schedule"}},
    )
    hub = await _open(store, tmp_path, tab=Tab.NEEDS_YOU)
    try:
        row = hub.selected_row()
        assert row is not None and row.item is not None
        assert "bash: pytest -k schedule" in _text(hub)
        assert "y approve" in _text(hub)
        hub.handle_key(Key.CHAR, "y")
        await hub.settle()
        item = inbox.get(row.item.item_id)
        assert item.status == "accepted"
        answer = inbox.accepted_answer(item.item_id)
        assert answer is not None and answer.response["allowed"] is True
    finally:
        await hub.stop()


async def test_a_plan_review_asks_for_the_mode_to_continue_in(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _create(store, "plan", SessionStatus.RUNNING, 60)
    inbox = _register_approval(
        store,
        "plan",
        {
            "tool_name": "exit_plan_mode",
            "request_kind": "plan_review",
            "mode_options": ["Safe", "Yolo"],
        },
    )
    hub = await _open(store, tmp_path, tab=Tab.NEEDS_YOU)
    try:
        hub.handle_key(Key.CHAR, "y")
        assert "[Safe]" in _text(hub)
        hub.handle_key(Key.RIGHT, "")
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        item = inbox.list_items(status="accepted")[0]
        answer = inbox.accepted_answer(item.item_id)
        assert answer is not None and answer.response["mode"] == "Yolo"
    finally:
        await hub.stop()


async def test_resume_and_retry_use_the_supervisor_like_the_cli(
    seeded: AgentsHub, store: BackgroundStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    supervisor = seeded.supervisor
    monkeypatch.setattr(
        supervisor,
        "resume",
        lambda session_id: calls.append(("resume", session_id)) or store.get(session_id),
    )
    monkeypatch.setattr(
        supervisor,
        "retry",
        lambda session_id: calls.append(("retry", session_id)) or store.get(session_id),
    )
    _create(store, "lost", SessionStatus.ORPHANED, 30)
    seeded._request_load("test", force=True)
    await seeded.settle()
    seeded._selected_key = "session:lost"
    assert "r resume" in _text(seeded)
    seeded.handle_key(Key.CHAR, "r")
    await seeded.settle()
    seeded._selected_key = "session:auth"
    seeded.handle_key(Key.CHAR, "R")  # completed: retry does not apply
    await seeded.settle()
    assert calls == [("resume", "lost")]


async def test_actions_that_do_not_apply_are_neither_listed_nor_accepted(
    seeded: AgentsHub, store: BackgroundStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGX-07 §6.3: a running agent offers no resume, retry, approval or archive."""
    calls: list[str] = []
    for name in ("resume", "retry", "archive"):
        monkeypatch.setattr(
            seeded.supervisor, name, lambda session_id, _name=name: calls.append(_name)
        )
    seeded._selected_key = "session:docs"
    footer = _text(seeded).splitlines()[-1]
    assert not any(word in footer for word in ("resume", "retry", "approve", "archive"))
    for key in ("r", "R", "y", "n", "A"):
        seeded.handle_key(Key.CHAR, key)
    await seeded.settle()
    assert calls == []
    assert store.get("docs").status is SessionStatus.RUNNING
    seeded.handle_key(Key.ENTER, "")  # the detail view offers the same set
    await seeded.settle()
    detail_footer = _text(seeded).splitlines()[-1]
    assert "stop" in detail_footer
    assert not any(word in detail_footer for word in ("resume", "retry", "approve"))


async def test_stop_and_archive_apply_to_marked_agents(
    seeded: AgentsHub, store: BackgroundStore
) -> None:
    seeded._selected_key = "session:docs"
    seeded.handle_key(Key.CHAR, "v")
    seeded._selected_key = "session:deps"
    seeded.handle_key(Key.CHAR, "m")
    assert seeded.marked_ids == {"docs", "deps"}
    seeded.handle_key(Key.CHAR, "c")
    await seeded.settle()
    assert store.get("docs").status in (SessionStatus.CANCELLING, SessionStatus.CANCELLED)
    assert store.get("deps").status in (SessionStatus.CANCELLING, SessionStatus.CANCELLED)
    assert not seeded.marked_ids


async def test_rename_labels_and_pin_go_through_prompts(
    seeded: AgentsHub, store: BackgroundStore
) -> None:
    seeded._selected_key = "session:docs"
    seeded.handle_key(Key.CHAR, "e")
    seeded.handle_key(Key.CTRL_U, "")
    for ch in "CLI reference":
        seeded.handle_key(Key.CHAR, ch)
    seeded.handle_key(Key.ENTER, "")
    seeded.handle_key(Key.CHAR, "l")
    for ch in "docs, release":
        seeded.handle_key(Key.CHAR, ch)
    seeded.handle_key(Key.ENTER, "")
    seeded.handle_key(Key.CHAR, "p")
    await seeded.settle()
    record = store.get("docs")
    assert (record.title, record.labels, record.pinned) == (
        "CLI reference",
        ("docs", "release"),
        True,
    )
    assert seeded.rows[0].key == "header:Pinned"


async def test_delete_moves_the_agent_to_recoverable_trash(
    seeded: AgentsHub, store: BackgroundStore
) -> None:
    seeded._selected_key = "session:auth"
    seeded.handle_key(Key.CTRL_X, "")
    await seeded.settle()
    assert store.get("auth", include_deleted=True).status is SessionStatus.DELETED
    assert "Deleted 1 agent(s) to recoverable trash" in seeded.notice


# ── peek and detail ──────────────────────────────────────────────────────────


def _journal(session: BackgroundSession, *records: dict[str, object]) -> None:
    path = Path(session.artifact_dir) / "conversation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


async def test_peek_shows_progress_under_the_row(store: BackgroundStore, tmp_path: Path) -> None:
    session = _create(
        store,
        "work",
        SessionStatus.RUNNING,
        30,
        artifact_dir=str(tmp_path / "artifacts" / "work"),
        started_at=NOW - 300,
    )
    _journal(
        session,
        {"kind": "text", "payload": {"text": "I changed the retry window. Next I run the tests."}},
        {
            "kind": "tool_complete",
            "payload": {"name": "bash", "args_str": "pytest -q", "success": True},
        },
    )
    hub = await _open(store, tmp_path)
    try:
        hub.handle_key(Key.CHAR, " ")
        await hub.settle()
        assert hub.peek_open
        text = _text(hub)
        assert "● running · 5m" in text
        assert "I changed the retry window" in text
        assert "bash" in text and "pytest -q" in text
        hub.handle_key(Key.ESC, "")
        assert not hub.peek_open
    finally:
        await hub.stop()


async def test_detail_shows_a_redacted_transcript_and_only_set_fields(
    store: BackgroundStore, tmp_path: Path
) -> None:
    session = _create(
        store,
        "work",
        SessionStatus.FAILED,
        60,
        artifact_dir=str(tmp_path / "artifacts" / "work"),
        error="provider error: rate limit exceeded (429)",
    )
    secret = "sk-ant-api03-" + "x" * 40
    _journal(
        session,
        {"kind": "user_message", "payload": {"text": f"use the key {secret} please"}},
        {"kind": "text", "payload": {"text": "Done."}},
    )
    hub = await _open(store, tmp_path, tab=Tab.HISTORY)
    try:
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        assert hub.detail_session_id == "work"
        text = _text(hub, height=60)
        assert secret not in text
        assert "use the key" in text
        assert "Error" in text and "rate limit exceeded" in text
        assert "Branch" not in text and "Worker PID" not in text  # empty fields are omitted
        hub.handle_key(Key.ESC, "")
        assert hub.detail_session_id is None
    finally:
        await hub.stop()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


async def test_detail_summarises_a_worktree_agents_changes(
    store: BackgroundStore, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "a@example.com")
    _git(repository, "config", "user.name", "A")
    (repository / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "a.py").write_text("a = 2\nb = 3\n", encoding="utf-8")  # uncommitted
    _create(
        store,
        "worker",
        SessionStatus.COMPLETED,
        60,
        cwd=str(repository),
        branch="agx/worker",
        base_commit=base,
        role="worker",
    )
    hub = await _open(store, tmp_path)
    try:
        hub._selected_key = "session:worker"
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        text = _text(hub, height=60)
        assert "1 file changed, +2 −1" in text
        assert "branch agx/worker" in text
        assert "d diff" in text
    finally:
        await hub.stop()


# ── keys, help, rebinding and the mouse (AGX-13) ─────────────────────────────


async def test_every_hub_action_is_in_help_and_resolves_in_its_context(seeded: AgentsHub) -> None:
    from agenthicc.tui.actions import (
        AGENTS_ACTIONS,
        AGENTS_DETAIL_ACTIONS,
        AGENTS_LIST_ACTIONS,
        AGENTS_PEEK_ACTIONS,
    )

    keymap = seeded.keymap
    everything = frozenset(
        {
            "row",
            "agent",
            "session",
            "inbox",
            "saved",
            "needs_approval",
            "accepts_input",
            "resumable",
            "retryable",
            "stoppable",
            "archivable",
            "attachable",
            "pinned",
            "worker",
            "collapsed",
            "expanded",
            "running_tab",
            "library_tab",
            "definition",
            "editable_definition",
            "primary_definition",
            "transcript",
            "changes",
        }
    )
    for context, actions in (
        ("agents.list", (*AGENTS_ACTIONS, *AGENTS_LIST_ACTIONS)),
        ("agents.peek", AGENTS_PEEK_ACTIONS),
        ("agents.detail", AGENTS_DETAIL_ACTIONS),
    ):
        names = {entry.action for entry in keymap.help(context)}
        for action in actions:
            assert action.name in names, (context, action.name)
            conditions = everything - action.unless
            resolved = keymap.resolve(context, action.keys[0], conditions)
            assert resolved is not None and resolved.name == action.name, (context, action.name)
    # Every action has a handler, and every handler an action.
    defined = {
        action.name
        for action in (
            *AGENTS_ACTIONS,
            *AGENTS_LIST_ACTIONS,
            *AGENTS_PEEK_ACTIONS,
            *AGENTS_DETAIL_ACTIONS,
        )
    }
    assert set(seeded.handlers()) == defined


async def test_help_overlay_lists_the_keys_from_the_keymap(seeded: AgentsHub) -> None:
    seeded.handle_key(Key.CHAR, "?")
    text = _text(seeded, height=80)
    assert "Approve the exact pending request" in text
    assert "(needs_approval)" in text
    seeded.handle_key(Key.CHAR, "x")
    assert not seeded.help_visible


async def test_rebinding_approve_applies_without_restart(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _create(store, "fix", SessionStatus.RUNNING, 60)
    inbox = _register_approval(store, "fix", {"tool_name": "bash", "tool_input": {"command": "ls"}})
    state = AppState.create()
    bindings = tmp_path / "keybindings.toml"
    service = KeymapService(state, bindings)
    hub = await _open(store, tmp_path, tab=Tab.NEEDS_YOU, app_state=state)
    try:
        assert "y approve" in _text(hub)
        bindings.write_text('[agents.list]\n"agents.approve" = "ctrl+y"\n', encoding="utf-8")
        service.reload()
        text = _text(hub, height=80)
        assert "Ctrl+Y approve" in text and "y approve" not in text
        hub.handle_key(Key.CHAR, "?")
        assert "Ctrl+Y" in _text(hub, height=80)
        hub.handle_key(Key.ESC, "")
        hub.handle_key(Key.CHAR, "y")  # no longer approves
        await hub.settle()
        assert inbox.list_items(status="pending")
        hub.handle_key(Key.CTRL_Y, "")
        await hub.settle()
        assert not inbox.list_items(status="pending")
    finally:
        await hub.stop()


async def test_a_click_selects_a_row_and_the_wheel_scrolls(seeded: AgentsHub) -> None:
    _text(seeded)  # lays out the frame the clicks refer to
    rows = seeded._geometry.rows
    line = next(line for line, index in rows.items() if seeded.rows[index].key == "session:docs")
    seeded.handle_key(Key.MOUSE, MouseEvent(0, 5, line + 1, pressed=True).encode())
    assert seeded.selected_row() is not None and seeded.selected_row().key == "session:docs"  # type: ignore[union-attr]
    # A second click opens it.
    seeded.handle_key(Key.MOUSE, MouseEvent(0, 5, line + 1, pressed=True).encode())
    assert seeded.detail_session_id == "docs"
    seeded.handle_key(Key.ESC, "")
    before = seeded.selected_row()
    seeded.handle_key(Key.MOUSE, MouseEvent(MouseEvent.WHEEL_UP, 1, 1, pressed=True).encode())
    assert seeded.selected_row() != before
    # A click on a tab switches to it.
    start, _end, _tab = seeded._geometry.tabs[2]
    seeded.handle_key(Key.MOUSE, MouseEvent(0, start + 1, 1, pressed=True).encode())
    assert seeded.tab is Tab.HISTORY


# ── drawing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("width", [60, 80, 120, 200])
async def test_status_labels_stay_complete_and_the_footer_is_one_line(
    seeded: AgentsHub, width: int
) -> None:
    text = _text(seeded, width=width, height=24)
    lines = text.splitlines()
    assert "◆ needs approval" in text and "◆ needs input" in text
    assert "◌ queued" in text
    footer = lines[-1]
    assert footer.rstrip().endswith("q quit") and "? help" in footer
    assert all(len(line) <= width for line in lines)
    assert len(lines) == 24


async def test_a_terminal_with_room_shows_more_than_ten_rows(
    store: BackgroundStore, tmp_path: Path
) -> None:
    for index in range(30):
        _create(store, f"r{index:02}", SessionStatus.RUNNING, index)
    hub = await _open(store, tmp_path)
    try:
        text = _text(hub, height=50)
        assert sum(1 for line in text.splitlines() if "● running" in line) > 10
    finally:
        await hub.stop()


async def test_screen_reader_rows_are_sentences(seeded: AgentsHub) -> None:
    from agenthicc.config import UISettings

    seeded._state.ui.set(UISettings(screen_reader=True))
    text = _text(seeded)
    assert "Investigate memory growth, needs input, app-0, 40 minutes ago." in text
    assert "─" not in text


async def test_an_empty_hub_explains_where_agents_come_from(
    store: BackgroundStore, tmp_path: Path
) -> None:
    hub = await _open(store, tmp_path)
    try:
        text = _text(hub)
        assert "No agents yet." in text
        assert "Agents waiting for your answer appear here" in text
    finally:
        await hub.stop()


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_dispatch_lines_take_a_directory_workflow_mode_and_model(tmp_path: Path) -> None:
    (tmp_path / "app-1").mkdir()
    request, _defaults = parse_dispatch(
        "Investigate the slow build\nthen report @app-1 /workflow code_plan /model gpt-x", tmp_path
    )
    assert request is not None
    assert request.intent == "Investigate the slow build\nthen report"
    assert request.cwd == (tmp_path / "app-1").resolve()
    assert (request.workflow_name, request.model) == ("code_plan", "gpt-x")
    alone, defaults = parse_dispatch("/mode Plan", tmp_path)
    assert alone is None and defaults.mode_name == "Plan"
    with pytest.raises(ValueError, match="No such directory"):
        parse_dispatch("go @missing", tmp_path)
    with pytest.raises(ValueError, match="Unknown /agent"):
        parse_dispatch("/agent reviewer look", tmp_path)


async def test_dispatch_starts_an_agent_and_selects_it(
    store: BackgroundStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = await _open(store, tmp_path)
    submitted: list[dict[str, object]] = []

    def submit(**request: object) -> BackgroundSession:
        submitted.append(request)
        return _create(store, "new-agent", SessionStatus.QUEUED, 0, title="Look into it")

    monkeypatch.setattr(hub.supervisor, "submit", submit)
    try:
        hub.handle_key(Key.CHAR, "+")
        for ch in "Look into it":
            hub.handle_key(Key.CHAR, ch)
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        assert submitted and submitted[0]["intent"] == "Look into it"
        assert submitted[0]["cwd"] == str(tmp_path)
        row = hub.selected_row()
        assert row is not None and row.key == "session:new-agent"
    finally:
        await hub.stop()


async def test_switching_from_inside_a_session_asks_first(
    store: BackgroundStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(store, "other", SessionStatus.RUNNING, 10, title="Other agent")
    hub = await _open(store, tmp_path, in_session=True)
    try:
        hub.handle_key(Key.ENTER, "")
        assert hub.detail_session_id == "other"
        assert hub.handle_key(Key.ENTER, "") is None
        assert "Switch to Other agent?" in _text(hub)
        hub.handle_key(Key.CHAR, "y")
        await hub.settle()
        assert hub.attach_result == HubResult("attach", "other")
    finally:
        await hub.stop()


async def test_the_run_filter_shows_one_goal_runs_agents(
    store: BackgroundStore, tmp_path: Path
) -> None:
    _create(store, "main", SessionStatus.RUNNING, 10, run_id="run-1")
    _create(store, "worker", SessionStatus.RUNNING, 10, run_id="run-1", parent_session_id="main")
    _create(store, "unrelated", SessionStatus.RUNNING, 10)
    hub = await _open(store, tmp_path, run_id="run-1")
    try:
        assert [row.key for row in hub.rows if row.selectable] == ["session:main", "session:worker"]
        assert hub.rows[2].depth == 1
    finally:
        await hub.stop()


def test_unseen_activity_is_remembered_per_user(tmp_path: Path) -> None:
    prefs = HubPrefs(path=tmp_path / "prefs.json")
    session = BackgroundSession.create("a", title="t", cwd="/", workflow_name="", intent="i")
    assert not prefs.unseen(session)  # first sight is not news
    changed = session.evolve(latest_activity="New report")
    assert prefs.unseen(changed)
    prefs.mark_seen(changed)
    prefs.save()
    assert not HubPrefs.load(tmp_path / "prefs.json").unseen(changed)


def _library_sources(project_root: Path, user_root: Path) -> HubSources:
    from agenthicc.plugins.trust import ExtensionTrust, override_extension_trust
    from agenthicc.tui.workspace.agents_hub.model import LibraryEntry
    from agenthicc.tui.workspace.agents_hub.sources import load_library

    def load() -> list[LibraryEntry]:
        trust = ExtensionTrust(
            store_path=project_root.parent / ".test-extension-trust.json",
            trusted_roots=(project_root, user_root),
        )
        with override_extension_trust(trust):
            return load_library(project_root, user_root)

    return HubSources(library=load)


def _submit_library_text(hub: AgentsHub, value: str) -> None:
    for character in value:
        hub.handle_key(Key.CHAR, character)
    hub.handle_key(Key.ENTER, "")


async def test_library_detail_loads_effective_tools_from_the_active_session(
    store: BackgroundStore, tmp_path: Path
) -> None:
    from agenthicc.agents.library import write_definition

    spec = AgentSpec(
        name="tool-inspector",
        description="Shows the effective session tools",
        prompt="Inspect tools.",
        mode="primary",
    )
    path = write_definition(spec, scope="project", project_root=tmp_path)
    app_state = AppState.create()
    calls: list[tuple[str, AppState]] = []

    def effective_tools(candidate: AgentSpec, policy_state: AppState) -> tuple[str, ...]:
        calls.append((candidate.name, policy_state))
        return ("fs_read", "git_diff")

    hub = await _open(
        store,
        tmp_path,
        tab=Tab.LIBRARY,
        sources=_library_sources(tmp_path / ".agenthicc", tmp_path / "home" / ".agenthicc"),
        app_state=app_state,
        effective_tools=effective_tools,
    )
    try:
        entry = next(row.entry for row in hub.rows if row.entry and row.entry.name == spec.name)
        assert entry is not None
        hub._show_library_entry(entry)
        assert any("calculating…" in line for line in hub._library_view_lines or ())
        await hub.settle()

        assert calls and calls[0][0] == spec.name
        assert calls[0][1] is not app_state
        assert any(
            line.endswith("fs_read, git_diff")
            for line in hub._library_view_lines or ()
            if line.startswith("Effective tools (model-visible;")
        )
        assert str(path) in "\n".join(hub._library_view_lines or ())
    finally:
        await hub.stop()


async def test_library_create_reviews_before_writing_a_definition(
    store: BackgroundStore, tmp_path: Path
) -> None:
    from agenthicc.agents.definitions import parse_definition

    agent_dir = tmp_path / ".agenthicc"
    hub = await _open(
        store,
        tmp_path,
        tab=Tab.LIBRARY,
        sources=_library_sources(agent_dir, tmp_path / "home" / ".agenthicc"),
    )
    path = agent_dir / "agents" / "hub-created.md"
    try:
        hub.handle_key(Key.CHAR, "n")
        hub.handle_key(Key.ENTER, "")  # project scope
        _submit_library_text(hub, "hub-created")
        _submit_library_text(hub, "Created from the Library")
        _submit_library_text(hub, "primary")
        hub.handle_key(Key.ENTER, "")  # session model
        hub.handle_key(Key.ENTER, "")  # session tools
        _submit_library_text(hub, "Follow the requested review checklist.")

        assert hub._choice is not None and hub._choice.kind == "library_save"
        assert not path.exists()
        assert any(
            "Follow the requested review checklist." in line
            for line in hub._library_view_lines or ()
        )

        hub.handle_key(Key.ENTER, "")
        await hub.settle()

        spec = parse_definition(path.read_text(encoding="utf-8"), path=path, source="project")
        assert spec.name == "hub-created"
        assert spec.mode == "primary"
        assert spec.prompt == "Follow the requested review checklist."
        assert any(row.entry is not None and row.entry.name == spec.name for row in hub.rows)
    finally:
        await hub.stop()


async def test_library_generated_definition_is_reviewed_then_saved(
    store: BackgroundStore, tmp_path: Path
) -> None:
    from agenthicc.agents.definitions import AgentSpec, render_definition

    calls: list[tuple[str, str]] = []

    async def generate(name: str, description: str) -> str:
        calls.append((name, description))
        return render_definition(
            AgentSpec(
                name=name,
                description=description,
                prompt="Generated prompt for review.",
                mode="subagent",
            )
        )

    agent_dir = tmp_path / ".agenthicc"
    hub = await _open(
        store,
        tmp_path,
        tab=Tab.LIBRARY,
        sources=_library_sources(agent_dir, tmp_path / "home" / ".agenthicc"),
    )
    hub._generate_definition = generate
    path = agent_dir / "agents" / "generated-reviewer.md"
    try:
        hub.handle_key(Key.CHAR, "g")
        hub.handle_key(Key.ENTER, "")
        _submit_library_text(hub, "generated-reviewer")
        _submit_library_text(hub, "Reviews generated changes")
        await hub.settle()

        assert calls == [("generated-reviewer", "Reviews generated changes")]
        assert hub._choice is not None and hub._choice.kind == "library_save"
        assert not path.exists()
        assert any("Generated prompt for review." in line for line in hub._library_view_lines or ())

        hub.handle_key(Key.ENTER, "")
        await hub.settle()

        assert path.is_file()
        assert "Generated prompt for review." in path.read_text(encoding="utf-8")
    finally:
        await hub.stop()


async def test_library_edit_uses_external_editor_and_reloads_definition(
    store: BackgroundStore, tmp_path: Path
) -> None:
    from agenthicc.agents.definitions import AgentSpec, render_definition
    from agenthicc.agents.library import write_definition
    from agenthicc.plugins.trust import current_extension_trust

    original = AgentSpec(
        name="external-edit",
        description="Before edit",
        prompt="Initial prompt.",
        mode="primary",
    )
    path = write_definition(original, scope="project", project_root=tmp_path)
    calls: list[tuple[Path, str]] = []

    async def edit(path: Path, scope: Literal["project", "user"]) -> None:
        calls.append((path, scope))
        updated = AgentSpec(
            name=original.name,
            description="After external edit",
            prompt="Changed in $EDITOR.",
            mode="primary",
        )
        path.write_text(render_definition(updated), encoding="utf-8")
        current_extension_trust().trust(path, kind="agent")

    hub = await _open(
        store,
        tmp_path,
        tab=Tab.LIBRARY,
        sources=_library_sources(tmp_path / ".agenthicc", tmp_path / "home" / ".agenthicc"),
        external_editor=edit,
    )
    try:
        hub._selected_key = f"definition:project:{path}"
        hub.handle_key(Key.CHAR, "e")
        await hub.settle()

        assert calls == [(path, "project")]
        assert "Changed in $EDITOR." in path.read_text(encoding="utf-8")
        assert "reloaded" in hub._notice
        entry = next(row.entry for row in hub.rows if row.entry and row.entry.name == original.name)
        assert entry is not None and entry.spec is not None
        assert entry.spec.description == "After external edit"
    finally:
        await hub.stop()


async def test_full_screen_hub_runs_external_editor_outside_raw_mode(
    store: BackgroundStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = await _open(store, tmp_path)
    loop = asyncio.get_running_loop()
    wake = threading.Event()
    raw_depth = 0
    reads = 0
    editor_raw_depths: list[int] = []

    class Backend:
        def is_interactive(self) -> bool:
            return True

        def enter_raw_mode(self) -> object:
            class RawMode:
                def __enter__(self) -> None:
                    nonlocal raw_depth
                    raw_depth += 1

                def __exit__(self, *args: object) -> None:
                    nonlocal raw_depth
                    raw_depth -= 1

            return RawMode()

        def read_key(self) -> tuple[Key, str]:
            nonlocal reads
            reads += 1
            if reads == 1:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(hub.run_terminal_handoff(edit))
                )
                if not wake.wait(timeout=2):
                    raise TimeoutError("terminal handoff did not wake hub input")
                raise OSError("terminal input stopped")
            return Key.UNKNOWN, ""

        def restore(self) -> None:
            wake.set()

        def close(self) -> None:
            wake.clear()

        def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
            return None

    async def edit() -> None:
        editor_raw_depths.append(raw_depth)

    class Live:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    backend = Backend()
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)
    monkeypatch.setattr("agenthicc.tui.workspace.live_region.LiveRegion", Live)
    monkeypatch.setattr(hub, "_draw", lambda _live: None)
    monkeypatch.setattr(hub, "handle_key", lambda _key, _char="": HubResult("exit"))

    result = await hub.run()

    assert result == HubResult("exit")
    assert editor_raw_depths == [0]
    assert raw_depth == 0
    assert reads == 2


async def test_library_edit_default_and_confirmed_delete(
    store: BackgroundStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.agents.definitions import AgentSpec, render_definition
    from agenthicc.agents.library import write_definition
    from agenthicc.agents.selection import project_default

    monkeypatch.setenv("AGENTHICC_AGENT_DEFAULTS", str(tmp_path / "agent-defaults.json"))
    spec = AgentSpec(
        name="hub-primary",
        description="Before edit",
        prompt="Initial prompt.",
        mode="primary",
    )
    path = write_definition(spec, scope="project", project_root=tmp_path)
    agent_dir = tmp_path / ".agenthicc"
    hub = await _open(
        store,
        tmp_path,
        tab=Tab.LIBRARY,
        sources=_library_sources(agent_dir, tmp_path / "home" / ".agenthicc"),
    )
    try:
        entry = next(row.entry for row in hub.rows if row.entry and row.entry.name == spec.name)
        assert entry is not None
        hub._selected_key = f"definition:project:{path}"
        hub.handle_key(Key.CHAR, "*")
        await hub.settle()
        assert project_default(tmp_path) == spec.name

        hub.handle_key(Key.CHAR, "e")
        await hub.settle()
        updated = AgentSpec(
            name=spec.name,
            description="After edit",
            prompt="Updated prompt.",
            mode="primary",
        )
        hub.handle_key(Key.CTRL_U, "")
        hub.handle_key(Key.PASTE, render_definition(updated))
        hub.handle_key(Key.ENTER, "")
        await hub.settle()
        content = path.read_text(encoding="utf-8")
        assert "description: After edit" in content
        assert "Updated prompt." in content

        hub.handle_key(Key.CTRL_X, "")
        assert hub._choice is not None and hub._choice.kind == "library_delete"
        assert path.exists()
        hub.handle_key(Key.CHAR, "y")
        await hub.settle()
        assert not path.exists()
        assert project_default(tmp_path) is None
    finally:
        await hub.stop()


async def test_library_validate_shows_file_and_parse_error(
    store: BackgroundStore, tmp_path: Path
) -> None:
    agent_dir = tmp_path / ".agenthicc" / "agents"
    agent_dir.mkdir(parents=True)
    invalid = agent_dir / "broken.md"
    invalid.write_text("not a definition", encoding="utf-8")
    hub = await _open(
        store,
        tmp_path,
        tab=Tab.LIBRARY,
        sources=_library_sources(tmp_path / ".agenthicc", tmp_path / "home" / ".agenthicc"),
    )
    try:
        hub.handle_key(Key.CHAR, "v")
        await hub.settle()

        text = "\n".join(hub._library_view_lines or ())
        assert "broken" in text
        assert str(invalid) in text
        assert "frontmatter" in text
    finally:
        await hub.stop()
