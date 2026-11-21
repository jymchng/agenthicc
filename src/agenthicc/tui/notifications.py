"""Telling the terminal what the session is doing (TUX-10 §2.3, §2.4).

Three small objects, all writing through :meth:`TerminalBackend.write_control`
(which never writes to a stream that is not a terminal):

- :class:`NotificationSink` is the one exit for terminal notifications. Its
  routing table (:data:`PRIORITY`) is how a foreground event and a background
  attention event for the same moment become one notification, not two.
- :class:`TurnNotifier` decides when a foreground turn is worth one: the user
  is away and the turn ended after more than 10 s, failed, or needs them.
- :class:`TerminalPresence` keeps the window title and the taskbar progress
  (``OSC 9;4``) in step with the turn, and restores both on exit.

The background attention router (AGX-10) reports its own events to the same
sink; it no longer sets the window title (the presence adds ``(2)`` when other
sessions need the user).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from agenthicc.tui.terminal import osc
from agenthicc.tui.terminal.osc import ProgressState

if TYPE_CHECKING:
    from agenthicc.tools.approval import ApprovalRequest
    from agenthicc.tui.conversation_store import AppState, ConversationEvent
    from agenthicc.tui.terminal.backend import TerminalBackend

__all__ = [
    "NOTIFY_AFTER_S",
    "PRIORITY",
    "UNKNOWN_FOCUS_AFTER_S",
    "NotificationSink",
    "TerminalPresence",
    "TurnNotifier",
    "session_label",
]

#: A turn must have run longer than this to notify an away user (§2.3).
NOTIFY_AFTER_S: Final = 10.0
#: With focus reporting unavailable, only a turn longer than this notifies.
UNKNOWN_FOCUS_AFTER_S: Final = 30.0
#: The window title changes at most this often.
TITLE_INTERVAL_S: Final = 1.0
#: How long the taskbar shows a failure before it is cleared.
ERROR_HOLD_S: Final = 3.0
#: Two notifications this close are one event seen twice.
_COALESCE_S: Final = 2.0
#: The same text twice within this is dropped.
_REPEAT_S: Final = 10.0

#: The routing table: what each kind of notification outranks. A foreground
#: approval and a background session that needs the user are the same
#: moment, so whichever comes second is dropped unless it outranks the first.
PRIORITY: Mapping[str, int] = {
    "approval": 3,
    "needs": 3,
    "failed": 2,
    "done": 1,
    "finished": 1,
}


def session_label(app_state: AppState) -> str:
    """The session's title, or ``agenthicc`` before it has one."""
    title = app_state.conversation.session_title()
    return title or "agenthicc"


def _notify_setting(app_state: AppState) -> str:
    """``[ui] notify``: ``auto``, ``osc``, ``bell`` or ``off``."""
    from agenthicc.tui.preferences import ui_settings

    ui = ui_settings(app_state)
    return ui.notify if ui is not None else "auto"


class NotificationSink:
    """The one place a terminal notification leaves the process (OSC 9, OSC 777 or the bell)."""

    def __init__(
        self,
        backend: TerminalBackend,
        app_state: AppState,
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._state = app_state
        self._env = os.environ if env is None else env
        self._clock = clock
        self._last: tuple[str, int, float] | None = None
        self._recent: dict[str, float] = {}

    def send(self, kind: str, summary: str) -> bool:
        """Raise *summary* for a notification of *kind*; ``False`` when it was dropped.

        Dropped when ``[ui] notify`` is ``off``, when the terminal would
        print the sequence instead of acting on it, when the same text was
        sent in the last 10 s, and when it is the same moment as a
        notification that outranks it.
        """
        mode = osc.notify_mode(_notify_setting(self._state), self._env)
        sequence = osc.notification(mode, summary)
        if not sequence:
            return False
        now = self._clock()
        priority = PRIORITY.get(kind, 0)
        if self._last is not None:
            _, before, at = self._last
            if now - at < _COALESCE_S and priority <= before:
                return False
        text = osc.sanitize(summary)
        self._recent = {k: t for k, t in self._recent.items() if now - t < _REPEAT_S}
        if text in self._recent:
            return False
        self._recent[text] = now
        self._last = (kind, priority, now)
        self._backend.write_control(sequence)
        return True

    def background(self, kind: str, summary: str) -> None:
        """The attention router's entry: a background session needs you, finished or failed."""
        self.send(kind, summary)


class TurnNotifier:
    """Notify a user who is away when the foreground turn needs or finishes for them (§2.3)."""

    def __init__(self, app_state: AppState, sink: NotificationSink) -> None:
        self._state = app_state
        self._sink = sink
        self._failed = False
        self._interrupted = False
        self._unsubscribers: list[Callable[[], None]] = []

    def start(self) -> None:
        conversation = self._state.conversation
        self._unsubscribers = [
            conversation.on_event(self._on_event),
            self._state.pending_approval.subscribe(self._on_approval),
        ]

    def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []

    # ── when the user is away ────────────────────────────────────────────────

    def away(self, ran_for_s: float) -> bool:
        """The terminal is unfocused, or focus is unknown and the turn ran for 30 s."""
        conversation = self._state.conversation
        if conversation.terminal_focus_known():
            return not conversation.terminal_focused()
        return ran_for_s > UNKNOWN_FOCUS_AFTER_S

    # ── events ───────────────────────────────────────────────────────────────

    def _on_event(self, event: ConversationEvent) -> None:
        from agenthicc.tui.workspace.formatters import compact_duration

        if event.kind == "error":
            self._failed = True
        elif event.kind == "turn_complete" and event.payload.get("interrupted") is True:
            self._interrupted = True
        elif event.kind == "activity_complete":
            failed, interrupted = self._failed, self._interrupted
            self._failed = self._interrupted = False
            elapsed = event.payload.get("elapsed_s")
            seconds = float(elapsed) if isinstance(elapsed, (int, float)) else 0.0
            if interrupted or not self.away(seconds):
                return  # the user stopped it themselves, or is looking at it
            label = session_label(self._state)
            if failed:
                self._sink.send("failed", f"{label}: failed after {compact_duration(seconds)}")
            elif seconds > NOTIFY_AFTER_S:
                self._sink.send("done", f"{label}: done in {compact_duration(seconds)}")

    def _on_approval(self) -> None:
        request: ApprovalRequest | None = self._state.pending_approval()
        if request is None:
            return
        from agenthicc.tui.activity import waiting_reason

        seconds = self._state.conversation.activity_elapsed_s()
        if not self.away(seconds):
            return
        reason = waiting_reason(
            request.kind, request.tool_name, workspace_access=bool(request.workspace_access)
        )
        self._sink.send("approval", f"{session_label(self._state)}: {reason}")


class TerminalPresence:
    """The window title and the taskbar progress, in step with the session (§2.4)."""

    def __init__(
        self,
        app_state: AppState,
        backend: TerminalBackend,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._state = app_state
        self._backend = backend
        self._clock = clock
        self._started = False
        self._pushed = False
        self._title = ""
        self._title_at = float("-inf")
        self._progress: tuple[ProgressState, int] = (ProgressState.CLEAR, 0)
        self._error_until: float | None = None
        self._failed = False
        self._unsubscribe: Callable[[], None] | None = None

    def start(self) -> None:
        """Save the user's title and show the session's; listen for failures."""
        self._started = True
        self._unsubscribe = self._state.conversation.on_event(self._on_event)
        if self._titles():
            self._backend.write_control(osc.TITLE_PUSH)
            self._pushed = True
        self.update()

    def stop(self) -> None:
        """Clear the progress and put the user's title back."""
        if not self._started:
            return
        self._started = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._set_progress(ProgressState.CLEAR, 0)
        if self._pushed:
            self._pushed = False
            self._backend.write_control(osc.TITLE_POP)

    def _titles(self) -> bool:
        from agenthicc.tui.preferences import ui_settings

        ui = ui_settings(self._state)
        return ui is None or ui.window_title

    def _on_event(self, event: ConversationEvent) -> None:
        if event.kind == "error":
            self._failed = True
        elif event.kind == "activity_complete" and self._failed:
            self._failed = False
            self._error_until = self._clock() + ERROR_HOLD_S

    # ── title ────────────────────────────────────────────────────────────────

    def title_text(self) -> str:
        """``⠹ fix flaky test · agenthicc``, with ``(2)`` when other sessions need the user."""
        from agenthicc.tui.preferences import ui_settings
        from agenthicc.tui.theme import glyphs

        conversation = self._state.conversation
        g = glyphs()
        ui = ui_settings(self._state)
        if self._state.pending_approval() is not None:
            mark = g.diamond
        elif conversation.is_running() or conversation.compaction_active():
            animated = ui is None or ui.animated
            mark = g.spinner[conversation.frame() % len(g.spinner)] if animated else g.bullet
        else:
            mark = g.bullet
        title = conversation.session_title()
        text = f"{mark} {title} {g.sep} agenthicc" if title else f"{mark} agenthicc"
        needs = conversation.background_needs_you()
        return f"{text} ({needs})" if needs else text

    def update(self) -> None:
        """Bring the title and the progress up to date (called on the session's clock)."""
        if not self._started:
            return
        now = self._clock()
        if self._titles():
            text = self.title_text()
            if text != self._title and now - self._title_at >= TITLE_INTERVAL_S:
                self._title, self._title_at = text, now
                self._backend.write_control(osc.title(text))
        self._set_progress(*self._progress_target(now))

    # ── progress ─────────────────────────────────────────────────────────────

    def _progress_target(self, now: float) -> tuple[ProgressState, int]:
        conversation = self._state.conversation
        if self._error_until is not None:
            if now < self._error_until:
                return ProgressState.ERROR, 100
            self._error_until = None
        if conversation.compaction_active():
            progress = conversation.compaction_progress()
            # Only a measured step moves the bar; otherwise it is indeterminate.
            if progress is not None:
                return ProgressState.DETERMINATE, int(progress.fraction * 100)
            return ProgressState.INDETERMINATE, 0
        if conversation.is_running() or self._state.pending_approval() is not None:
            return ProgressState.INDETERMINATE, 0
        return ProgressState.CLEAR, 0

    def _set_progress(self, state: ProgressState, percent: int) -> None:
        if (state, percent) == self._progress:
            return
        self._progress = (state, percent)
        if self._backend.supports_progress():
            self._backend.write_control(osc.progress(state, percent))
