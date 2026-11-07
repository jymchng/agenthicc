"""E2E: TUX-06 transcript data and replay through real sessions.

A ``MockTransport`` scripts real ``tool_use`` completions; the agent-turn
runner executes the built-in tools, the conversation store persists to a real
session log, and ``ScrollBufferAppender`` renders the replay.

NOTE: no ``from __future__ import annotations`` — the ``@tool()`` decorator
inspects annotations at decoration time.
"""

import asyncio
import json
import os
import re
import time
from io import StringIO
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._memory import ShortTermMemory
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport
from rich.console import Console

from agenthicc.config import AgenthiccConfig
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.tui.conversation_store import AppState as TUIAppState
from agenthicc.tui.runtime.mode_manager import build_default_registry
from agenthicc.tui.runtime.replay import ConversationReplayer
from agenthicc.tui.runtime.session_log import SessionEventLog
from agenthicc.tui.workspace.appender import ScrollBufferAppender

pytestmark = pytest.mark.e2e

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tux06" / "agt_tools_session.jsonl"
_MARKUP = re.compile(
    r"\[/?(?:dim|bold|italic|underline|reverse|red|green|yellow|blue|magenta|cyan|white|"
    r"black|grey\d*|gray\d*|bright_\w+|#[0-9a-fA-F]{6})[^\]]*\]"
)


#: Exactly the data a ``tool_complete`` event carries (TUX-06 §2.1).
_PAYLOAD_FIELDS = {
    "tool_use_id",
    "name",
    "success",
    "state",
    "ready",
    "summary",
    "summary_detail",
    "args",
    "duration_ms",
    "output_lines",
    "output_head",
    "output_total",
    "exit_code",
    "error_code",
}


def _completion(index, content="", calls=()):
    return Completion(
        id=f"c{index}",
        model="mock-model",
        content=content,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSIONS_DIR", root)
    monkeypatch.setattr("agenthicc.tui.runtime.session_log._SESSION_INDEX", root / "index.json")
    return root


@pytest.fixture
def app_state():
    state = TUIAppState.create()
    yolo = build_default_registry().get("Yolo")
    if yolo is not None:
        state.active_mode.set(yolo)
    return state


@pytest.fixture
async def processor(tmp_path):
    kernel_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(proc.run())
    yield proc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _turn(app_state, processor, mock, text):
    from unittest.mock import MagicMock

    cfg = AgenthiccConfig()
    await _run_agent_turn(
        text,
        runner=AgentRunnerBase(transport=mock, signals=SignalBus()),
        processor=processor,
        session_memory=ShortTermMemory(max_tokens=cfg.execution.effective_usable_budget()),
        max_agent_turns=8,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills={},
        skill_permissions=cfg.agents.skill_permissions_for("auto"),
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=[],
        system_prompt_suffix="",
    )


async def test_a_new_session_writes_tool_events_without_markup(
    tmp_path, monkeypatch, sessions, app_state, processor
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("TODO one\nTODO two\n", encoding="utf-8")
    mock = MockTransport()
    mock.queue_response(
        _completion(
            0,
            calls=[
                ToolCall(tool_use_id="r1", name="read_file", input={"path": "docs/notes.md"}),
                ToolCall(
                    tool_use_id="g1", name="grep_files", input={"pattern": "TODO", "path": "docs"}
                ),
                ToolCall(tool_use_id="m1", name="read_file", input={"path": "missing.md"}),
            ],
        )
    )
    mock.queue_response(_completion(1, "Done."))
    log = SessionEventLog("s-new")
    app_state.conversation.on_event(log.append)
    try:
        await _turn(app_state, processor, mock, "Read the notes")
    finally:
        log.close()

    records = [
        json.loads(line)
        for line in (sessions / "s-new" / "conversation.jsonl").read_text().splitlines()
    ]
    tools = [record["payload"] for record in records if record["kind"] == "tool_complete"]
    assert {payload["name"] for payload in tools} == {"read_file", "grep_files"}
    for payload in tools:
        assert not _MARKUP.search(json.dumps(payload)), payload
        assert set(payload) - {"workspace_access", "presentation"} == _PAYLOAD_FIELDS
        assert isinstance(payload["summary"], str) and isinstance(payload["args"], dict)
    by_id = {payload["tool_use_id"]: payload for payload in tools}
    assert by_id["r1"]["summary"] == "docs/notes.md"
    assert by_id["g1"]["summary"] == '"TODO" in docs/'
    assert by_id["g1"]["summary_detail"] == "→ 2 matches"
    assert by_id["m1"]["error_code"] == "not_found"


async def test_a_missing_mention_is_recorded_as_an_unresolved_chip(
    tmp_path, monkeypatch, app_state, processor
):
    monkeypatch.chdir(tmp_path)
    mock = MockTransport()
    mock.queue_response(_completion(0, "There is no such file."))
    events = []
    app_state.conversation.on_event(events.append)
    await _turn(app_state, processor, mock, "@nope.md hello")

    chips = [event.payload["chips"] for event in events if event.kind == "mention_chips"]
    assert chips == [
        [{"raw": "@nope.md", "kind": "unresolved", "ok": False, "reason": "not found"}]
    ]
    assert not [event for event in events if event.kind == "tool_complete"]


async def test_replay_keeps_the_original_times_inside_separators(sessions, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        session_dir = sessions / "s-old"
        session_dir.mkdir(parents=True)
        (session_dir / "conversation.jsonl").write_text(_FIXTURE.read_text(), encoding="utf-8")
        state = TUIAppState.create()
        console = Console(file=StringIO(), record=True, width=120, color_system=None)
        appender = ScrollBufferAppender(state, console)
        appender.mount()
        seen = []
        state.conversation.on_event(seen.append)
        await ConversationReplayer("s-old", state.conversation, object()).run()
        await asyncio.sleep(0)
        appender.flush()
        rows = [row.rstrip() for row in console.export_text().splitlines()]
        now = time.strftime("%H:%M")
    finally:
        os.environ.pop("TZ", None)
        time.tzset()

    # Every replayed event keeps the time it was recorded, not the replay's.
    stored = [json.loads(line)["timestamp"] for line in _FIXTURE.read_text().splitlines()]
    replayed = [event for event in seen if event.kind != "replay_separator"]
    assert [event.timestamp for event in replayed] == stored
    assert rows[0].startswith("── replayed from 1 Jan 05:04 · 15 events ──")
    assert rows[-1].startswith("── now ──")
    if now != "05:04":
        assert not [row for row in rows if now in row]
    assert '● Read memory: "topic_brief"' in rows
    assert not [
        row for row in rows if "Completed" in row or "more lines" in row and "ctrl+t" not in row
    ]
