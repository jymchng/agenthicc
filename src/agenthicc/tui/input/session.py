"""IdleInputSession — the idle CBREAK input loop (PRD-57).

State split into focused components:

  InputBuffer       — buf + cursor; typed mutation methods
  PasteState        — condensed paste management
  HistoryNavigator  — up/down history
  PromptRenderer    — all ANSI terminal I/O
  _TriggerState     — active trigger handler + fragment + matches (or None)

Key dispatch uses Python 3.10 ``match`` statements, one per mode (normal /
trigger).  There is no interleaving — each mode has exactly one dispatch
method.

**Sentinel**: ``_EXIT = object()`` is the only value that causes ``run()`` to
``return None``.  A plain ``None`` from a dispatch method means "keep looping".
This eliminates the class of bug where Ctrl+D on an empty buffer or double
Ctrl+C caused an infinite loop instead of exiting.

**Patchability**: ``mention_input.read_line_with_mention`` passes
``_fn_raw_mode``, ``_fn_read_key``, and ``_fn_redraw`` as constructor arguments
so that tests patching ``mention_input._raw_mode / _read_key / _redraw`` still
work without a real TTY.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agenthicc.tui.cbreak_reader import Key, raw_mode as _default_raw_mode, read_key as _default_read_key
from agenthicc.tui.input.buffer import InputBuffer
from agenthicc.tui.input.history import HistoryNavigator
from agenthicc.tui.input.paste import PasteState
from agenthicc.tui.input.renderer import DropdownState, PromptRenderer

if TYPE_CHECKING:
    from agenthicc.tui.trigger import TriggerContext, TriggerHandler, TriggerRegistry
    from agenthicc.tui.menu import MenuDriver, MenuWidget
    from agenthicc.modes import ModeManager

# Sentinel returned by dispatch methods to mean "run() should return None".
# Plain None means "keep looping"; _EXIT means "exit and return None".
_EXIT = object()


@dataclass
class _TriggerState:
    """Active trigger mode state."""
    handler: "TriggerHandler"
    fragment: str = ""
    matches: list[Any] = field(default_factory=list)
    selected: int = 0
    hint: str | None = None


class InputSession:
    """One interactive input cycle.

    Call :meth:`run` to block until the user submits text or exits.

    Parameters
    ----------
    _fn_raw_mode:
        Override the CBREAK context manager (default:
        :func:`~agenthicc.tui.cbreak_reader.raw_mode`).
        Tests patch ``mention_input._raw_mode`` and pass it here.
    _fn_read_key:
        Override the keystroke reader (default:
        :func:`~agenthicc.tui.cbreak_reader.read_key`).
        Tests patch ``mention_input._read_key`` and pass it here.
    _fn_redraw:
        When provided, used as the rendering backend instead of
        :class:`~agenthicc.tui.input.renderer.PromptRenderer`.
        Must match ``_redraw(prompt, buf, fragment, matches, selected,
        prev, in_trigger, hint, trigger_char, mode_line, cursor) -> int``.
        Tests patch ``mention_input._redraw`` and pass it here.
    """

    def __init__(
        self,
        cwd: Path,
        history: list[str],
        registry: "TriggerRegistry | None" = None,
        initial_menu: "MenuWidget | None" = None,
        resume_id: str = "",
        mode_manager: "ModeManager | None" = None,
        initial_buf: list[str] | None = None,
        _fn_raw_mode: Callable | None = None,
        _fn_read_key: Callable | None = None,
        _fn_redraw: Callable | None = None,
    ) -> None:
        from agenthicc.tui.trigger import TriggerRegistry  # noqa: PLC0415
        from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
        from agenthicc.tui.menu import MenuDriver  # noqa: PLC0415

        self._cwd = cwd
        self._resume_id = resume_id
        self._mode_manager = mode_manager
        self._renderer = PromptRenderer()

        if registry is None:
            registry = TriggerRegistry()
            registry.register(AtMentionTrigger())
        self._registry = registry
        self._ctx: TriggerContext  # lazily built on first trigger use

        self._buf = InputBuffer(initial_buf)
        self._paste = PasteState()
        self._hist = HistoryNavigator(history)

        self._trigger: _TriggerState | None = None
        self._ctrl_c_count = 0
        self._mode_notification: Any = None

        self._driver = MenuDriver()
        if initial_menu is not None:
            self._driver.open(initial_menu)

        # Injectable callables (G1, G5 from PRD §7).
        self._fn_raw_mode: Callable = _fn_raw_mode or _default_raw_mode
        self._fn_read_key: Callable = _fn_read_key or _default_read_key

        # Build render function. When _fn_redraw is provided (backward-compat
        # path from mention_input), wrap it into a zero-argument callable that
        # the run loop can call uniformly.
        if _fn_redraw is not None:
            self._fn_render: Callable = self._make_redraw_compat(_fn_redraw)
        else:
            self._fn_render = self._render

    def _make_redraw_compat(self, redraw_fn: Callable) -> Callable:
        """Return a zero-arg render callable that delegates to *redraw_fn*.

        *redraw_fn* has the old ``_redraw(prompt, buf, fragment, matches,
        selected, prev, in_trigger, hint, trigger_char, mode_line, cursor)``
        signature.  The wrapper reads current session state on each call so
        it is always up to date — no captured mutable state.
        """
        session = self

        def _render_via_redraw() -> None:
            t = session._trigger
            display_buf = (
                list(session._paste.label)
                if session._paste.condensed and t is None
                else session._buf.buf
            )
            disp_cursor = (
                len(display_buf)
                if session._paste.condensed and t is None
                else session._buf.cursor
            )
            redraw_fn(
                "",
                display_buf,
                t.fragment if t else "",
                t.matches if t else [],
                t.selected if t else 0,
                0,
                t is not None,
                t.hint if t else None,
                t.handler.char if t else "@",
                session._mode_line(),
                disp_cursor,
            )

        return _render_via_redraw

    # ── public interface ──────────────────────────────────────────────────────

    def run(self) -> str | None:
        """Block in CBREAK mode until text is submitted or the session exits."""
        if not sys.stdin.isatty():
            try:
                line = input("\x1b[1;32m❯\x1b[0m ")
                self._hist.commit(line)
                return line
            except (EOFError, KeyboardInterrupt):
                return None

        from agenthicc.tui.trigger import TriggerContext  # noqa: PLC0415
        self._ctx = TriggerContext(cwd=self._cwd, history=self._hist._history)

        fd = sys.stdin.fileno()
        with self._fn_raw_mode(fd):
            while True:
                try:
                    self._fn_render()
                    key, ch = self._fn_read_key(fd)
                except KeyboardInterrupt:
                    # SIGINT arrived in the thread — treat as double Ctrl+C and
                    # exit immediately (tui.py's outer except will also catch it).
                    self._renderer.show_exit_hint(self._resume_id)
                    return None
                except OSError:
                    # os.read() interrupted by a signal (EINTR) — retry the loop.
                    continue

                if self._driver.active:
                    self._driver.handle_key(key, ch)
                    self._ctrl_c_count = 0
                    continue

                ret = (
                    self._dispatch_trigger(key, ch)
                    if self._trigger is not None
                    else self._dispatch_normal(key, ch)
                )

                if ret is _EXIT:
                    return None
                if ret is not None:
                    return ret

    # ── rendering ─────────────────────────────────────────────────────────────

    def _mode_line(self) -> str:
        if self._paste.condensed:
            return "ctrl+v to expand paste"
        if self._mode_notification is not None:
            notif = self._mode_notification
            self._mode_notification = None
            return f"❖ Switched to {notif.name} mode"
        if self._ctrl_c_count > 0:
            return "Press Ctrl+C again to exit."
        if self._mode_manager is not None:
            m = getattr(self._mode_manager, "active", None)
            if m is not None and getattr(m, "name", "") != "Auto":
                return f"⏵⏵ {m.badge} {m.name}  (shift+tab to cycle)  │  ctrl+j = ↵"
        return "⏵⏵ Auto  (shift+tab to cycle)  │  ctrl+j = ↵"

    def _render(self) -> None:
        t = self._trigger
        if self._paste.condensed and t is None and not self._driver.active:
            disp_buf = list(self._paste.label)
            disp_cursor = len(disp_buf)
        else:
            disp_buf = self._buf.buf
            disp_cursor = self._buf.cursor

        dd = DropdownState(
            active=(t is not None),
            matches=t.matches if t else [],
            selected=t.selected if t else 0,
            hint=t.hint if t else None,
            trigger_char=t.handler.char if t else "@",
            fragment=t.fragment if t else "",
        )
        self._renderer.render(disp_buf, disp_cursor, dd, self._mode_line())

    # ── normal mode dispatch ──────────────────────────────────────────────────

    def _dispatch_normal(self, key: Key, ch: str) -> object:
        match key:
            case Key.CTRL_C:
                return self._ctrl_c_sequence()

            case Key.CTRL_D:
                text = self._buf.text
                n = max(1, text.count("\n") + 1)
                self._renderer.scrub_cursor(self._buf.buf)
                self._renderer.erase_below(n)
                sys.stdout.write("\n" * n)
                sys.stdout.flush()
                return text if text else _EXIT

            case Key.ENTER:
                return self._submit()

            case Key.CTRL_ENTER:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                self._buf.insert("\n")

            case Key.CTRL_V:
                self._ctrl_c_count = 0
                self._paste.expand()

            case Key.LEFT:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                    self._buf.cursor = len(self._buf)
                else:
                    self._buf.move_left()

            case Key.RIGHT:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                    self._buf.cursor = len(self._buf)
                else:
                    self._buf.move_right()

            case Key.HOME:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                self._buf.move_home()

            case Key.END:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                self._buf.move_end()

            case Key.BACKSPACE:
                self._ctrl_c_count = 0
                self._handle_backspace()

            case Key.CTRL_U:
                self._ctrl_c_count = 0
                self._buf.clear()
                self._paste.condensed = False

            case Key.UP:
                self._ctrl_c_count = 0
                if not self._paste.condensed and self._buf.move_up():
                    pass
                else:
                    result = self._hist.up(self._buf.buf)
                    if result is not None:
                        self._buf.set(result)
                        self._paste.condensed = False

            case Key.DOWN:
                self._ctrl_c_count = 0
                if not self._paste.condensed and self._buf.move_down():
                    pass
                else:
                    result = self._hist.down(self._buf.buf)
                    if result is not None:
                        self._buf.set(result)
                        self._paste.condensed = False

            case Key.SHIFT_TAB:
                self._ctrl_c_count = 0
                if self._mode_manager is not None:
                    new_mode = self._mode_manager.cycle()
                    self._mode_notification = new_mode

            case Key.PASTE if ch:
                self._ctrl_c_count = 0
                import shutil  # noqa: PLC0415
                cols = shutil.get_terminal_size((80, 24)).columns
                if self._paste.condensed:
                    self._paste.expand()
                self._paste.apply(self._buf, ch, cols)

            case Key.AT if "@" in self._registry.chars:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                self._activate_trigger("@")

            case Key.CHAR if ch:
                self._ctrl_c_count = 0
                if self._paste.condensed:
                    self._paste.expand()
                    self._buf.cursor = len(self._buf)
                if not ch.isspace() and self._buf.cursor == len(self._buf):
                    tail = self._find_trigger_tail()
                    if tail is not None:
                        tch, tpre, tfrag = tail
                        handler = self._registry.get(tch)
                        if handler:
                            self._buf.set(tpre)
                            self._trigger = _TriggerState(
                                handler=handler,
                                fragment=tfrag + ch,
                            )
                            self._update_trigger_matches()
                            return None
                if ch in self._registry.chars:
                    self._activate_trigger(ch)
                else:
                    self._buf.insert(ch)

        return None

    # ── trigger mode dispatch ─────────────────────────────────────────────────

    def _dispatch_trigger(self, key: Key, ch: str) -> object:
        assert self._trigger is not None
        t = self._trigger

        match key:
            case Key.CTRL_C:
                # Cancel trigger then apply the same double-Ctrl+C semantics.
                self._cancel_trigger()
                return self._ctrl_c_sequence()  # (G3: dedup with normal mode)

            case Key.ESC:
                self._cancel_trigger()

            case Key.ENTER | Key.TAB:
                result = self._select_trigger(tab=(key == Key.TAB))
                if result is not None:
                    return result

            case Key.UP:
                if t.matches:
                    t.selected = (t.selected - 1) % len(t.matches)
                    t.hint = t.handler.get_hint(t.matches[t.selected])

            case Key.DOWN:
                if t.matches:
                    t.selected = (t.selected + 1) % len(t.matches)
                    t.hint = t.handler.get_hint(t.matches[t.selected])

            case Key.BACKSPACE:
                if t.fragment:
                    t.fragment = t.fragment[:-1]
                    self._update_trigger_matches()
                else:
                    self._buf.set(t.handler.on_cancel(t.fragment, self._buf.buf))
                    if self._buf.buf and self._buf.buf[-1] == t.handler.char:
                        self._buf.delete_before()
                    self._trigger = None

            case Key.AT:
                t.fragment += "@"
                self._update_trigger_matches()

            case Key.CHAR if ch:
                if ch == " " and t.fragment and t.matches:
                    exact = next(
                        (m for m in t.matches if m.value == t.handler.char + t.fragment),
                        t.matches[0] if len(t.matches) == 1 else None,
                    )
                    if exact is not None:
                        self._buf.set(t.handler.on_select(exact, t.fragment, self._buf.buf))
                        self._buf.insert(" ")
                        self._trigger = None
                        return None
                t.fragment += ch
                self._update_trigger_matches()

        return None

    # ── trigger helpers ───────────────────────────────────────────────────────

    def _activate_trigger(self, char: str) -> None:
        handler = self._registry.get(char)
        if handler and handler.can_activate(self._buf.buf[: self._buf.cursor]):
            self._trigger = _TriggerState(handler=handler)
            self._update_trigger_matches()
        else:
            self._buf.insert(char)

    def _cancel_trigger(self) -> None:
        if self._trigger is None:
            return
        self._buf.set(self._trigger.handler.on_cancel(self._trigger.fragment, self._buf.buf))
        self._trigger = None

    def _select_trigger(self, *, tab: bool = False) -> str | None:
        assert self._trigger is not None
        t = self._trigger
        item = t.matches[t.selected] if t.matches else None
        self._buf.set(t.handler.on_select(item, t.fragment, self._buf.buf))
        if tab and self._buf.buf and self._buf.buf[-1] != " ":
            self._buf.insert(" ")
        self._trigger = None
        if item is None and self._buf.buf:
            return self._submit()
        return None

    def _update_trigger_matches(self) -> None:
        assert self._trigger is not None
        t = self._trigger
        t.matches = t.handler.get_matches(t.fragment, self._ctx)
        t.selected = 0
        t.hint = t.handler.get_hint(t.matches[0] if t.matches else None)

    def _find_trigger_tail(self) -> tuple[str, list[str], str] | None:
        buf = self._buf.buf
        for i in range(len(buf) - 1, -1, -1):
            ch = buf[i]
            if ch.isspace():
                return None
            if ch in self._registry.chars:
                pre = buf[:i]
                fragment = "".join(buf[i + 1 :])
                handler = self._registry.get(ch)
                if handler and handler.can_activate(pre):
                    return (ch, pre, fragment)
        return None

    # ── Ctrl+C / exit logic ───────────────────────────────────────────────────

    def _ctrl_c_sequence(self) -> object:
        """Shared double-Ctrl+C exit sequence (G3: used by both normal + trigger).

        First press: clear buffer, set warning flag, keep looping.
        Second press: show exit hint, return _EXIT.

        The _mode_line() method reads _ctrl_c_count and shows the "Press Ctrl+C
        again to exit." hint on the next render cycle automatically.
        """
        self._ctrl_c_count += 1
        if self._ctrl_c_count == 1:
            # First press: clear buffer and note the warning.
            n = max(1, self._buf.text.count("\n") + 1)
            self._renderer.erase_below(n)
            self._buf.clear()
            self._paste.condensed = False
            return None   # keep looping
        # Second press: exit.
        self._renderer.erase_below(1)
        self._renderer.show_exit_hint(self._resume_id)
        return _EXIT

    # ── backspace / submit ────────────────────────────────────────────────────

    def _handle_backspace(self) -> None:
        if self._paste.condensed:
            self._paste.backspace(self._buf)
            return
        if self._buf.cursor == len(self._buf):
            tail = self._find_trigger_tail()
            if tail is not None:
                tch, tpre, tfrag = tail
                handler = self._registry.get(tch)
                if handler:
                    self._buf.set(tpre)
                    self._trigger = _TriggerState(handler=handler, fragment=tfrag)
                    self._update_trigger_matches()
                    return
        self._buf.delete_before()

    def _submit(self) -> str:
        text = self._buf.text
        if self._paste.condensed:
            self._renderer.scrub_cursor(list(self._paste.label))
            self._renderer.erase_below(1)
            sys.stdout.write("\n")
        else:
            n = max(1, text.count("\n") + 1)
            self._renderer.scrub_cursor(self._buf.buf)
            self._renderer.erase_below(n)
            sys.stdout.write("\n" * n)
        sys.stdout.flush()
        self._hist.commit(text)
        return text if text else ""


def run_input_session(
    prompt_str: str,
    cwd: Path,
    history: list[str],
    registry: "TriggerRegistry | None" = None,
    initial_menu: "MenuWidget | None" = None,
    resume_id: str = "",
    mode_manager: "ModeManager | None" = None,
    initial_buf: list[str] | None = None,
    _fn_raw_mode: Callable | None = None,
    _fn_read_key: Callable | None = None,
    _fn_redraw: Callable | None = None,
) -> str | None:
    """Functional wrapper — creates :class:`InputSession` and calls :meth:`run`.

    The ``_fn_*`` parameters are forwarded to :class:`InputSession` for test
    patchability; production callers can omit them.
    """
    session = InputSession(
        cwd=cwd,
        history=history,
        registry=registry,
        initial_menu=initial_menu,
        resume_id=resume_id,
        mode_manager=mode_manager,
        initial_buf=initial_buf,
        _fn_raw_mode=_fn_raw_mode,
        _fn_read_key=_fn_read_key,
        _fn_redraw=_fn_redraw,
    )
    return session.run()
