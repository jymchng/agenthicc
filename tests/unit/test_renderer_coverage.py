"""BEAU-1 / TUX-06: every registered event kind has a renderer and a snapshot.

Each case renders through ``ScrollBufferAppender`` at 80 columns in all three
tool detail levels and is compared with ``tests/fixtures/tux06/snapshots``.
A new renderer must add a case here in the same change that registers it.

Regenerate the snapshots after an intended visual change with::

    AGENTHICC_UPDATE_SNAPSHOTS=1 pytest tests/unit/test_renderer_coverage.py
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace.appender import (
    _RENDERERS,
    TOOL_DETAIL_LEVELS,
    ScrollBufferAppender,
    set_session_tool_detail,
)

pytestmark = pytest.mark.unit

_SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "tux06" / "snapshots"
_T0 = 1_767_243_843.0  # 2026-01-01 05:04:03 UTC

_TOOL = {
    "tool_use_id": "t",
    "name": "run_bash",
    "success": True,
    "state": "exited",
    "ready": None,
    "summary": "uv run pytest -q",
    "summary_detail": "in agenthicc/",
    "args": {"command": "cd agenthicc && uv run pytest -q"},
    "duration_ms": 2_400,
    "output_lines": ["collected 3 items", "", "...", "3 passed in 0.10s"],
    "output_head": 4,
    "output_total": 4,
    "exit_code": 0,
    "error_code": None,
}

#: One or more payloads per kind: the second form exercises another branch.
CASES: dict[str, list[dict[str, object]]] = {
    "turn_start": [{"agent": "reviewer", "model": "sonnet-4.6", "agent_name": "x"}],
    "user_message": [{"text": "Fix the failing tests in @src/app.py please"}],
    "text": [{"text": "I'll run the tests first."}],
    "thinking_step": [{"step": "Comparing the recovery flow", "done": False}],
    "mention_chips": [
        {
            "chips": [
                {"raw": "@src/app.py", "kind": "file", "ok": True},
                {
                    "raw": "@docs/missing.md",
                    "kind": "unresolved",
                    "ok": False,
                    "reason": "not found",
                },
            ]
        }
    ],
    "tool_complete": [
        _TOOL,
        {
            **_TOOL,
            "state": "failed",
            "exit_code": 1,
            "output_lines": ["FAIL src/App.test.tsx", "  Unable to find the text"],
            "output_head": 2,
            "output_total": 2,
        },
    ],
    "file_modified": [
        {
            "path": "src/app.py",
            "old_lines": ["a = 1", "b = 2"],
            "new_lines": ["a = 1", "b = 3"],
            "tool": "patch_file",
        },
        {"path": "src/new.py", "old_lines": [], "new_lines": ["x = 1"], "tool": "write_file"},
    ],
    "error": [{"message": "Agent turn failed", "detail": "Provider returned HTTP 400"}],
    "system": [{"text": "Compacted 12 messages"}],
    "announcement": [{"text": "Agent finished."}],
    "tool_recovery": [{"text": "Tool execution was interrupted."}],
    "mcp_server_failure": [{"text": "MCP server github is unavailable.", "required": False}],
    "network_retry": [
        {"attempt": 2, "max_retries": 5, "delay_s": 1.0, "reason": "Timeout", "status_code": 503}
    ],
    "provider_recovery_retry": [
        {"attempt": 1, "max_retries": 3, "delay_s": 2.0, "status_code": 400}
    ],
    "goal_list_mutated": [{"operation": "append", "index": 2, "goal_count": 3}],
    "phase_control_requested": [
        {"operation": "postpone", "phase_id": "build", "reason": "waiting"}
    ],
    "phase_plan_mutated": [{"operation": "skip", "phase_id": "build", "disposition": "skipped"}],
    "phase_control_rejected": [
        {"operation": "delete", "phase_id": "build", "error_code": "active"}
    ],
    "turn_complete": [{"elapsed_s": 53.0}],
    "activity_complete": [{"elapsed_s": 54.0}],
    "replay_separator": [{"edge": "start", "since": _T0, "events": 827}, {"edge": "end"}],
    "subagent_pool_started": [
        {"total": 2, "workers": [{"label": "Reviewer 1", "task": "Review PRDs"}]}
    ],
    "subagent_worker_done": [
        {
            "ok": True,
            "label": "Reviewer 1",
            "done": 1,
            "total": 2,
            "duration_ms": 12_400,
            "text": "Found 4 gaps",
        }
    ],
    "subagent_worker_failed": [
        {"ok": False, "label": "Reviewer 2", "done": 2, "total": 2, "error": "test failed"}
    ],
    "subagent_transcript_degraded": [{"transcript_write_failures": 2}],
    "subagent_pool_done": [{"succeeded": 1, "total": 2, "failed": 1}],
}


@pytest.fixture(autouse=True)
def _utc() -> Iterator[None]:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()
    set_session_tool_detail(None)


def _render(kind: str, level: str) -> str:
    console = Console(file=StringIO(), record=True, width=80, color_system=None)
    appender = ScrollBufferAppender(AppState.create(), console)
    set_session_tool_detail(level)
    for index, payload in enumerate(CASES[kind]):
        appender._render_one(ConversationEvent(f"{kind}-{index}", kind, dict(payload), _T0 + index))
    appender.flush()
    return "\n".join(row.rstrip() for row in console.export_text().splitlines())


def _snapshot(kind: str) -> str:
    # One final newline, as the repository's end-of-file hook keeps it.
    text = "\n".join(f"── {level} ──\n{_render(kind, level)}\n" for level in TOOL_DETAIL_LEVELS)
    return text.rstrip("\n") + "\n"


def test_every_registered_kind_has_a_case() -> None:
    assert set(CASES) == set(_RENDERERS)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_every_kind_matches_its_snapshot_in_all_three_detail_levels(kind: str) -> None:
    path = _SNAPSHOTS / f"{kind}.txt"
    rendered = _snapshot(kind)
    if os.environ.get("AGENTHICC_UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.is_file(), f"missing snapshot {path.name}; set AGENTHICC_UPDATE_SNAPSHOTS=1"
    assert rendered == path.read_text(encoding="utf-8")
    for level in TOOL_DETAIL_LEVELS:
        assert _render(kind, level).strip(), f"{kind} renders nothing at {level}"


def test_every_snapshot_belongs_to_a_registered_kind() -> None:
    assert {path.stem for path in _SNAPSHOTS.glob("*.txt")} <= set(_RENDERERS)
