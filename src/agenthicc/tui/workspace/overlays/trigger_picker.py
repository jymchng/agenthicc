"""TriggerPickerOverlay — @mention / /command picker in the Live region (PRD-62, PRD-69)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.tui.input.paste import PasteState

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.trigger import MatchItem, TriggerHandler, TriggerManager, TriggerResult
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.input.buffer import InputBuffer


@dataclass
class _ActiveTrigger:
    handler: TriggerHandler
    char: str
    fragment: str
    pre_buf: list[str]


class TriggerPickerOverlay(Overlay):
    """Shows a dropdown picker for @mention and /command triggers.

    Rendered inside the always-on Live block — no pause/restart required.
    The picker is seeded with ``initial_buf`` (current text + trigger char),
    activates the trigger immediately, and calls ``on_complete`` when done.

    TUX-05: the matches are drawn by :class:`agenthicc.tui.workspace.picker.Picker`
    (groups, match highlight, keys, wrapped details, a position footer).
    """

    name = "trigger_picker"

    def __init__(
        self,
        initial_buf: list[str],
        registry: TriggerManager,
        cwd: "Path",
        on_complete: Callable[[TriggerResult | None], None],
        busy: bool = False,
        paste_state: "PasteState | None" = None,
    ) -> None:
        from agenthicc.tui.trigger import TriggerContext

        self._buf = InputBuffer(initial_buf)
        self._registry = registry
        self._cwd = cwd
        self._complete = on_complete
        self._ctx = TriggerContext(cwd=cwd, busy=busy)
        self._paste_state = paste_state
        self._trigger: _ActiveTrigger | None = None
        self._matches: list[MatchItem] = []
        self._selected: int = 0  # index into self._matches
        self._scroll: int = 0  # index of first visible item
        self._hint: str | None = None
        self._init_trigger()

    # ── setup ─────────────────────────────────────────────────────────────────

    def _init_trigger(self) -> None:
        """Find the rightmost activatable trigger char in initial_buf and activate it.

        PRD-77 fix: do NOT anchor on last_char.  The last character may be a
        regular char appended after the trigger (e.g. InsertCapability re-opens
        the overlay with initial = pre + ["@"] + fragment + [new_char], where
        new_char is "." — not a trigger).  Walking backward and using each
        candidate char's own handler avoids the wrong-handler bug too.
        """
        buf = self._buf.buf
        if not buf:
            return
        chars = self._registry.chars if self._registry else set()
        for i in range(len(buf) - 1, -1, -1):
            ch = buf[i]
            if ch.isspace():
                break  # stop at a word boundary; no trigger activates here
            if ch in chars:
                handler = self._registry.get(ch)  # handler for THIS char
                pre = buf[:i]
                fragment = "".join(buf[i + 1 :])
                if handler and handler.can_activate(pre):
                    self._trigger = _ActiveTrigger(
                        handler=handler,
                        char=ch,
                        fragment=fragment,
                        pre_buf=list(pre),
                    )
                    self._buf.set(list(pre))
                    self._update_matches()
                    break

    def _update_matches(self) -> None:
        if self._trigger is None:
            return
        self._matches = self._trigger.handler.get_matches(self._trigger.fragment, self._ctx)
        self._selected = 0
        self._scroll = 0
        self._hint = self._trigger.handler.get_hint(self._matches[0] if self._matches else None)

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> "RenderableType":
        """The prompt line, then the matches drawn by :class:`Picker` (TUX-05)."""
        from rich.console import Group
        from rich.text import Text
        from agenthicc.tui.input.renderer import build_prompt
        from agenthicc.tui.theme import glyphs
        from agenthicc.tui.workspace.picker import Picker, picker_height, rows_from_matches

        frag = self._trigger.fragment if self._trigger else ""
        tchar = self._trigger.char if self._trigger else ""
        prompt_buf = self._buf.buf
        prompt_cursor = self._buf.cursor
        if self._paste_state is not None:
            prompt_buf, prompt_cursor = self._paste_state._condensed_view(self._buf)
        prompt = build_prompt(
            prompt_buf,
            prompt_cursor,
            mention_suffix=tchar + frag if self._trigger else "",
            in_trigger=self._trigger is not None,
        )
        result_lines: list[RenderableType] = [Text.from_markup(prompt)]

        if self._matches and self._trigger:
            size = shutil.get_terminal_size((80, 24))
            g = glyphs()
            picker = Picker(
                rows_from_matches(self._matches),
                query=tchar + frag if tchar == "/" else frag,
                footer_keys=f"{g.up}{g.down} tab enter",
            )
            picker.selected = self._selected
            picker.scroll = self._scroll
            result_lines.extend(picker.render(size.columns, picker_height(size.lines)))
            self._scroll = picker.scroll

        return Group(*result_lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ESC:
                if self._trigger:
                    buf = self._trigger.handler.on_cancel(self._trigger.fragment, self._buf.buf)
                    self._complete(TriggerResult(buffer=buf))
                else:
                    self._complete(None)

            case Key.ENTER:
                # PRD-77: Enter with no match commits the text AND submits so
                # the user doesn't need a second Enter press.  Enter with a
                # match commits the selection (same as Tab).
                item = self._matches[self._selected] if self._matches else None
                if self._trigger:
                    result = self._trigger.handler.on_select(
                        item, self._trigger.fragment, self._buf.buf
                    )
                    if item is None:
                        result = TriggerResult(
                            buffer=result.buffer, submit=True, cursor=result.cursor
                        )
                    self._complete(result)
                else:
                    self._complete(None)

            case Key.TAB:
                # Tab commits the selection without submitting — user continues
                # typing after the inserted text.
                item = self._matches[self._selected] if self._matches else None
                if self._trigger:
                    result = self._trigger.handler.on_select(
                        item, self._trigger.fragment, self._buf.buf
                    )
                    self._complete(result)
                else:
                    self._complete(None)

            case Key.UP:
                if self._matches:
                    self._selected = (self._selected - 1) % len(self._matches)
                    if self._trigger:
                        self._hint = self._trigger.handler.get_hint(self._matches[self._selected])

            case Key.DOWN:
                if self._matches:
                    self._selected = (self._selected + 1) % len(self._matches)
                    if self._trigger:
                        self._hint = self._trigger.handler.get_hint(self._matches[self._selected])

            case Key.BACKSPACE:
                if self._trigger and self._trigger.fragment:
                    self._trigger.fragment = self._trigger.fragment[:-1]
                    self._update_matches()
                else:
                    self._complete(None)

            case Key.AT:
                # cbreak_reader reports every literal ``@`` as Key.AT rather
                # than Key.CHAR.  Once a picker is open, another ``@`` is
                # ordinary fragment text (for example ``@@user``), not a new
                # trigger and not a key to silently discard.
                if self._trigger:
                    self._trigger.fragment += "@"
                    self._update_matches()

            case Key.CHAR if ch:
                if self._trigger:
                    # Space is ordinary input.  It must never implicitly
                    # select the highlighted suggestion, invoke on_select(),
                    # submit, or close the picker.  This also keeps the
                    # behavior consistent for @mentions, slash commands, and
                    # plugin-provided triggers.  Argument completion remains
                    # available because the handler receives the updated
                    # fragment (for example, ``workflow ``).
                    self._trigger.fragment += ch
                    self._update_matches()

        return True  # overlay always consumes all keys
