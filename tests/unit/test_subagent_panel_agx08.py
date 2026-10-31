"""The subagent panel, transcripts and their screens (AGX-08)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from agenthicc.subagents.pool import FINISHED_ROW_S, SubagentPoolState, WorkerState
from agenthicc.subagents.transcripts import (
    TranscriptEvent,
    TranscriptTail,
    TranscriptWriter,
    WorkerRecord,
    last_event,
    list_pools,
    read_transcript,
)
from agenthicc.tools.approval import ApprovalRequest, ApprovalRequester
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.subagent_panel import panel_lines, summary_line, tokens
from agenthicc.tui.workspace.overlays.subagents import (
    EarlierPool,
    SubagentTranscriptOverlay,
    SubagentsOverlay,
    TranscriptTarget,
    load_earlier,
    transcript_lines,
)

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0


def _worker(worker_id: str, status: str, **changes: object) -> WorkerState:
    label = str(changes.pop("label", f"explorer #{worker_id[-1]}"))
    return WorkerState(
        label,
        "explorer",
        status,
        worker_id=worker_id,
        activity=str(changes.pop("activity", "")),
        started_at=changes.pop("started_at", NOW - 42),  # type: ignore[arg-type]
        finished_at=changes.pop("finished_at", None),  # type: ignore[arg-type]
        tokens=int(changes.pop("tokens", 0)),  # type: ignore[call-overload]
        dismissed=bool(changes.pop("dismissed", False)),
    )


def _plain(markup: str) -> str:
    from rich.text import Text

    return Text.from_markup(markup).plain


async def _write(
    root: Path, events: dict[str, list[TranscriptEvent]], *, limit: int = 2**21
) -> TranscriptWriter:
    writer = TranscriptWriter(root, "pool-1", limit=limit)
    writer.start(
        [
            WorkerRecord(worker_id, f"explorer #{i}", "explorer", "task")
            for i, worker_id in enumerate(events, 1)
        ],
        NOW,
    )
    for worker_id, items in events.items():
        for event in items:
            writer.append(worker_id, event)
    writer.finish(NOW + 10)
    await writer.aclose()
    return writer


# ── transcripts ──────────────────────────────────────────────────────────────


async def test_transcripts_are_written_in_order_and_listed_newest_first(tmp_path: Path) -> None:
    await _write(
        tmp_path,
        {
            "task-0": [
                TranscriptEvent("prompt", NOW, text="Find the store"),
                TranscriptEvent("tool_started", NOW + 1, text="src/store.py", tool="read"),
                TranscriptEvent("finished", NOW + 2, text="Found it.", ok=True),
            ]
        },
    )
    older = tmp_path / "pool-0"
    older.mkdir()
    (older / "pool.json").write_text(
        json.dumps({"pool_id": "pool-0", "created_at": NOW - 99, "workers": []})
    )
    pools = list_pools(tmp_path)
    assert [pool.pool_id for pool in pools] == ["pool-1", "pool-0"]
    assert pools[0].finished_at == NOW + 10
    events = read_transcript(pools[0].transcript_path("task-0"))
    assert [event.kind for event in events] == ["prompt", "tool_started", "finished"]
    last = last_event(pools[0].transcript_path("task-0"))
    assert last is not None and last.kind == "finished" and last.ok is True


async def test_transcripts_mask_secrets(tmp_path: Path) -> None:
    await _write(
        tmp_path,
        {"task-0": [TranscriptEvent("text", NOW, text='Use password = "hunter2hunter2" here')]},
    )
    content = (tmp_path / "pool-1" / "task-0.jsonl").read_text()
    assert "hunter2hunter2" not in content


async def test_a_long_transcript_keeps_its_newest_events_behind_a_marker(tmp_path: Path) -> None:
    events = [TranscriptEvent("text", NOW + i, text=f"message {i} " + "x" * 300) for i in range(60)]
    await _write(tmp_path, {"task-0": events}, limit=8_192)
    path = tmp_path / "pool-1" / "task-0.jsonl"
    assert path.stat().st_size <= 8_192
    kept = read_transcript(path)
    assert kept[0].kind == "truncated" and kept[0].dropped > 0
    assert kept[-1].text.startswith("message 59")
    assert kept[0].dropped + len(kept) - 1 == 60  # every event is kept or counted


def test_unsafe_identifiers_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TranscriptWriter(tmp_path, "../escape")


def test_a_tail_reads_only_what_was_appended(tmp_path: Path) -> None:
    path = tmp_path / "w.jsonl"
    tail = TranscriptTail(path)
    assert not tail.refresh()
    path.write_text(json.dumps(TranscriptEvent("prompt", NOW, text="a").to_dict()) + "\n")
    assert tail.refresh() and len(tail.events) == 1
    with path.open("a") as handle:
        handle.write(json.dumps(TranscriptEvent("text", NOW, text="b").to_dict()) + "\n")
        handle.write('{"kind": "text", "at"')  # a write in progress
    assert tail.refresh() and [e.text for e in tail.events] == ["a", "b"]
    with path.open("a") as handle:
        handle.write(': 1.0, "text": "c"}\n')
    assert tail.refresh() and [e.text for e in tail.events] == ["a", "b", "c"]
    replacement = tmp_path / "new.jsonl"
    replacement.write_text(
        json.dumps(TranscriptEvent("truncated", NOW, dropped=3).to_dict()) + "\n"
    )
    os.replace(replacement, path)  # compacted: a new file
    assert tail.refresh() and [e.kind for e in tail.events] == ["truncated"]


# ── the snapshot ─────────────────────────────────────────────────────────────


def test_finished_rows_fold_and_failures_stay_until_dismissed() -> None:
    state = SubagentPoolState(
        "pool",
        4,
        (
            _worker("task-1", "running"),
            _worker("task-2", "done", finished_at=NOW - 5),
            _worker("task-3", "done", finished_at=NOW - FINISHED_ROW_S - 1),
            _worker("task-4", "failed", finished_at=NOW - 999),
        ),
    )
    assert [w.worker_id for w in state.shown(NOW)] == ["task-1", "task-2", "task-4"]
    assert state.active and state.done == 3
    finished = SubagentPoolState(
        "pool",
        1,
        (_worker("task-4", "failed", finished_at=NOW - 999, dismissed=True),),
        finished_at=NOW,
    )
    assert finished.expired(NOW) and not finished.active


def test_the_store_tick_clears_a_finished_pool_with_nothing_to_show() -> None:
    state = AppState.create()
    conversation = state.conversation
    conversation.subagent_pool_state.set(
        SubagentPoolState("pool", 1, (_worker("task-1", "done", finished_at=1.0),), finished_at=1.0)
    )
    conversation.tick()
    assert conversation.subagent_pool_state() is None and conversation.subagent_rows() == 0


# ── the panel ────────────────────────────────────────────────────────────────


def test_the_panel_lists_each_workers_status_activity_time_and_tokens() -> None:
    state = SubagentPoolState(
        "pool",
        3,
        (
            _worker("task-1", "running", activity="read src/store.py", tokens=3_100),
            _worker("task-2", "waiting", activity="needs approval: bash pytest"),
            _worker(
                "task-3", "done", activity="3 findings", finished_at=NOW - 11, started_at=NOW - 42
            ),
        ),
        limit=4,
    )
    rows = [
        _plain(row)
        for row in panel_lines(state, 120, now=NOW, terminal_rows=40, hint="Ctrl+O focus")
    ]
    assert rows[0].startswith("  Subagents · 1 needs input · 1 running · 1/3 done")
    assert rows[0].rstrip().endswith("Ctrl+O focus")
    assert summary_line(state, 100) == (
        "  [bold magenta]Subagents[/bold magenta] · 1 needs input · 1 running · 1/3 done"
    )
    assert "● Explorer 1" in rows[1] and "read src/store.py" in rows[1]
    assert "0:42" in rows[1] and "3.1k" in rows[1]
    assert "◆ Explorer 2" in rows[2] and "Needs approval: bash pytest" in rows[2]
    assert "✓ Explorer 3" in rows[3] and "0:31" in rows[3]
    assert all(len(row) <= 120 for row in rows)


def test_pool_summary_distinguishes_queued_approval_and_attention_counts() -> None:
    state = SubagentPoolState(
        "pool",
        6,
        (
            _worker("task-1", "pending"),
            _worker("task-2", "running"),
            _worker("task-3", "waiting"),
            _worker("task-4", "done"),
            _worker("task-5", "failed"),
            _worker("task-6", "stopped"),
        ),
        limit=2,
    )

    summary = _plain(summary_line(state, 120))

    assert "1 failed" in summary
    assert "1 needs input" in summary
    assert "1 stopped" in summary
    assert "1 running" in summary and "1 queued" in summary
    assert "1/6 done" in summary and "limit 2" in summary


def test_panel_rows_fit_the_width_matrix_and_keep_attention_visible() -> None:
    from agenthicc.tui.rendering import visible_len

    state = SubagentPoolState(
        "pool",
        5,
        (
            _worker("task-1", "running", activity="read_file src/a/very/long/module.py"),
            _worker("task-2", "waiting", activity="needs approval: run_tests tests/unit"),
            _worker("task-3", "failed"),
            _worker("task-4", "pending"),
            _worker("task-5", "done"),
        ),
        limit=2,
    )
    for width in (40, 63, 64, 80, 119, 120, 200):
        rows = panel_lines(state, width, now=NOW, terminal_rows=40, hint="Ctrl+O details")
        assert all(visible_len(row) <= width for row in rows)
        assert any(word in _plain(rows[0]) for word in ("failed", "fail"))
    narrow = panel_lines(state, 39, now=NOW, terminal_rows=40)
    assert len(narrow) == 2 and "Explorer 3" in _plain(narrow[1])


def test_nested_pool_summary_shows_depth_and_keeps_parent_identity() -> None:
    state = SubagentPoolState(
        "nested-pool",
        1,
        (
            WorkerState(
                "explorer #1",
                "explorer",
                worker_id="task-1",
                depth=2,
                parent_pool_id="nested-pool",
            ),
        ),
        depth=2,
        parent_pool_id="parent-pool",
        limit=4,
    )

    summary = _plain(summary_line(state, 100))

    assert "depth 2" in summary
    assert state.parent_pool_id == "parent-pool"
    assert state.workers[0].depth == 2
    assert state.workers[0].parent_pool_id == "nested-pool"


def test_more_than_six_workers_collapse_and_a_short_terminal_shows_the_summary() -> None:
    workers = tuple(_worker(f"task-{i}", "running") for i in range(9))
    state = SubagentPoolState("pool", 9, workers)
    rows = panel_lines(state, 100, now=NOW, terminal_rows=40)
    assert len(rows) == 1 + 6 and _plain(rows[-1]).strip() == "… +4 more"
    assert len(panel_lines(state, 100, now=NOW, terminal_rows=20)) == 1


def test_screen_reader_rows_are_plain_sentences() -> None:
    state = SubagentPoolState("pool", 1, (_worker("task-1", "running", activity="grep todo"),))
    rows = panel_lines(state, 100, now=NOW, terminal_rows=40, plain=True)
    assert "·" not in rows[0] and "●" not in rows[1]
    assert "running, Explorer 1, grep todo, elapsed 0:42" in _plain(rows[1])
    assert tokens(999) == "999" and tokens(2_500_000) == "2.5M"
    assert "failed" in _plain(
        summary_line(SubagentPoolState("p", 1, (_worker("a1", "failed"),)), 80)
    )


# ── the focused panel ────────────────────────────────────────────────────────


def _render(overlay: object, width: int = 100) -> str:
    console = Console(width=width, record=True, color_system=None)
    console.print(overlay.render())  # type: ignore[attr-defined]
    return console.export_text()


async def test_the_focused_panel_selects_opens_stops_and_dismisses(tmp_path: Path) -> None:
    state = AppState.create()
    state.conversation.subagent_pool_state.set(
        SubagentPoolState(
            "pool",
            3,
            (
                _worker("task-1", "running"),
                _worker("task-2", "failed", finished_at=NOW),
                _worker("task-3", "done", finished_at=NOW),
            ),
            transcripts=tmp_path,
        )
    )
    opened: list[TranscriptTarget] = []
    stopped: list[tuple[str, str]] = []
    dismissed: list[tuple[str, str]] = []
    closed: list[bool] = []
    overlay = SubagentsOverlay(
        state,
        open_transcript=opened.append,
        stop=lambda pool, worker: stopped.append((pool, worker)) or True,
        dismiss=lambda pool, worker: dismissed.append((pool, worker)) or True,
        close=lambda: closed.append(True),
        clock=lambda: NOW,
    )
    text = _render(overlay)
    assert "Explorer 1" in text and "x stop" in text
    overlay.handle_key(Key.CHAR, "x")
    assert stopped == [("pool", "task-1")]
    overlay.handle_key(Key.DOWN, "")
    assert "x dismiss" in _render(overlay)
    overlay.handle_key(Key.CHAR, "x")
    assert dismissed == [("pool", "task-2")]
    overlay.handle_key(Key.ENTER, "")
    assert opened and opened[-1].pool_id == "pool" and opened[-1].directory == tmp_path
    assert [w.worker_id for w in opened[-1].workers][opened[-1].index] == "task-3"
    overlay.handle_key(Key.ESC, "")
    assert closed == [True]


async def test_earlier_pools_come_from_their_transcripts(tmp_path: Path) -> None:
    await _write(
        tmp_path,
        {
            "task-0": [TranscriptEvent("finished", NOW, text="All good.", ok=True)],
            "task-1": [TranscriptEvent("tool_started", NOW, tool="read")],
        },
    )
    earlier = load_earlier(tmp_path)
    assert [w.status for w in earlier[0].workers] == ["done", "stopped"]
    overlay = SubagentsOverlay(
        AppState.create(),
        earlier=earlier,
        open_transcript=lambda target: None,
        stop=lambda pool, worker: False,
        dismiss=lambda pool, worker: False,
        close=lambda: None,
        clock=lambda: NOW,
    )
    text = _render(overlay)
    assert "Earlier" in text and "All good." in text and "interrupted" in text
    assert overlay.conditions() == frozenset({"worker"})  # earlier workers cannot be stopped
    assert isinstance(earlier[0], EarlierPool)


# ── a transcript ─────────────────────────────────────────────────────────────


async def test_a_transcript_moves_between_siblings_and_returns_at_the_top(tmp_path: Path) -> None:
    await _write(
        tmp_path,
        {
            "task-0": [
                TranscriptEvent("prompt", NOW, text="Find the store"),
                TranscriptEvent("tool_started", NOW + 1, text="src/store.py", tool="read"),
                TranscriptEvent("tool_finished", NOW + 2, text="0.1s", tool="read", ok=True),
                TranscriptEvent("approval_requested", NOW + 3, text="pytest", tool="bash"),
                TranscriptEvent("approval_resolved", NOW + 4, tool="bash", ok=False),
                TranscriptEvent("finished", NOW + 5, text="Done.", ok=True),
            ],
            "task-1": [TranscriptEvent("prompt", NOW, text="Second task")],
        },
    )
    state = AppState.create()
    workers = (_worker("task-0", "done", finished_at=NOW + 5), _worker("task-1", "running"))
    closed: list[bool] = []
    stopped: list[str] = []
    overlay = SubagentTranscriptOverlay(
        state,
        TranscriptTarget("pool-1", workers, tmp_path / "pool-1", 0),
        stop=lambda pool, worker: stopped.append(worker) or True,
        close=lambda: closed.append(True),
        clock=lambda: NOW,
    )
    overlay.set_viewport(100, 20)
    text = _render(overlay)
    for expected in (
        "task: Find the store",
        "read  src/store.py",
        "needs approval: bash",
        "bash denied",
        "done · Done.",
    ):
        assert expected in text
    assert "x stop" not in text  # a finished worker
    overlay.handle_key(Key.RIGHT, "")
    assert overlay.worker_id == "task-1" and "Second task" in _render(overlay)
    overlay.handle_key(Key.LEFT, "")
    assert overlay.worker_id == "task-0"
    overlay.handle_key(Key.UP, "")  # at the top: back to the conversation
    assert closed == [True]


def test_transcript_lines_fit_the_width() -> None:
    lines = transcript_lines([TranscriptEvent("text", NOW, text="word " * 100)], 60)
    assert all(len(_plain(line)) <= 60 for line in lines)


# ── the approval prompt ──────────────────────────────────────────────────────


def test_a_workers_approval_names_it_and_o_opens_its_transcript() -> None:
    from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay

    requester = ApprovalRequester("tester #1", "tester", "pool", "task-0")
    request = ApprovalRequest(
        tool_name="bash",
        tool_use_id="call-1",
        tool_input={"command": "pytest"},
        capabilities=frozenset({"execute"}),
        event=asyncio.Event(),
        requester=requester,
    )
    responses: list[bool] = []
    service = SimpleNamespace(respond=lambda allowed, **_: responses.append(allowed) or True)
    opened: list[ApprovalRequester] = []
    overlay = ApprovalOverlay(
        request,
        service,
        lambda: None,
        open_requester=opened.append,  # type: ignore[arg-type]
    )
    text = _render(overlay)
    assert "tester #1 (tester) wants to run bash" in text
    assert "o open tester #1's transcript" in text
    overlay.handle_key(Key.CHAR, "o")
    assert opened == [requester] and responses == []
    overlay.handle_key(Key.ESC, "")
    assert responses == [False]  # only this call is denied


# ── the session ──────────────────────────────────────────────────────────────


def _session(tmp_path: Path) -> object:
    from agenthicc.runners.tui_session import TUISession
    from agenthicc.supervised_tasks import SupervisedTasks
    from agenthicc.tui.workspace.overlay import OverlayHost

    state = AppState.create()
    session = TUISession.__new__(TUISession)
    session._ctx = SimpleNamespace(  # type: ignore[assignment]
        app_state=state, session_id="session-1", approval_svc=None
    )
    session._workspace = SimpleNamespace(overlays=OverlayHost(state))  # type: ignore[assignment]
    session._tasks = SupervisedTasks("test")
    return session


def test_a_hub_subagent_row_opens_its_transcript(tmp_path: Path) -> None:
    from agenthicc.tui.workspace.agents_hub import HubResult

    session = _session(tmp_path)
    state = session._ctx.app_state  # type: ignore[attr-defined]
    state.conversation.subagent_pool_state.set(
        SubagentPoolState("pool", 1, (_worker("task-0", "running"),), transcripts=tmp_path)
    )
    session._switch_to(HubResult("subagent", "pool:task-0"))  # type: ignore[attr-defined]
    widget = session._workspace.overlays.widget  # type: ignore[attr-defined]
    assert isinstance(widget, SubagentTranscriptOverlay) and widget.worker_id == "task-0"


def test_dismissing_a_finished_pools_failure_updates_the_panel(tmp_path: Path) -> None:
    session = _session(tmp_path)
    state = session._ctx.app_state  # type: ignore[attr-defined]
    state.conversation.subagent_pool_state.set(
        SubagentPoolState(
            "pool", 1, (_worker("task-0", "failed", finished_at=NOW),), finished_at=NOW
        )
    )
    assert session._dismiss_subagent("pool", "task-0")  # type: ignore[attr-defined]
    assert state.conversation.subagent_pool_state().workers[0].dismissed
    assert not session._stop_subagent("pool", "task-0")  # type: ignore[attr-defined]


async def test_ctrl_o_on_the_prompt_opens_the_focused_panel() -> None:
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime.commands import CommandBus, OpenSubagentsCommand

    state = AppState.create()
    bus = CommandBus()
    opened: list[OpenSubagentsCommand] = []
    bus.register(OpenSubagentsCommand, opened.append)
    session = UnifiedInputSession(state, bus)
    await session._dispatch(Key.CTRL_O, "")
    assert opened == []  # no pool: Ctrl+O is not bound
    state.conversation.subagent_pool_state.set(
        SubagentPoolState("pool", 1, (_worker("task-0", "running"),))
    )
    await session._dispatch(Key.CTRL_O, "")
    assert len(opened) == 1


def test_the_subagents_command_opens_the_panel() -> None:
    from agenthicc.commands.builtins import _cmd_subagents
    from agenthicc.commands.command import CommandContext

    opened: list[bool] = []
    printed: list[str] = []
    console = SimpleNamespace(print=lambda text, **_kwargs: printed.append(str(text)))
    context = CommandContext(
        text="/subagents",
        args="",
        model="m",
        console=console,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        open_subagents=lambda: opened.append(True),
    )
    assert _cmd_subagents(context) and opened == [True]
    headless = CommandContext(
        text="/subagents",
        args="",
        model="m",
        console=console,
        config=None,  # type: ignore[arg-type]
    )
    assert _cmd_subagents(headless) and "interactive TUI" in printed[-1]


def test_the_live_block_shows_the_panel_between_prompt_and_footer() -> None:
    from agenthicc.tui.workspace.workspace import Workspace

    state = AppState.create()
    console = Console(width=100, height=40, record=True, color_system=None)
    workspace = Workspace(state, console)
    state.conversation.subagent_pool_state.set(
        SubagentPoolState("pool", 1, (_worker("task-0", "running", activity="grep todo"),))
    )
    lines = ["".join(segment.text for segment in line) for line in workspace._build_lines()]
    panel = next(i for i, line in enumerate(lines) if "Subagents" in line)
    assert "grep todo" in lines[panel + 1]
    assert "Ctrl+O focus" in lines[panel]


# ── the inbox and the hub ────────────────────────────────────────────────────


def test_a_background_workers_inbox_request_names_the_subagent() -> None:
    from agenthicc.background.worker import BackgroundApprovalService
    from agenthicc.background.inbox import BackgroundInboxItem
    from agenthicc.tui.workspace.agents_hub.detail import approval_lines

    request = ApprovalRequest(
        tool_name="bash",
        tool_use_id="call-1",
        tool_input={"command": "pytest"},
        capabilities=frozenset({"execute"}),
        event=asyncio.Event(),
        requester=ApprovalRequester("tester #1", "tester", "pool", "task-0"),
    )
    target = BackgroundApprovalService._approval_target(request)
    assert target["requester"] == {
        "label": "tester #1",
        "agent_type": "tester",
        "pool_id": "pool",
        "worker_id": "task-0",
    }
    item = BackgroundInboxItem(
        item_id="i",
        session_id="s",
        attempt=1,
        request_id="r",
        kind="approval",
        created_at=NOW,
        expires_at=None,
        target=target,
        status="pending",
        version=1,
        required_scope="",
        project_root="",
        workspace="",
    )
    assert ("from", "tester #1 (tester)") in approval_lines(item)


def test_the_in_session_hub_lists_the_turns_subagents() -> None:
    from agenthicc.tui.workspace.agents_hub.model import SUBAGENTS_GROUP, RowKind, running_rows

    pool = SubagentPoolState(
        "pool",
        2,
        (_worker("task-0", "running"), _worker("task-1", "failed", dismissed=True)),
    )
    rows = running_rows([], subagents=pool)
    assert [row.kind for row in rows] == [RowKind.HEADER, RowKind.SUBAGENT]
    assert rows[0].text == SUBAGENTS_GROUP and rows[1].key == "subagent:pool:task-0"
