"""UnifiedInputSession — single CBREAK context for the application lifetime (PRD-62, PRD-74).

One session, one raw_mode context, capability-pipeline dispatch (PRD-74).

Modes:
    IDLE      — full feature set: triggers, history, cursor movement, mode cycling
    STREAMING — reduced: queue messages, interrupt agent, paste, editing, cursor movement

Each mode is a declared list of Capability instances (see capabilities.py).
Adding a new trigger char or a new mode requires no changes here.

TUX-04: the ``/``, ``$`` and ``@`` pickers read this session's buffer (keys
they do not claim edit the prompt and the picker follows), Ctrl+R searches
history, Ctrl+X Ctrl+E edits the prompt in ``$VISUAL``/``$EDITOR``, and ↑ on
an empty prompt during a turn moves focus into the queued messages.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agenthicc.tui.cbreak_reader import Key, key_name
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.capabilities import (
    _EXIT,
    IDLE_CAPABILITIES,
    STREAMING_CAPABILITIES,
    Capability,
    _ExitSentinel,
)
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.keymap import Action
from agenthicc.tui.preferences import ui_settings
from agenthicc.tui.runtime.commands import CommandBus, SendMessageCommand
from agenthicc.tui.runtime.mode_manager import ModeManager

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.input.history_search import HistorySearchOverlay
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.terminal.backend import TerminalBackend
    from agenthicc.tui.trigger import TriggerManager
    from agenthicc.tui.workspace.appender import ScrollBufferAppender
    from agenthicc.tui.workspace.overlay import OverlayHost
    from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

# Re-export for tests/external callers that import from here.
__all__ = [
    "_EXIT",
    "Acceptance",
    "CHORD_TIMEOUT_S",
    "InputMode",
    "SubmitReceipt",
    "TerminalHooks",
    "UnifiedInputSession",
]

#: Ctrl+X waits this long for Ctrl+E; then it opens the editor alone (TUX-04 §2.1).
CHORD_TIMEOUT_S = 0.3

#: Runs an operation with the terminal handed back (cooked mode), then resumes.
HandoffRunner = Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]


def _noop() -> None:
    return None


@dataclass(frozen=True)
class TerminalHooks:
    """What the composer's owner does around an external editor and Ctrl+L.

    ``suspend``/``resume`` hand the screen to the editor and take it back (the
    workspace's live region); ``repaint`` redraws the live region;
    ``runtime_dir`` holds the editor's temporary file. An embedded composer,
    which runs no key loop of its own, sets ``handoff`` to its host's
    terminal handoff.
    """

    suspend: Callable[[], None] = _noop
    resume: Callable[[], None] = _noop
    repaint: Callable[[], None] = _noop
    runtime_dir: Path | None = None
    handoff: HandoffRunner | None = None


def _write_draft(directory: Path, text: str) -> Path:
    """Write *text* to a new private file (mode 0600) in *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="prompt-", suffix=".md", dir=directory)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(text)
    return Path(name)


def _run_editor(path: Path) -> int:
    """Run ``$VISUAL``, ``$EDITOR`` or ``vi`` on *path*; return its exit status."""
    configured = (
        os.environ.get("VISUAL", "").strip() or os.environ.get("EDITOR", "").strip() or "vi"
    )
    command = shlex.split(configured)
    return subprocess.run([*command, str(path)], check=False).returncode


class InputMode(Enum):
    IDLE = auto()
    STREAMING = auto()


class UnifiedInputSession:
    """Single CBREAK session for the entire application lifetime.

    raw_mode is entered once at startup via run() and exited at shutdown.
    Mode transitions are synchronous signal updates on AppState.
    """

    def __init__(
        self,
        app_state: AppState,
        command_bus: CommandBus,
        trigger_registry: TriggerManager | None = None,
        mode_manager: ModeManager | None = None,
        overlay_host: OverlayHost | None = None,
        cwd: Path | None = None,
        cfg: AgenthiccConfig | None = None,
        history: list[str] | None = None,
        busy: bool = False,
        clear_after_acceptance: bool = False,
        preserve_submission_text: bool = False,
        on_ready: Callable[[bool], None] | None = None,
        terminal_hooks: TerminalHooks | None = None,
    ) -> None:
        self._state: AppState = app_state
        #: Set by the session owner once its workspace exists.
        self.terminal_hooks: TerminalHooks = terminal_hooks or TerminalHooks()
        # Set by the /background handoff to end the input loop after detaching.
        self._exit_requested: bool = False
        self._terminal_handoff: (
            tuple[Callable[[], Awaitable[None]], asyncio.Future[None]] | None
        ) = None
        self._backend: TerminalBackend | None = None
        self._bus: CommandBus = command_bus
        self._registry: TriggerManager | None = trigger_registry
        self._modes: ModeManager = mode_manager or ModeManager()
        self._overlay: OverlayHost | None = overlay_host
        self._cwd: Path = cwd or Path(".")
        self._cfg: AgenthiccConfig | None = cfg
        self._busy = busy
        self._clear_after_acceptance = clear_after_acceptance
        self._preserve_submission_text = preserve_submission_text
        self._on_ready = on_ready

        self._mode: InputMode = InputMode.IDLE
        self._capabilities: list[Capability] = IDLE_CAPABILITIES  # switched by set_mode()
        self._buf: InputBuffer = InputBuffer()
        self._paste: PasteState = PasteState()
        # Undo restores a condensed paste's range together with the text.
        self._buf.snapshot_hook = self._paste.memento
        # The open ``/``/``@`` picker, which reads ``_buf`` (TUX-04 §2.2).
        self._picker: TriggerPickerOverlay | None = None
        # A pressed Ctrl+X waiting for Ctrl+E (TUX-04 §2.4).
        self._chord: asyncio.TimerHandle | None = None
        # Keep the caller-owned history list: manager-bound editors use one
        # list per target session so accepted submissions remain navigable
        # when the composer is closed and reopened.
        self._hist: HistoryNavigator = HistoryNavigator(history if history is not None else [])
        self._ctrl_c_count: int = 0
        # The composer action the key being dispatched is bound to (AGX-13).
        self._action: str | None = None
        self._action_rebindable = False
        # When deleting or recalling history last emptied the prompt, and
        # whether a guarded ← has asked for its second press (AGX-05 §5.2).
        self._emptied_at = 0.0
        self._hub_left_armed = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def set_mode(self, mode: InputMode) -> None:
        self._mode = mode
        if mode == InputMode.STREAMING:
            self._ctrl_c_count = 0
        else:
            # The queue is only shown during a turn.
            self._state.conversation.queue.focus.set(None)
        self._capabilities = (
            STREAMING_CAPABILITIES if mode == InputMode.STREAMING else IDLE_CAPABILITIES
        )

    def set_text(self, text: str) -> None:
        """Replace the composer contents without submitting the text.

        Registry overlays use this after a detail-page selection. Keeping the
        mutation here ensures the reactive ``InputState`` is updated before
        the overlay is hidden and redrawn.
        """
        self._buf.set(list(text))
        self._paste.condensed = False
        self._ctrl_c_count = 0
        self._push()
        self._sync_picker()

    def set_history(self, history: list[str]) -> None:
        """Install resumed input history after the shared transcript preview loads."""

        self._hist.replace_history(history)

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Block until exit, yielding the terminal for requested external work."""
        import asyncio as _asyncio

        from agenthicc.tui.terminal.backend import get_backend

        backend = get_backend()
        if not backend.is_interactive():
            # Non-interactive environment (pipe, CI, redirect, Windows without
            # a console) — exit cleanly so TUISession cancels tasks normally.
            if self._on_ready is not None:
                self._on_ready(False)
            return

        self._backend = backend
        loop = _asyncio.get_event_loop()
        try:
            if self._on_ready is not None:
                self._on_ready(True)
            while True:
                handoff: tuple[Callable[[], Awaitable[None]], asyncio.Future[None]] | None = None
                with backend.enter_raw_mode():
                    # The inline TUI keeps native selection unless [ui] mouse = "on".
                    ui = ui_settings(self._state)
                    backend.set_reporting(
                        focus=True,
                        mouse=ui is not None and ui.mouse_enabled(fullscreen=False),
                    )
                    while True:
                        try:
                            # run_in_executor lets the event loop service other tasks
                            # (agent streaming, workspace redraws, tick loop, etc.)
                            key, ch = await loop.run_in_executor(None, backend.read_key)
                        except KeyboardInterrupt:
                            self._state.conversation.notification.set(None)
                            return
                        except OSError:
                            if self._terminal_handoff is not None:
                                handoff, self._terminal_handoff = self._terminal_handoff, None
                                break
                            if self._exit_requested:
                                return
                            continue

                        if await self._dispatch(key, ch) is _EXIT:
                            return
                        if self._terminal_handoff is not None:
                            handoff, self._terminal_handoff = self._terminal_handoff, None
                            break

                if handoff is None:
                    return
                backend.close()
                operation, completed = handoff
                try:
                    await operation()
                except asyncio.CancelledError:
                    if not completed.done():
                        completed.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - report failures to the handoff caller
                    if not completed.done():
                        completed.set_exception(exc)
                else:
                    if not completed.done():
                        completed.set_result(None)
                if self._exit_requested:
                    return
        finally:
            self._backend = None
            pending, self._terminal_handoff = self._terminal_handoff, None
            if pending is not None and not pending[1].done():
                pending[1].set_exception(
                    RuntimeError("the input session stopped before terminal handoff")
                )

    async def run_terminal_handoff(self, operation: Callable[[], Awaitable[None]]) -> None:
        """Run *operation* with cooked terminal input, then resume this session."""
        backend = self._backend
        if backend is None or self._exit_requested:
            raise RuntimeError("the interactive input session is not running")
        if self._terminal_handoff is not None:
            raise RuntimeError("another terminal handoff is already pending")
        completed = asyncio.get_running_loop().create_future()
        handoff = (operation, completed)
        self._terminal_handoff = handoff
        backend.restore()
        try:
            await completed
        except asyncio.CancelledError:
            if self._terminal_handoff is handoff:
                self._terminal_handoff = None
                backend.restore()
            raise

    def request_exit(self) -> None:
        """End :meth:`run` now, even while it waits for a key.

        Used when the session hands over without a keystroke of its own —
        switching to another agent from the hub (AGX-05 §5.2). The wake byte
        interrupts the blocking read.
        """
        from agenthicc.tui.terminal.backend import get_backend

        self._exit_requested = True
        get_backend().restore()

    # ── capability pipeline dispatch ──────────────────────────────────────────

    def conditions(self) -> frozenset[str]:
        """The composer conditions key bindings depend on (AGX-13)."""
        flags: set[str] = set()
        if self._mode == InputMode.STREAMING:
            flags.add("running")
        pool_state = self._state.conversation.subagent_pool_state()
        if pool_state is not None:
            flags.add("subagents")
            if pool_state.active:
                flags.add("subagents_running")
        if self._paste.condensed:
            flags.add("paste")
            if self._buf.cursor == self._paste.end:
                flags.add("paste_at_cursor")
        elif not self._buf.text:
            flags.add("empty")
        if self._state.conversation.queue:
            flags.add("queued")
        return frozenset(flags)

    async def _dispatch(self, key: Key, ch: str) -> _ExitSentinel | None:
        """Run the active capability list until one consumes the key.

        Focus reports are terminal state, not composer input: they must never
        enter history, modify the prompt, or reach a keybinding.
        """
        if key is Key.FOCUS_IN:
            self._state.conversation.terminal_focused.set(True)
            return None
        if key is Key.FOCUS_OUT:
            self._state.conversation.terminal_focused.set(False)
            return None
        keymap = self._state.keymap()
        name = key_name(key, ch)
        if self._chord is not None:
            # Ctrl+X was pressed: Ctrl+E completes the chord; any other key
            # cancels it and is handled as usual.
            self._cancel_chord()
            if name == "ctrl+e":
                self.open_external_editor()
                return None
        queue = self._state.conversation.queue
        if queue.focus() is not None:
            if self._dispatch_queue(keymap.resolve("queue", name)):
                return None
            # Any other key gives focus back to the prompt, then edits it.
            queue.focus.set(None)
        conditions = self.conditions()
        action = None
        if name == "left" and not self._buf.text and not self._paste.condensed:
            # ← on an empty prompt has its own binding (agents.open_hub).
            action = keymap.resolve("composer", "left_on_empty", conditions)
        if action is None:
            action = keymap.resolve("composer", name, conditions)
        self._action = None if action is None else action.name
        self._action_rebindable = action is not None and action.rebindable
        if self._action != "agents.open_hub":
            self._hub_left_armed = False
        for cap in self._capabilities:
            result = await cap.handle(key, ch, self)
            if result is _EXIT:
                return _EXIT
            if result:
                break
        # The picker follows whatever the key did to the prompt (TUX-04 §2.2).
        self._sync_picker()
        # A started /background handoff, or a switch to another session, ends
        # the input loop.
        if self._exit_requested:
            return _EXIT
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _push(self) -> None:
        inp = self._state.input
        if self._paste.condensed:
            display_buf, display_cursor = self._paste._condensed_view(self._buf)
            inp.update(
                display_buf,
                display_cursor,
                paste_condensed=True,
                paste_label=self._paste.label,
            )
        else:
            inp.update(list(self._buf.buf), self._buf.cursor)

    def _ctrl_c_sequence(self) -> _ExitSentinel | None:
        self._ctrl_c_count += 1
        if self._ctrl_c_count == 1:
            self._buf.clear()
            self._paste.condensed = False
            self._push()
            self._state.conversation.notification.set("Press Ctrl+C again to exit.")
            return None  # keep looping
        self._state.conversation.notification.set(None)
        from agenthicc.tui.input.renderer import show_exit_hint

        show_exit_hint(self._state.conversation.session_id())
        return _EXIT

    def _prepare_submission(self) -> None:
        """Synchronous pre-submission cleanup — single source of truth.

        Every code path that sends a message must call this before dispatching
        SendMessageCommand.  Adding new cleanup here automatically applies to
        all submission paths (Enter, Ctrl+D, a picker's one-Enter command).
        """
        self._buf.clear()
        self._buf.forget_undo()
        self._paste.condensed = False
        self._ctrl_c_count = 0
        self._push()

    async def _submit(self, text: str) -> None:
        """Prepare the input state and dispatch SendMessageCommand."""
        self._prepare_submission()
        await self._bus.dispatch_async(SendMessageCommand(text=text))

    async def _submit_buffer(self, *, commit_history: bool, interject: bool = False) -> None:
        """Send the prompt (Enter, a picker's one-Enter command, an interjection).

        A target-bound composer (``clear_after_acceptance``) keeps the text
        until its owner accepts it; otherwise the prompt clears first.
        """
        raw_text = self._buf.text
        has_text = bool(raw_text.strip())
        text = raw_text if self._preserve_submission_text else raw_text.strip()
        command = SendMessageCommand(text=text, interject=interject)
        if self._clear_after_acceptance and has_text:
            result = await self._bus.dispatch_async(command)
            if isinstance(result, Acceptance) and not result.ok:
                return
            self._prepare_submission()
            if commit_history:
                self._hist.commit(text)
            return
        self._prepare_submission()
        if has_text:
            if commit_history:
                self._hist.commit(text)
            await self._bus.dispatch_async(command)

    # ── the ``/`` and ``@`` pickers (TUX-04 §2.2) ─────────────────────────────

    def _find_trigger_anchor(self) -> int | None:
        """Index of the trigger character of the token that ends at the cursor."""
        if self._registry is None:
            return None
        buf = self._buf.buf
        for index in range(self._buf.cursor - 1, -1, -1):
            ch = buf[index]
            if ch.isspace():
                return None
            if ch in self._registry.chars:
                handler = self._registry.get(ch)
                if handler is not None and handler.can_activate(buf[:index]):
                    return index
        return None

    @property
    def picker(self) -> TriggerPickerOverlay | None:
        """The open trigger picker, if any."""
        return self._picker

    def _open_picker(self, anchor: int) -> None:
        """Open the picker for the trigger character at *anchor* in the buffer."""
        if self._overlay is None or self._registry is None:
            return
        handler = self._registry.get(self._buf.buf[anchor])
        if handler is None:
            return
        from agenthicc.tui.workspace.overlays.trigger_picker import TriggerPickerOverlay

        picker = TriggerPickerOverlay(
            self._buf,
            anchor,
            handler,
            cwd=self._cwd,
            keymap=self._state.keymap(),
            busy=self._mode is InputMode.STREAMING or self._busy,
            paste_state=self._paste,
        )
        if not picker.refresh():
            return
        self._picker = picker
        self._push()
        self._overlay.show(picker)

    def _close_picker(self) -> None:
        """Close the picker; the prompt keeps its text."""
        picker, self._picker = self._picker, None
        # InputState first: hiding redraws, and the composer must not flash
        # stale text.
        self._push()
        if picker is not None and self._overlay is not None and self._overlay.widget is picker:
            self._overlay.hide()

    def _sync_picker(self) -> None:
        """Re-derive the picker from the buffer; close it when its trigger is gone."""
        picker = self._picker
        if picker is None:
            return
        if self._overlay is None or self._overlay.widget is not picker:
            self._picker = None  # another overlay replaced it
            return
        if picker.refresh():
            self._overlay.request_redraw()
        else:
            self._close_picker()

    async def _picker_key(self, picker: TriggerPickerOverlay, key: Key, ch: str) -> bool:
        """Give *key* to the picker; ``False`` when it is a key for the prompt."""
        from agenthicc.tui.workspace.overlays.trigger_picker import PickerOutcome

        outcome = picker.choose(key, ch)
        if outcome is None:
            return False
        self._push()
        if outcome is PickerOutcome.STAY:
            if self._overlay is not None:
                self._overlay.request_redraw()
            return True
        self._close_picker()
        if outcome is PickerOutcome.SUBMIT:
            await self._submit_buffer(commit_history=self._mode is InputMode.IDLE)
        return True

    # ── Ctrl+R history search (TUX-04 §2.3) ──────────────────────────────────

    def _open_history_search(self) -> None:
        if self._overlay is None:
            return
        from agenthicc.tui.input.history_search import HistorySearchOverlay

        self._overlay.show(
            HistorySearchOverlay(
                self._hist.entries,
                self._state.keymap(),
                list(self._buf.buf),
                self._buf.cursor,
            )
        )

    def _search_key(self, search: HistorySearchOverlay, key: Key, ch: str) -> None:
        from agenthicc.tui.input.history_search import SearchOutcome

        outcome = search.choose(key, ch)
        if outcome is SearchOutcome.ACCEPT and search.match is not None:
            self._buf.set(list(search.match))
            self._paste.condensed = False
        if outcome is not SearchOutcome.STAY and self._overlay is not None:
            # Esc changes nothing: the draft was never touched.
            self._push()
            self._overlay.hide()
        elif self._overlay is not None:
            self._overlay.request_redraw()

    # ── the queued messages (TUX-04 §2.6) ─────────────────────────────────────

    def _focus_queue(self) -> None:
        """↑ on an empty prompt during a turn: select the newest queued message."""
        items = self._state.conversation.queue.items()
        if items:
            self._state.conversation.queue.focus.set(items[-1].id)

    def _dispatch_queue(self, action: Action | None) -> bool:
        """Run a ``queue`` action on the focused message; ``False`` for other keys."""
        if action is None:
            return False
        queue = self._state.conversation.queue
        items = queue.items()
        focus = queue.focus()
        index = next((i for i, item in enumerate(items) if item.id == focus), None)
        if index is None:
            queue.focus.set(None)
            return False
        match action.name:
            case "queue.up":
                queue.focus.set(items[max(0, index - 1)].id)
            case "queue.down":
                queue.focus.set(items[index + 1].id if index + 1 < len(items) else None)
            case "queue.edit":
                queue.focus.set(None)
                message = queue.remove(items[index].id)
                if message is not None:
                    self._buf.set(list(message.text))
                    self._paste.condensed = False
                    self._push()
            case "queue.remove":
                rest = [item for item in items if item.id != items[index].id]
                queue.remove(items[index].id)
                queue.focus.set(rest[min(index, len(rest) - 1)].id if rest else None)
            case "queue.back":
                queue.focus.set(None)
            case _:
                return False
        return True

    # ── external editor (TUX-04 §2.4) ─────────────────────────────────────────

    def _start_chord(self) -> None:
        """Ctrl+X: wait for Ctrl+E; alone, it opens the editor after the timeout."""
        self._cancel_chord()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.open_external_editor()
            return
        self._chord = loop.call_later(CHORD_TIMEOUT_S, self._chord_expired)

    def _cancel_chord(self) -> None:
        if self._chord is not None:
            self._chord.cancel()
            self._chord = None

    def _chord_expired(self) -> None:
        self._chord = None
        self.open_external_editor()

    def open_external_editor(self) -> None:
        """Edit the prompt in ``$VISUAL``/``$EDITOR`` with the terminal handed back."""
        hooks = self.terminal_hooks
        text = self._buf.text

        async def operation() -> None:
            await self._edit_externally(text, hooks)

        if hooks.handoff is not None:
            handoff = hooks.handoff

            async def handed_off() -> None:
                await handoff(operation)

            task: asyncio.Task[None] = asyncio.get_running_loop().create_task(handed_off())
            task.add_done_callback(_report_editor_failure(self))
            return
        backend = self._backend
        if backend is None or self._terminal_handoff is not None:
            self._state.conversation.notify_transient("The external editor is not available here")
            return
        completed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        completed.add_done_callback(_retrieve)
        self._terminal_handoff = (operation, completed)
        # Wake a read in progress (the Ctrl+X timeout fires while one waits).
        backend.restore()

    async def _edit_externally(self, text: str, hooks: TerminalHooks) -> None:
        """Write *text* to a private file, run the editor, load the result back."""
        directory = hooks.runtime_dir or Path(tempfile.gettempdir())
        path = await asyncio.to_thread(_write_draft, directory, text)
        try:
            hooks.suspend()
            try:
                status = await asyncio.to_thread(_run_editor, path)
            except (OSError, ValueError) as exc:
                self._state.conversation.notify_transient(
                    f"The editor could not start ({exc}); the prompt is unchanged", duration=4.0
                )
                return
            finally:
                hooks.resume()
            if status != 0:
                self._state.conversation.notify_transient(
                    f"The editor exited with status {status}; the prompt is unchanged", duration=4.0
                )
                return
            edited = await asyncio.to_thread(path.read_text, encoding="utf-8")
            # Editors end a file with a newline; a prompt does not.
            self._buf.set(list(edited.removesuffix("\n")))
            self._paste.condensed = False
            self._push()
        finally:
            await asyncio.to_thread(path.unlink, missing_ok=True)

    def print_idle_header(self, appender: ScrollBufferAppender | None) -> None:
        """Print the session info + separator before each new prompt."""
        if appender:
            appender.print_idle_header()


@runtime_checkable
class Acceptance(Protocol):
    """A submit handler's answer: ``ok`` is false when the target refused the text."""

    @property
    def ok(self) -> bool: ...


@dataclass(frozen=True)
class SubmitReceipt:
    """What a target-bound composer's submit handler returns (``ok=False`` keeps the text)."""

    ok: bool


def _retrieve(future: asyncio.Future[None]) -> None:
    """Mark a handoff's outcome as seen; the editor reports its own failures."""
    if not future.cancelled():
        future.exception()


def _report_editor_failure(
    session: UnifiedInputSession,
) -> Callable[[asyncio.Task[None]], None]:
    def done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            session._state.conversation.notify_transient(
                f"The external editor is not available here ({exc})", duration=4.0
            )

    return done
