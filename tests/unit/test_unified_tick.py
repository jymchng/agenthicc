"""Tests for unified frame counter (PRD-120)."""

from __future__ import annotations

import pytest
from agenthicc.tui.conversation_store import AppState, ConversationStore, AgentState


pytestmark = pytest.mark.unit


class TestFrameSignal:
    def test_initial_frame_is_zero(self) -> None:
        conv = ConversationStore()
        assert conv.frame() == 0

    def test_tick_increments_frame(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.tick()
        assert conv.frame() == 1

    def test_tick_does_not_increment_frame_when_idle(self) -> None:
        conv = ConversationStore()
        assert conv.agent_state() == AgentState.IDLE
        for _ in range(5):
            conv.tick()
        assert conv.frame() == 0

    @pytest.mark.parametrize("state", [AgentState.COMPLETE, AgentState.ERROR])
    def test_tick_does_not_increment_frame_for_static_terminal_states(
        self, state: AgentState
    ) -> None:
        conv = ConversationStore()
        conv.agent_state.set(state)
        changes: list[int] = []
        conv.frame.subscribe(lambda: changes.append(conv.frame()))

        conv.tick()

        assert conv.frame() == 0
        assert changes == []

    def test_idle_tick_does_not_notify_frame_subscribers(self) -> None:
        conv = ConversationStore()
        changes: list[int] = []
        conv.frame.subscribe(lambda: changes.append(conv.frame()))

        conv.tick()
        conv.tick()

        assert changes == []

    def test_tick_increments_during_compaction(self) -> None:
        conv = ConversationStore()
        conv.compaction_active.set(True)
        for _ in range(3):
            conv.tick()
        assert conv.frame() == 3

    @pytest.mark.parametrize(
        "state",
        [AgentState.THINKING, AgentState.RUNNING, AgentState.RECOVERING],
    )
    def test_tick_increments_for_animated_agent_states(self, state: AgentState) -> None:
        conv = ConversationStore()
        conv.agent_state.set(state)
        conv.tick()
        assert conv.frame() == 1

    def test_tick_stays_stable_while_user_prompt_is_pending(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.tick()
        conv.tick(paused=True)
        conv.tick(paused=True)
        assert conv.frame() == 1

    def test_frame_monotonically_increases(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        values: list[int] = []
        for _ in range(10):
            conv.tick()
            values.append(conv.frame())
        assert values == list(range(1, 11))

    def test_frame_never_resets_between_turns(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("a", "t1")
        for _ in range(5):
            conv.tick()
        frame_mid = conv.frame()
        conv.close_turn()
        conv.begin_turn("a", "t2")
        conv.tick()
        assert conv.frame() == frame_mid + 1

    def test_no_compact_tick_attribute(self) -> None:
        conv = ConversationStore()
        assert not hasattr(conv, "compact_tick")

    def test_no_thinking_frame_attribute(self) -> None:
        conv = ConversationStore()
        assert not hasattr(conv, "_thinking_frame")

    def test_no_flower_frame_attribute(self) -> None:
        conv = ConversationStore()
        assert not hasattr(conv, "_flower_frame")


class TestElapsedSProperty:
    def test_elapsed_s_zero_when_idle(self) -> None:
        conv = ConversationStore()
        assert conv.elapsed_s == 0.0

    def test_elapsed_s_is_float(self) -> None:
        conv = ConversationStore()
        assert isinstance(conv.elapsed_s, float)

    def test_elapsed_s_positive_during_turn(self) -> None:
        import time  # noqa: PLC0415

        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        time.sleep(0.05)
        assert conv.elapsed_s > 0.0

    def test_elapsed_s_resets_after_turn_ends(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        conv.close_turn()
        assert conv.elapsed_s == 0.0

    def test_elapsed_s_is_not_a_signal(self) -> None:
        from agenthicc.reactive import Signal  # noqa: PLC0415

        conv = ConversationStore()
        assert not isinstance(conv.elapsed_s, Signal)

    def test_elapsed_s_is_not_callable(self) -> None:
        conv = ConversationStore()
        assert not callable(conv.elapsed_s)

    def test_display_clock_excludes_prompt_wait_but_wall_clock_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 100.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")

        now = 101.0
        conv.tick()
        assert conv.display_elapsed_s() == pytest.approx(1.0)

        conv.set_display_paused(True)
        now = 111.0
        # Rendering and paused ticks cannot charge the ten-second wait.
        assert conv.display_elapsed_s() == pytest.approx(1.0)
        conv.tick()
        assert conv.display_elapsed_s() == pytest.approx(1.0)
        assert conv.elapsed_s == pytest.approx(11.0)

        # Repeated state edges are idempotent.  The first active tick establishes
        # the new baseline; only subsequent active time is displayed.
        conv.set_display_paused(True)
        conv.set_display_paused(False)
        conv.tick()
        now = 112.0
        conv.tick()
        assert conv.display_elapsed_s() == pytest.approx(2.0)

    def test_paused_ticks_do_not_publish_activity_timer_updates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 100.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")

        now = 101.0
        conv.tick()
        updates: list[float] = []
        conv.activity_elapsed_s.subscribe(lambda: updates.append(conv.activity_elapsed_s()))

        conv.set_display_paused(True)
        now = 111.0
        conv.tick()
        now = 113.0
        conv.tick()

        assert updates == []
        assert conv.activity_elapsed_s() == pytest.approx(1.0)
        assert conv.display_elapsed_s() == pytest.approx(1.0)

        # The first active tick publishes the complete wall-clock duration
        # once, while the display clock resumes from its paused baseline.
        conv.set_display_paused(False)
        assert updates == [pytest.approx(13.0)]
        now = 114.0
        conv.tick()
        assert updates == [pytest.approx(13.0), pytest.approx(14.0)]
        assert conv.activity_elapsed_s() == pytest.approx(14.0)
        assert conv.display_elapsed_s() == pytest.approx(2.0)

    def test_pending_signal_controls_display_pause_without_tick_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 100.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        app_state = AppState.create()
        conv = app_state.conversation
        conv.begin_turn("agent", "t1")
        now = 101.0
        conv.tick()
        before = conv.display_elapsed_s()

        app_state.pending_approval.set(object())  # type: ignore[arg-type]
        conv.tick()  # ``None`` uses the authoritative pending state.
        assert conv.display_elapsed_s() == before

        app_state.pending_approval.set(object())  # replacement remains paused
        conv.tick()
        assert conv.display_elapsed_s() == before
        app_state.pending_approval.set(None)
        now = 102.0
        conv.tick()
        assert conv.display_elapsed_s() > before

    def test_turn_complete_retains_wall_clock_duration_during_prompt_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 200.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        now = 202.0
        conv.tick()
        conv.set_display_paused(True)
        now = 222.0
        conv.tick()
        events = []
        conv.on_event(events.append)
        conv.close_turn()

        completion = [event for event in events if event.kind == "turn_complete"]
        assert len(completion) == 1
        assert completion[0].payload["elapsed_s"] == pytest.approx(22.0)
        assert conv.display_elapsed_s() == 0.0

    def test_activity_clock_accumulates_across_internal_turns_until_idle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 100.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        conv = ConversationStore()

        # TUISession owns this outer scope while a workflow may run several
        # individual LLM turns.
        conv.begin_activity()
        conv.begin_turn("agent", "phase-1")
        now = 103.0
        conv.tick()
        assert conv.activity_elapsed_s() == pytest.approx(3.0)

        conv.close_turn()
        assert conv.agent_state() != AgentState.IDLE
        now = 108.0
        conv.tick(paused=True)  # no turn, but outer activity continues through a wait
        assert conv.activity_elapsed_s() == pytest.approx(3.0)
        now = 110.0
        conv.tick(paused=True)
        assert conv.activity_elapsed_s() == pytest.approx(3.0)

        conv.begin_turn("agent", "phase-2")
        now = 111.0
        conv.tick()
        assert conv.activity_elapsed_s() == pytest.approx(11.0)
        conv.close_turn()
        assert conv.activity_elapsed_s() == pytest.approx(11.0)

        now = 114.0
        total = conv.end_activity()
        assert total == pytest.approx(14.0)
        assert conv.agent_state() == AgentState.IDLE
        assert conv.activity_elapsed_s() == 0.0

    def test_activity_clock_resets_at_the_next_idle_to_active_edge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 50.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        conv = ConversationStore()
        conv.begin_activity()
        now = 55.0
        conv.tick()
        assert conv.activity_elapsed_s() == pytest.approx(5.0)
        conv.end_activity()

        now = 90.0
        conv.begin_activity()
        conv.tick()
        assert conv.activity_elapsed_s() == pytest.approx(0.0)

    def test_activity_completion_emits_total_duration_before_reset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 100.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        conv = ConversationStore()
        events = []
        conv.on_event(events.append)

        conv.begin_activity()
        now = 107.5
        total = conv.end_activity()

        completion = [event for event in events if event.kind == "activity_complete"]
        assert total == pytest.approx(7.5)
        assert len(completion) == 1
        assert completion[0].payload["elapsed_s"] == pytest.approx(7.5)
        assert conv.agent_state() == AgentState.IDLE
        assert conv.activity_elapsed_s() == 0.0

    def test_status_renders_total_activity_across_internal_turns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from io import StringIO  # noqa: PLC0415

        from rich.console import Console  # noqa: PLC0415

        from agenthicc.tui.workspace.components import StatusComponent  # noqa: PLC0415

        import agenthicc.tui.conversation_store as conversation_store_module  # noqa: PLC0415

        now = 10.0
        monkeypatch.setattr(conversation_store_module.time, "monotonic", lambda: now)
        app_state = AppState.create()
        conv = app_state.conversation
        conv.model_name.set("test-model")
        conv.begin_activity()
        conv.begin_turn("agent", "phase-1")

        now = 13.0
        conv.tick()
        first = StringIO()
        Console(file=first, force_terminal=False, markup=False, width=120).print(
            StatusComponent(app_state).render()
        )
        assert "Thinking" in first.getvalue()
        assert "3s" in first.getvalue()

        conv.close_turn()
        now = 18.0
        conv.tick()
        conv.begin_turn("agent", "phase-2")
        second = StringIO()
        Console(file=second, force_terminal=False, markup=False, width=120).print(
            StatusComponent(app_state).render()
        )
        assert "Thinking" in second.getvalue()
        assert "8s" in second.getvalue()

        conv.close_turn()
        conv.end_activity()
        idle = StringIO()
        Console(file=idle, force_terminal=False, markup=False, width=120).print(
            StatusComponent(app_state).render()
        )
        assert "Thinking" not in idle.getvalue()
        assert "8s" not in idle.getvalue()


class TestFrameDrivesAnimation:
    """Verify StatusComponent reads frame() for all animated elements."""

    def _make_state(self, frame: int = 7) -> object:
        from unittest.mock import MagicMock  # noqa: PLC0415

        state = MagicMock()
        state.conversation.frame.return_value = frame
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
        state.pending_approval.return_value = None
        state.active_mode.return_value = MagicMock(badge="⏵⏵")
        state.workflow_run.return_value = None
        return state

    def test_flower_is_static_when_idle(self) -> None:
        """Flower must not change when agent is idle and compaction is off."""
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import StatusComponent, _FLOWERS  # noqa: PLC0415

        results: set[str] = set()
        for i in range(len(_FLOWERS)):
            state = self._make_state(frame=i)  # idle, compaction off
            comp = StatusComponent(state)
            console = Console(highlight=False, markup=False, no_color=True, width=120)
            with console.capture() as cap:
                console.print(comp.render())
            results.add(cap.get()[0])

        assert len(results) == 1, "Flower must be fixed when idle"
        assert results == {_FLOWERS[0]}

    def test_flower_animates_when_running(self) -> None:
        """Flower must cycle through values when the agent is running."""
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import StatusComponent, _FLOWERS  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        results: set[str] = set()
        for i in range(len(_FLOWERS)):
            state = self._make_state(frame=i)
            state.conversation.is_running.return_value = True
            state.conversation.agent_state.return_value = MagicMock(name="THINKING")
            state.conversation.agent_state().name = "THINKING"
            state.conversation.elapsed_s = float(i)
            comp = StatusComponent(state)
            console = Console(highlight=False, markup=False, no_color=True, width=120)
            with console.capture() as cap:
                console.print(comp.render())
            results.add(cap.get()[0])

        assert len(results) == len(_FLOWERS), "All flowers should appear when running"

    def test_compaction_spinner_changes_with_frame(self) -> None:
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import StatusComponent, _COMPACT_SPINNER  # noqa: PLC0415

        results: set[str] = set()
        for i in range(len(_COMPACT_SPINNER)):
            state = self._make_state(frame=i)
            state.conversation.compaction_active.return_value = True
            comp = StatusComponent(state)
            console = Console(highlight=False, markup=False, no_color=True, width=120)
            with console.capture() as cap:
                console.print(comp.render())
            rendered = cap.get()
            # The spinner char appears at the start of the "Compacting…" line
            for line in rendered.splitlines():
                if "Compacting" in line:
                    results.add(line[0])
                    break

        assert len(results) > 1, "Spinner must cycle across frame values"

    def test_transcript_loading_status_has_no_spinner(self) -> None:
        from rich.console import Console  # noqa: PLC0415
        from agenthicc.tui.workspace.components import (  # noqa: PLC0415
            StatusComponent,
            _COMPACT_SPINNER,
        )

        for i in range(4):
            state = self._make_state(frame=i)
            state.conversation.transcript_loading.return_value = True
            comp = StatusComponent(state)
            console = Console(highlight=False, markup=False, no_color=True, width=120)
            with console.capture() as cap:
                console.print(comp.render())
            rendered = cap.get()
            line = next(line for line in rendered.splitlines() if "Loading transcript" in line)
            assert not any(f"{spinner} Loading transcript" in line for spinner in _COMPACT_SPINNER)

    @pytest.mark.parametrize(
        ("kind", "label"),
        [
            ("tool", "Waiting for approval"),
            ("plan_review", "Waiting for plan approval"),
            ("questions", "Waiting for your answer"),
            ("unknown", "Waiting for approval"),
        ],
    )
    def test_waiting_prompt_freezes_status_animation(self, kind: str, label: str) -> None:
        from rich.console import Console  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415
        from agenthicc.tui.workspace.components import (  # noqa: PLC0415
            StatusComponent,
            _FLOWERS,
        )

        rendered: set[str] = set()
        for i in range(len(_FLOWERS)):
            state = self._make_state(frame=i)
            state.conversation.is_running.return_value = True
            state.conversation.agent_state.return_value = MagicMock(name="THINKING")
            state.conversation.agent_state().name = "THINKING"
            state.pending_approval.return_value = MagicMock(kind=kind)
            comp = StatusComponent(state)
            console = Console(highlight=False, markup=False, no_color=True, width=120)
            with console.capture() as cap:
                console.print(comp.render())
            rendered.add(cap.get())

        assert len(rendered) == 1
        output = next(iter(rendered))
        assert label in output
        assert "Thinking" not in output
