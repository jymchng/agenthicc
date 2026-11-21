"""Fixtures for the TUX-09 tests: a session log on disk and a fake terminal."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pyte
import pytest
from rich.console import Console

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.runtime import session_log

__all__ = [
    "FakeBackend",
    "FakeTerminal",
    "event_line",
    "keys",
    "failed_tool",
    "scheduler_session",
    "use_sessions_dir",
    "write_session",
]

_T0 = 1_767_243_843.0

_TOOL_OK = {
    "tool_use_id": "t-ok",
    "name": "read_file",
    "success": True,
    "state": "exited",
    "summary": "src/scheduler.py",
    "summary_detail": "",
    "args": {"path": "src/scheduler.py"},
    "duration_ms": 12,
    "output_lines": ["def due(cursor): ..."],
    "output_head": 1,
    "output_total": 1,
    "exit_code": 0,
    "error_code": None,
}


def failed_tool(
    lines: int = 60, *, command: str = "pytest tests/unit/test_scheduler.py -q"
) -> dict[str, object]:
    """A failed command whose stored output holds *lines* lines (8 head, 32 tail kept)."""
    body = [f"output line {i:03d}" for i in range(lines)]
    kept = body[:8] + body[-32:] if lines > 40 else body
    return {
        "tool_use_id": "t-fail",
        "name": "run_bash",
        "success": False,
        "state": "failed",
        "summary": command,
        "summary_detail": "in repo/",
        "args": {"command": command},
        "duration_ms": 3200,
        "output_lines": kept,
        "output_head": 8 if lines > 40 else len(kept),
        "output_total": lines,
        "exit_code": 1,
        "error_code": None,
    }


def event_line(index: int, kind: str, payload: dict[str, object], at: float | None = None) -> str:
    """One JSON line exactly as ``SessionEventLog`` writes it."""
    record = {
        "event_id": f"e{index:06d}",
        "kind": kind,
        "payload": payload,
        "timestamp": _T0 + index if at is None else at,
    }
    return json.dumps(record)


def write_session(
    root: Path, session_id: str, events: Iterable[tuple[str, dict[str, object]]]
) -> Path:
    """Write a conversation log under *root* (the sessions directory) and return its path."""
    path = root / session_id / "conversation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index, (kind, payload) in enumerate(events):
            handle.write(event_line(index, kind, payload) + "\n")
    return path


def scheduler_session(turns: int = 50) -> list[tuple[str, dict[str, object]]]:
    """*turns* user turns; every third mentions the scheduler, every fifth runs a failing command."""
    events: list[tuple[str, dict[str, object]]] = []
    for turn in range(1, turns + 1):
        topic = "the scheduler" if turn % 3 == 0 else "the parser"
        events.append(("user_message", {"text": f"turn {turn}: look at {topic}"}))
        events.append(("turn_start", {"turn_id": f"t{turn}", "agent": "default", "model": "m"}))
        events.append(("tool_complete", dict(_TOOL_OK)))
        if turn % 5 == 0:
            events.append(("tool_complete", failed_tool()))
        events.append(("text", {"text": f"Done with turn {turn}."}))
        # The store summarises a turn when it closes (TUX-10): it ran its tools.
        events.append(("turn_complete", {"elapsed_s": 4.0, "tools": 2 if turn % 5 == 0 else 1}))
    return events


def use_sessions_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point the session log at *root* (for the tests that call the loaders)."""
    monkeypatch.setattr(session_log, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_log, "_SESSION_INDEX", root / "index.json")


def keys(text: str) -> list[tuple[Key, str]]:
    """Keystrokes typing *text*."""
    return [(Key.CHAR, ch) for ch in text]


class FakeBackend:
    """A terminal backend fed by the test, with the wake-up semantics of the real one.

    ``restore()`` wakes a blocked ``read_key`` with ``OSError`` (the cancel pipe of
    the POSIX backend); ``close()`` drops any wake still pending.
    """

    def __init__(self, interactive: bool = True) -> None:
        self._interactive = interactive
        self._keys: queue.SimpleQueue[tuple[Key, str]] = queue.SimpleQueue()
        self._cond = threading.Condition()
        self._wakes = 0
        self.reporting: list[dict[str, bool | None]] = []
        self.raw_entered = 0

    def feed(self, *entries: tuple[Key, str] | list[tuple[Key, str]]) -> None:
        with self._cond:
            for entry in entries:
                for key in entry if isinstance(entry, list) else [entry]:
                    self._keys.put(key)
            self._cond.notify_all()

    def is_interactive(self) -> bool:
        return self._interactive

    def read_key(self) -> tuple[Key, str]:
        with self._cond:
            while True:
                if self._wakes:
                    self._wakes -= 1
                    raise OSError("woken")
                if not self._keys.empty():
                    return self._keys.get()
                self._cond.wait(0.05)

    def enter_raw_mode(self) -> AbstractContextManager[None]:
        self.raw_entered += 1
        return nullcontext()

    def restore(self) -> None:
        with self._cond:
            self._wakes += 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._wakes = 0

    def query_background(self, timeout: float) -> tuple[int, int, int] | None:
        return None

    def set_reporting(self, *, focus: bool | None = None, mouse: bool | None = None) -> None:
        self.reporting.append({"focus": focus, "mouse": mouse})

    def write_control(self, sequence: str) -> None:
        """Nothing reaches a real terminal from a fake one (TUX-10)."""

    def supports_progress(self) -> bool:
        return False

    def supports_hyperlinks(self) -> bool:
        return False


class FakeTerminal:
    """A console writing to a pyte screen: what the user would see."""

    def __init__(self, width: int = 100, height: int = 30) -> None:
        import io

        self.out = io.StringIO()
        self.console = Console(
            file=self.out,
            force_terminal=True,
            width=width,
            height=height,
            color_system="truecolor",
            highlight=False,
            emoji=False,
        )
        self.width, self.height = width, height

    @property
    def raw(self) -> str:
        return self.out.getvalue()

    def screen(self) -> list[str]:
        screen = pyte.Screen(self.width, self.height)
        pyte.Stream(screen).feed(self.raw)
        return [line.rstrip() for line in screen.display]

    def text(self) -> str:
        return "\n".join(self.screen())
