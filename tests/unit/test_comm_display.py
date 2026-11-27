"""How the terminal communications read to the operator.

Two things reach the agent that the operator also has to be able to read: the
notice that a background terminal ended, and the receipt of a command moved to the
background.  Each must say what it is, not read as something the operator typed,
and not show raw JSON.  (A worker's mail is covered with the rest of COMM-03 in
``test_worker_mail_boundary.py``.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.text import Text

from agenthicc.background.model import BackgroundSession
from agenthicc.subagents.delivery import deliver_notices
from agenthicc.tui import tool_summary as ts
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.workspace.agents_hub.detail import (
    TERMINAL_ROLE,
    DetailData,
    TranscriptLine,
    read_transcript,
    transcript_line,
)
from agenthicc.tui.workspace.agents_hub.render import detail_lines
from tests.unit.test_terminal_completion_notices import _conversation, _manager, _run, _source

pytestmark = pytest.mark.unit

SUMMARY = "Terminal exited successfully — build · term-ab12cd34"


# ── the hub's transcript of a session ────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "glyph"),
    [
        ("exited", "✓"),
        ("failed", "✗"),
        ("timed_out", "✗"),
        ("orphaned", "✗"),
        ("stopped", "●"),
        ("cancelled", "●"),
    ],
)
def test_a_terminal_notice_is_a_terminals_row_marked_by_how_it_ended(
    state: str, glyph: str
) -> None:
    line = transcript_line(
        "user_message",
        {
            "text": "Background terminal term-ab12cd34 (build) exited…\n--- stdout ---\nlots of output",
            "source": "terminal_notice",
            "summary": SUMMARY,
            "state": state,
        },
    )

    assert line == TranscriptLine(TERMINAL_ROLE, f"agent notified · {SUMMARY}", glyph)


def test_a_terminal_notice_is_never_the_operators_own_message() -> None:
    notice = transcript_line("user_message", {"text": "ended", "source": "terminal_notice"})
    typed = transcript_line("user_message", {"text": "ended"})

    assert notice is not None and notice.role == TERMINAL_ROLE
    assert typed == TranscriptLine("you", "ended")


async def test_a_real_notice_reaches_the_hub_transcript_as_one_readable_row(
    tmp_path: Path,
) -> None:
    manager, conversation = _manager(tmp_path), _conversation(tmp_path)
    store = AppState.create().conversation
    try:
        record = await _run(manager, "print('compiling'); raise SystemExit(2)", label="build")
        (notice,) = _source(conversation, manager).claim()
        deliver_notices((notice,), conversation.memory, store)  # type: ignore[arg-type]
        event = store._events_by_id[notice.message_id]
    finally:
        conversation.close()
        await manager.close()

    # The journal line the hub reads is the event's kind and payload.
    path = tmp_path / "conversation.jsonl"
    path.write_text(
        json.dumps({"kind": "user_message", "payload": {"text": "go"}})
        + "\n"
        + json.dumps({"kind": event.kind, "payload": event.payload})
        + "\n",
        encoding="utf-8",
    )
    lines = read_transcript(path, tail_bytes=1_000_000)

    assert [(line.role, line.status) for line in lines] == [("you", ""), (TERMINAL_ROLE, "✗")]
    assert lines[1].text.startswith("agent notified · Terminal failed with exit code 2")
    assert record.terminal_id in lines[1].text
    assert "compiling" not in lines[1].text  # the process's output stays in the model's notice

    session = BackgroundSession.create(
        "s", title="t", cwd=str(tmp_path), workflow_name="", intent="x"
    )
    view = detail_lines(session, DetailData("s", 1, transcript=lines), None, 100, now=0.0)
    shown = "\n".join(Text.from_markup(str(line)).plain for line in view.lines)
    assert "terminals" in shown and "agent notified" in shown and "exit code 2" in shown


# ── the receipt of a command moved to the background ─────────────────────────


def test_a_command_moved_to_the_background_shows_a_status_line_not_json() -> None:
    receipt = {
        "ok": True,
        "background": True,
        "backgrounded": True,
        "terminal_id": "term-ab12cd34",
        "state": "running",
        "pid": 4242,
        "stdout_so_far": "compiling…\nlinking…\n",
        "stderr_so_far": "",
        "message": "The user moved this command to the background; …",
    }

    projection = ts.output_projection(receipt)

    assert projection.lines == (
        "moved to the background · term-ab12cd34 · running",
        "compiling…",
        "linking…",
    )
    assert not any(line.lstrip().startswith("{") for line in projection.lines)


def test_a_plain_background_start_reads_as_started() -> None:
    projection = ts.output_projection(
        {"ok": True, "background": True, "terminal_id": "term-1", "state": "running", "pid": 7}
    )

    assert projection.lines == ("started · term-1 · running",)


def test_a_finished_terminals_result_and_a_refusal_are_not_mistaken_for_a_receipt() -> None:
    finished = ts.output_projection(
        {
            "background": True,
            "terminal_id": "term-1",
            "state": "exited",
            "stdout": "ok\n",
            "stderr": "",
        }
    )
    refused = ts.output_projection(
        {"ok": False, "background": True, "state": "rejected", "error": "terminal limit reached"}
    )

    assert finished.lines == ("ok",)
    assert refused.lines == ("terminal limit reached",)


# ── a message another session sent ───────────────────────────────────────────


def test_a_message_from_another_session_is_a_sessions_row_not_the_operator(tmp_path: Path) -> None:
    from agenthicc.background.session_messages import frame_session_message, input_event_payload
    from agenthicc.tui.workspace.agents_hub.detail import SESSION_ROLE

    framed = frame_session_message(
        "s-farm-1",
        "build farm",
        "The schema moved to v2.\nRebase your tests on it.",
        message_id="m1",
    )
    # What the worker records for it, and what the hub reads back from the journal.
    payload = input_event_payload(framed)
    path = tmp_path / "conversation.jsonl"
    path.write_text(
        json.dumps({"kind": "user_message", "payload": {"text": "please continue"}})
        + "\n"
        + json.dumps({"kind": "user_message", "payload": payload})
        + "\n",
        encoding="utf-8",
    )

    lines = read_transcript(path, tail_bytes=1_000_000)

    assert [(line.role, line.status) for line in lines] == [("you", ""), (SESSION_ROLE, "●")]
    assert lines[1].text == "from s-farm-1 (build farm) · The schema moved to v2."
    assert "UNTRUSTED" not in lines[1].text  # the header is for the model, not this row

    session = BackgroundSession.create(
        "s", title="t", cwd=str(tmp_path), workflow_name="", intent="x"
    )
    view = detail_lines(session, DetailData("s", 1, transcript=lines), None, 100, now=0.0)
    shown = "\n".join(Text.from_markup(str(line)).plain for line in view.lines)
    assert "sessions" in shown and "from s-farm-1 (build farm)" in shown


def test_a_session_message_whose_header_is_not_first_is_the_operators_text() -> None:
    from agenthicc.background.session_messages import frame_session_message

    pasted = "FYI:\n" + frame_session_message("s-1", "", "hi", message_id="m1")

    line = transcript_line("user_message", {"text": pasted, "source": "agents-manager"})

    assert line is not None and line.role == "you"
