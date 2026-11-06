"""Unit tests for PRD-124 Phase 3 — TUI integration."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agenthicc.subagents.pool import WorkerState, SubagentPoolState
from agenthicc.tui.conversation_store import AppState, ConversationStore

pytestmark = pytest.mark.unit


# ── ConversationStore signal ──────────────────────────────────────────────────


class TestSubagentPoolStateSignal:
    def test_signal_initialises_to_none(self) -> None:
        conv = ConversationStore()
        assert conv.subagent_pool_state() is None

    def test_signal_accepts_pool_state(self) -> None:
        conv = ConversationStore()
        workers = (WorkerState("explorer #1", "explorer", "pending"),)
        ps = SubagentPoolState("pool-1", 1, workers)
        conv.subagent_pool_state.set(ps)
        assert conv.subagent_pool_state() is ps

    def test_signal_accepts_none_reset(self) -> None:
        conv = ConversationStore()
        ps = SubagentPoolState("pool-1", 1, ())
        conv.subagent_pool_state.set(ps)
        conv.subagent_pool_state.set(None)
        assert conv.subagent_pool_state() is None


# ── SubagentPoolState ─────────────────────────────────────────────────────────


class TestSubagentPoolState:
    def test_done_counts_done_and_failed(self) -> None:
        workers = (
            WorkerState("a #1", "explorer", "done"),
            WorkerState("b #1", "tester", "failed"),
            WorkerState("c #1", "reviewer", "pending"),
            WorkerState("d #1", "planner", "running"),
        )
        ps = SubagentPoolState("p", 4, workers)
        assert ps.done == 2

    def test_done_zero_when_all_pending(self) -> None:
        workers = tuple(WorkerState(f"x #{i}", "explorer", "pending") for i in range(3))
        ps = SubagentPoolState("p", 3, workers)
        assert ps.done == 0

    def test_total_reflects_workers_list(self) -> None:
        ps = SubagentPoolState("p", 5, ())
        assert ps.total == 5


# ── EventKind additions ───────────────────────────────────────────────────────


class TestEventKindAdditions:
    def test_new_kinds_accepted_by_append_event(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        # Should not raise
        conv.append_event("subagent_pool_started", {"total": 3})
        conv.append_event("subagent_worker_done", {"label": "explorer #1", "ok": True})
        conv.append_event("subagent_pool_done", {"succeeded": 3, "total": 3})
        conv.append_event("subagent_pool_result", {"fingerprint": "abc", "text": "result"})
        conv.append_event("system", {"text": "generic message"})


# ── Scroll buffer renderers ───────────────────────────────────────────────────


class TestScrollBufferRenderers:
    def _render_event(self, kind: str, payload: dict, *, width: int = 80) -> str:
        from io import StringIO
        from rich.console import Console
        from agenthicc.tui.workspace.appender import _RENDERERS
        from agenthicc.tui.conversation_store import ConversationEvent

        ev = ConversationEvent(event_id="x", kind=kind, payload=payload)
        renderer = _RENDERERS.get(kind)
        if renderer is None:
            return ""
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False, no_color=True, width=width)
        stub = MagicMock()
        stub._console = console
        renderer(stub, ev)
        return buf.getvalue()

    def test_system_renderer_prints_text(self) -> None:
        out = self._render_event("system", {"text": "⎋ Compacting conversation…"})
        assert out.splitlines()[0].startswith("⎋ Compacting")

    def test_system_renderer_indents_completed_compaction_notice(self) -> None:
        out = self._render_event("system", {"text": "⎋ Compacted → ~215 tokens"})
        assert out.splitlines()[0].startswith("  ⎋ Compacted")

    def test_system_renderer_empty_payload_is_silent(self) -> None:
        out = self._render_event("system", {})
        assert out.strip() == ""

    def test_pool_started_shows_count(self) -> None:
        out = self._render_event(
            "subagent_pool_started",
            {
                "total": 3,
                "workers": [
                    {"label": "explorer #1", "task": "Find auth files"},
                    {"label": "tester #1", "task": "Write JWT tests"},
                    {"label": "reviewer #1", "task": "Review changes"},
                ],
            },
        )
        assert "3" in out or "Spawning" in out
        assert out.splitlines()[0].startswith("◈ Subagents")

    def test_pool_started_lists_workers(self) -> None:
        out = self._render_event(
            "subagent_pool_started",
            {
                "total": 2,
                "workers": [
                    {"label": "explorer #1", "task": "check auth"},
                    {"label": "tester #1", "task": "write tests"},
                ],
            },
        )
        assert "explorer #1" in out
        assert "tester #1" in out
        assert any(row.startswith("  └") and "explorer #1" in row for row in out.splitlines())

    def test_pool_started_bounds_previews_and_fits_each_row(self) -> None:
        from agenthicc.tui.rendering import visible_len

        workers = [
            {
                "label": f"reviewer #{index}",
                "task": f"Review a long task target under section {index}",
            }
            for index in range(1, 6)
        ]
        out = self._render_event(
            "subagent_pool_started",
            {"total": 5, "workers": workers},
            width=40,
        )
        rows = out.splitlines()

        assert all(visible_len(row) <= 40 for row in rows)
        assert sum("reviewer #" in row for row in rows) == 3
        assert "+2 more" in out and "Ctrl+O details" in out
        assert "section 4" not in out and "section 5" not in out

    def test_worker_done_success(self) -> None:
        out = self._render_event(
            "subagent_worker_done",
            {
                "label": "explorer #1",
                "ok": True,
                "done": 1,
                "total": 3,
                "duration_ms": 1200,
            },
        )
        assert "explorer #1" in out
        assert "1/3" in out
        assert out.splitlines()[0].startswith("✓ [1/3]")

    def test_worker_done_success_shows_changed_paths(self) -> None:
        out = self._render_event(
            "subagent_worker_done",
            {
                "label": "implementer #1",
                "ok": True,
                "done": 1,
                "total": 1,
                "duration_ms": 1200,
                "changed_paths": ["chapters/01.md"],
            },
        )
        assert "Modified chapters/01.md" in out

    def test_worker_done_success_shows_the_first_line_of_its_text(self) -> None:
        out = self._render_event(
            "subagent_worker_done",
            {"label": "explorer #1", "ok": True, "done": 1, "total": 1, "text": "Found it\nmore"},
        )
        assert "Found it" in out and "more" not in out

    def test_worker_done_failure_shows_error(self) -> None:
        out = self._render_event(
            "subagent_worker_failed",
            {
                "label": "tester #1",
                "ok": False,
                "error": "timeout",
                "done": 0,
                "total": 2,
                "duration_ms": 120000,
            },
        )
        assert "tester #1" in out
        assert "timeout" in out
        assert out.splitlines()[0].startswith("✗ [0/2]")

    def test_pool_done_shows_summary(self) -> None:
        out = self._render_event(
            "subagent_pool_done",
            {
                "succeeded": 3,
                "total": 4,
                "failed": 1,
            },
        )
        assert "3/4" in out or "3" in out
        assert out.splitlines()[0].startswith("◈ Subagents Failed")
        assert out.endswith("\n\n")

    def test_pool_done_marks_incomplete_pool_failed(self) -> None:
        out = self._render_event(
            "subagent_pool_done",
            {"succeeded": 0, "total": 2, "failed": 0},
        )
        assert out.splitlines()[0].startswith("◈ Subagents Failed")

    def test_pool_done_marks_all_success_complete(self) -> None:
        out = self._render_event(
            "subagent_pool_done",
            {"succeeded": 2, "total": 2, "failed": 0},
        )
        assert out.splitlines()[0].startswith("◈ Subagents complete")
        assert out.endswith("\n\n")

    def test_pool_done_shows_failed_count(self) -> None:
        out = self._render_event(
            "subagent_pool_done",
            {
                "succeeded": 2,
                "total": 3,
                "failed": 1,
            },
        )
        assert "failed" in out.lower() or "1" in out

    def test_pool_done_shows_budget_exceeded_reason(self) -> None:
        out = self._render_event(
            "subagent_pool_done",
            {
                "succeeded": 2,
                "total": 2,
                "failed": 0,
                "budget_exceeded_reason": "max_tokens_exceeded",
            },
        )
        assert out.splitlines()[0].startswith("◈ Subagents Failed")
        assert "budget exceeded: max tokens exceeded" in out.lower()


# ── StatusLine N/M counter and the composer's subagent hint (TUX-02) ─────────


def _app_state(pool_state: SubagentPoolState | None = None) -> AppState:
    state = AppState.create()
    state.conversation.model_name.set("test-model")
    state.conversation.subagent_pool_state.set(pool_state)
    return state


class TestStatusLineSubagentCounter:
    def _line(self, pool_state: SubagentPoolState | None) -> str:
        from rich.text import Text

        from agenthicc.tui.workspace.components import StatusLine

        return Text.from_markup(StatusLine(_app_state(pool_state)).markup(120)).plain

    def test_no_counter_when_pool_is_none(self) -> None:
        assert "workers" not in self._line(None)

    def test_counter_shown_when_pool_active(self) -> None:
        workers = (
            WorkerState("explorer #1", "explorer", "done"),
            WorkerState("tester #1", "tester", "running"),
            WorkerState("reviewer #1", "reviewer", "pending"),
        )
        assert "1/3 workers" in self._line(SubagentPoolState("pool-1", 3, workers))


class TestComposerSubagentHint:
    def _hint(self, pool_state: SubagentPoolState | None = None) -> str:
        from agenthicc.tui.workspace.components import ComposerComponent, ComposerFrame

        state = _app_state(pool_state)
        return ComposerFrame(state, ComposerComponent(state)).hint_row(200) or ""

    def test_hint_row_lists_no_workers_but_offers_the_panel_key(self) -> None:
        # Workers are the subagent panel's (AGX-08); the hint row offers its key.
        workers = (WorkerState("explorer #1", "explorer", "running", worker_id="w"),)
        with_pool = self._hint(SubagentPoolState("p", 1, workers))
        assert "explorer #1" not in with_pool
        assert "Ctrl+O" in with_pool and "subagents" in with_pool
        assert "Ctrl+O" not in self._hint()

    def test_status_line_height_does_not_grow_with_a_pool(self) -> None:
        from agenthicc.tui.workspace.components import StatusLine

        workers = (WorkerState("x #1", "explorer", "running", worker_id="w"),)
        assert StatusLine(_app_state()).height(80) == 1
        assert StatusLine(_app_state(SubagentPoolState("p", 1, workers))).height(80) == 1
