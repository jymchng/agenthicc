"""Tests for unified frame counter (PRD-120)."""
from __future__ import annotations

import pytest
from agenthicc.tui.conversation_store import ConversationStore, AgentState


pytestmark = pytest.mark.unit


class TestFrameSignal:
    def test_initial_frame_is_zero(self) -> None:
        conv = ConversationStore()
        assert conv.frame() == 0

    def test_tick_increments_frame(self) -> None:
        conv = ConversationStore()
        conv.tick()
        assert conv.frame() == 1

    def test_tick_increments_unconditionally_when_idle(self) -> None:
        conv = ConversationStore()
        assert conv.agent_state() == AgentState.IDLE
        for _ in range(5):
            conv.tick()
        assert conv.frame() == 5

    def test_tick_increments_during_compaction(self) -> None:
        conv = ConversationStore()
        conv.compaction_active.set(True)
        for _ in range(3):
            conv.tick()
        assert conv.frame() == 3

    def test_tick_increments_when_agent_running(self) -> None:
        conv = ConversationStore()
        conv.begin_turn("agent", "t1")
        for _ in range(4):
            conv.tick()
        assert conv.frame() == 4

    def test_frame_monotonically_increases(self) -> None:
        conv = ConversationStore()
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
