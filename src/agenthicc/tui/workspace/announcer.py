"""Plain-sentence announcements for screen readers (AGX-13).

With ``[ui] screen_reader = true`` the live block does not animate, and every
change a sighted user would notice in it — the agent starting or finishing,
a tool running, a prompt waiting for approval, a notification, a mode switch —
is also printed once, as a sentence, in the scroll buffer, where a screen
reader reads it in order. Announcements are presentation only: they are not
written to the session's event log.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agenthicc.tui.conversation_store import AgentState

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace.appender import ScrollBufferAppender

__all__ = ["Announcer"]

_STATE_SENTENCES = {
    AgentState.THINKING: "Thinking.",
    AgentState.RECOVERING: "Recovering from a tool error.",
    AgentState.COMPLETE: "Done.",
    AgentState.ERROR: "The turn failed.",
}


class Announcer:
    """Say each visible state change once in the scroll buffer."""

    def __init__(self, app_state: AppState, appender: ScrollBufferAppender) -> None:
        self._state = app_state
        self._appender = appender
        self._unsubscribe: list[Callable[[], None]] = []
        self._last: dict[str, str] = {}

    def start(self) -> None:
        conv = self._state.conversation
        for signal, handler in (
            (conv.agent_state, self._on_agent_state),
            (conv.active_tool, self._on_agent_state),
            (self._state.pending_approval, self._on_approval),
            (conv.notification, self._on_notification),
            (self._state.active_mode, self._on_mode),
        ):
            self._unsubscribe.append(signal.subscribe(handler))

    def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()

    def _say(self, topic: str, sentence: str) -> None:
        if self._last.get(topic) == sentence:
            return
        self._last[topic] = sentence
        if sentence:
            self._appender.announce(sentence)

    def _on_agent_state(self) -> None:
        conv = self._state.conversation
        state = conv.agent_state()
        if state is AgentState.RUNNING:
            tool = conv.active_tool()
            self._say("agent", f"Running {tool}." if tool else "Running a tool.")
            return
        self._say("agent", _STATE_SENTENCES.get(state, ""))

    def _on_approval(self) -> None:
        pending = self._state.pending_approval()
        if pending is None:
            self._say("approval", "")
            return
        if pending.kind == "questions":
            sentence = "A question is waiting for your answer."
        elif pending.kind == "plan_review":
            sentence = "A plan is waiting for your approval."
        else:
            sentence = f"Approval needed: {pending.tool_name}."
        self._say("approval", sentence)

    def _on_notification(self) -> None:
        text = self._state.conversation.notification()
        self._say("notification", text if isinstance(text, str) else "")

    def _on_mode(self) -> None:
        self._say("mode", f"{self._state.active_mode().name} mode.")
