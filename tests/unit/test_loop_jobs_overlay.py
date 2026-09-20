"""Unit coverage for the interactive ``/loops`` job table."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from agenthicc.runners.loop_scheduler import LoopLifecycle, LoopPayloadKind, LoopRecord
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.loops import LoopJobsOverlay


def _record(loop_id: str, session_id: str, payload: str) -> LoopRecord:
    return LoopRecord(
        loop_id=loop_id,
        session_id=session_id,
        conversation_id=session_id,
        payload_kind=(
            LoopPayloadKind.COMMAND if payload.startswith("/") else LoopPayloadKind.PROMPT
        ),
        payload=payload,
        interval_s=60,
        created_at=1.0,
        updated_at=float(int(loop_id[-1], 16) + 1),
        next_due_at=100.0,
        expires_at=1_000.0,
        state=LoopLifecycle.SCHEDULED,
    )


def test_loop_jobs_overlay_renders_a_table_and_enter_runs_selected_job() -> None:
    records = [
        _record("loop-1", "session-1", "inspect one"),
        _record("loop-2", "session-2", "/status"),
    ]
    closed: list[bool] = []
    run: list[str] = []
    deleted: list[str] = []
    overlay = LoopJobsOverlay(
        lambda: records,
        lambda: closed.append(True),
        lambda record: run.append(record.loop_id) or "run requested",
        lambda record: deleted.append(record.loop_id) or "deleted",
        session_id="session-1",
    )

    rendered = StringIO()
    Console(file=rendered, force_terminal=False, width=140).print(overlay.render())
    output = rendered.getvalue()
    assert "Scheduled Loops" in output
    assert "loop-1" in output
    assert "current" in output
    assert "session-2" in output

    overlay.handle_key(Key.DOWN, "")
    overlay.handle_key(Key.ENTER, "")
    assert run == ["loop-1"]
    assert closed == [True]
    assert deleted == []


def test_loop_jobs_overlay_requires_delete_confirmation() -> None:
    records = [_record("loop-a", "session-a", "inspect")]
    closed: list[bool] = []
    deleted: list[str] = []
    overlay = LoopJobsOverlay(
        lambda: records,
        lambda: closed.append(True),
        lambda record: "run",
        lambda record: deleted.append(record.loop_id) or "deleted",
        session_id="session-a",
    )

    overlay.handle_key(Key.CHAR, "d")
    assert deleted == []
    overlay.handle_key(Key.ENTER, "")
    assert deleted == ["loop-a"]
    assert closed == []
