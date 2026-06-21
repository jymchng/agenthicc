"""Unit tests for PRD-124 Phase 3 — TUI integration."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agenthicc.subagents.pool import WorkerState, SubagentPoolState
from agenthicc.tui.conversation_store import ConversationStore

pytestmark = pytest.mark.unit


# ── ConversationStore signal ──────────────────────────────────────────────────

class TestSubagentPoolStateSignal:
    def test_signal_initialises_to_none(self) -> None:
        conv = ConversationStore()
        assert conv.subagent_pool_state() is None

    def test_signal_accepts_pool_state(self) -> None:
        conv = ConversationStore()
        workers = [WorkerState("explorer #1", "explorer", "pending")]
        ps = SubagentPoolState("pool-1", 1, workers)
        conv.subagent_pool_state.set(ps)
        assert conv.subagent_pool_state() is ps

    def test_signal_accepts_none_reset(self) -> None:
        conv = ConversationStore()
        ps = SubagentPoolState("pool-1", 1, [])
        conv.subagent_pool_state.set(ps)
        conv.subagent_pool_state.set(None)
        assert conv.subagent_pool_state() is None


# ── SubagentPoolState ─────────────────────────────────────────────────────────

class TestSubagentPoolState:
    def test_done_counts_done_and_failed(self) -> None:
        workers = [
            WorkerState("a #1", "explorer", "done"),
            WorkerState("b #1", "tester",   "failed"),
            WorkerState("c #1", "reviewer", "pending"),
            WorkerState("d #1", "planner",  "running"),
        ]
        ps = SubagentPoolState("p", 4, workers)
        assert ps.done == 2

    def test_done_zero_when_all_pending(self) -> None:
        workers = [WorkerState(f"x #{i}", "explorer", "pending") for i in range(3)]
        ps = SubagentPoolState("p", 3, workers)
        assert ps.done == 0

    def test_total_reflects_workers_list(self) -> None:
        ps = SubagentPoolState("p", 5, [])
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
    def _render_event(self, kind: str, payload: dict) -> str:
        from io import StringIO  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.appender import _RENDERERS  # noqa: PLC0415
        from agenthicc.tui.conversation_store import ConversationEvent  # noqa: PLC0415

        ev = ConversationEvent(event_id="x", kind=kind, payload=payload)
        renderer = _RENDERERS.get(kind)
        if renderer is None:
            return ""
        buf = StringIO()
        console = Console(file=buf, highlight=False, markup=False, no_color=True)
        stub = MagicMock()
        stub._console = console
        renderer(stub, ev)
        return buf.getvalue()

    def test_system_renderer_prints_text(self) -> None:
        out = self._render_event("system", {"text": "compacting…"})
        assert "compacting" in out

    def test_system_renderer_empty_payload_is_silent(self) -> None:
        out = self._render_event("system", {})
        assert out.strip() == ""

    def test_pool_started_shows_count(self) -> None:
        out = self._render_event("subagent_pool_started", {
            "total": 3,
            "workers": [
                {"label": "explorer #1", "task": "Find auth files"},
                {"label": "tester #1",   "task": "Write JWT tests"},
                {"label": "reviewer #1", "task": "Review changes"},
            ],
        })
        assert "3" in out or "Spawning" in out

    def test_pool_started_lists_workers(self) -> None:
        out = self._render_event("subagent_pool_started", {
            "total": 2,
            "workers": [
                {"label": "explorer #1", "task": "check auth"},
                {"label": "tester #1",   "task": "write tests"},
            ],
        })
        assert "explorer #1" in out
        assert "tester #1" in out

    def test_worker_done_success(self) -> None:
        out = self._render_event("subagent_worker_done", {
            "label": "explorer #1", "ok": True,
            "done": 1, "total": 3, "duration_ms": 1200,
        })
        assert "explorer #1" in out
        assert "1/3" in out

    def test_worker_done_failure_shows_error(self) -> None:
        out = self._render_event("subagent_worker_done", {
            "label": "tester #1", "ok": False,
            "error": "timeout", "done": 0, "total": 2, "duration_ms": 120000,
        })
        assert "tester #1" in out
        assert "timeout" in out

    def test_pool_done_shows_summary(self) -> None:
        out = self._render_event("subagent_pool_done", {
            "succeeded": 3, "total": 4, "failed": 1,
        })
        assert "3/4" in out or "3" in out

    def test_pool_done_shows_failed_count(self) -> None:
        out = self._render_event("subagent_pool_done", {
            "succeeded": 2, "total": 3, "failed": 1,
        })
        assert "failed" in out.lower() or "1" in out


# ── StatusComponent N/M counter ───────────────────────────────────────────────

class TestStatusComponentSubagentCounter:
    def _make_state(self, pool_state=None) -> MagicMock:
        state = MagicMock()
        state.conversation.frame.return_value = 0
        state.conversation.elapsed_s = 0.0
        state.conversation.model_name.return_value = "test-model"
        state.conversation.session_id.return_value = "s1"
        state.conversation.turn_count.return_value = 0
        state.conversation.cost_usd.return_value = 0.0
        state.conversation.tokens_in.return_value = 0
        state.conversation.tokens_out.return_value = 0
        state.conversation.agent_state.return_value = MagicMock(name="IDLE")
        state.conversation.agent_state().name = "IDLE"
        state.conversation.is_running.return_value = False
        state.conversation.compaction_active.return_value = False
        state.conversation.notification.return_value = None
        state.conversation.workflow_override.return_value = None
        state.conversation.subagent_pool_state.return_value = pool_state
        state.active_mode.return_value = MagicMock(badge="⏵⏵")
        state.workflow_run.return_value = None
        return state

    def test_no_counter_when_pool_is_none(self) -> None:
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import StatusComponent  # noqa: PLC0415
        state = self._make_state(pool_state=None)
        comp = StatusComponent(state)
        console = Console(highlight=False, markup=False, no_color=True, width=120)
        with console.capture() as cap:
            console.print(comp.render())
        assert "subagents" not in cap.get().lower()

    def test_counter_shown_when_pool_active(self) -> None:
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import StatusComponent  # noqa: PLC0415
        workers = [
            WorkerState("explorer #1", "explorer", "done"),
            WorkerState("tester #1",   "tester",   "running"),
            WorkerState("reviewer #1", "reviewer", "pending"),
        ]
        pool = SubagentPoolState("pool-1", 3, workers)
        state = self._make_state(pool_state=pool)
        comp = StatusComponent(state)
        console = Console(highlight=False, markup=False, no_color=True, width=120)
        with console.capture() as cap:
            console.print(comp.render())
        rendered = cap.get()
        assert "subagents" in rendered.lower()
        assert "1/3" in rendered   # 1 done out of 3


# ── FooterComponent worker grid ───────────────────────────────────────────────

class TestFooterComponentWorkerGrid:
    def _render_footer(self, pool_state=None) -> str:
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import FooterComponent  # noqa: PLC0415

        state = MagicMock()
        state.conversation.notification.return_value = None
        state.conversation.agent_state.return_value = MagicMock(name="IDLE")
        state.conversation.agent_state().name = "IDLE"
        state.conversation.workflow_override.return_value = None
        state.conversation.subagent_pool_state.return_value = pool_state
        state.active_mode.return_value = MagicMock(badge="⏵⏵", name="Auto", color="green")
        state.input.paste_condensed.return_value = False
        state.workflow_run.return_value = None

        comp = FooterComponent(state)
        console = Console(highlight=False, markup=False, no_color=True, width=120)
        with console.capture() as cap:
            console.print(comp.render())
        return cap.get()

    def test_worker_grid_hidden_when_no_pool(self) -> None:
        out = self._render_footer(pool_state=None)
        assert "explorer" not in out

    def test_worker_grid_shows_workers(self) -> None:
        workers = [
            WorkerState("explorer #1", "explorer", "running"),
            WorkerState("tester #1",   "tester",   "pending"),
        ]
        pool = SubagentPoolState("p", 2, workers)
        out = self._render_footer(pool_state=pool)
        assert "explorer #1" in out
        assert "tester #1" in out

    def test_footer_height_increases_with_pool(self) -> None:
        from agenthicc.tui.workspace.components import FooterComponent  # noqa: PLC0415

        state = MagicMock()
        state.conversation.notification.return_value = None
        state.conversation.subagent_pool_state.return_value = None
        state.workflow_run.return_value = None
        comp = FooterComponent(state)
        height_no_pool = comp.height(80)

        workers = [WorkerState("x #1", "explorer", "running")]
        pool = SubagentPoolState("p", 1, workers)
        state.conversation.subagent_pool_state.return_value = pool
        height_with_pool = comp.height(80)

        assert height_with_pool == height_no_pool + 1
