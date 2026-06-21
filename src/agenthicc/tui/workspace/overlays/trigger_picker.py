"""TriggerPickerOverlay — @mention / /command picker in the Live region (PRD-62, PRD-69)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from rich.console import RenderableType

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.input.buffer import InputBuffer


class TriggerPickerOverlay(Overlay):
    """Shows a dropdown picker for @mention and /command triggers.

    Rendered inside the always-on Live block — no pause/restart required.
    The picker is seeded with ``initial_buf`` (current text + trigger char),
    activates the trigger immediately, and calls ``on_complete`` when done.

    PRD-69: uses handler.get_lines() for line-height-aware scroll so long
    descriptions can wrap without overflowing the visible window.
    """

    name = "trigger_picker"
    _MAX_LINES = 12   # terminal-line budget for the dropdown (not item count)

    def __init__(
        self,
        initial_buf: list[str],
        registry: object,
        cwd: "Path",
        on_complete: Callable[[object], None],
    ) -> None:
        from agenthicc.tui.trigger import TriggerContext   # noqa: PLC0415
        self._buf       = InputBuffer(initial_buf)
        self._registry  = registry
        self._cwd       = cwd
        self._complete  = on_complete
        self._ctx       = TriggerContext(cwd=cwd)
        self._trigger:  object = None
        self._matches:  list = []
        self._selected: int = 0   # index into self._matches
        self._scroll:   int = 0   # index of first visible item
        self._hint:     str | None = None
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
                break   # stop at a word boundary; no trigger activates here
            if ch in chars:
                handler  = self._registry.get(ch)   # handler for THIS char
                pre      = buf[:i]
                fragment = "".join(buf[i + 1:])
                if handler and handler.can_activate(pre):
                    from types import SimpleNamespace  # noqa: PLC0415
                    self._trigger = SimpleNamespace(
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
        self._matches  = self._trigger.handler.get_matches(self._trigger.fragment, self._ctx)
        self._selected = 0
        self._scroll   = 0
        self._hint     = self._trigger.handler.get_hint(
            self._matches[0] if self._matches else None
        )

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> "RenderableType":
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415
        from agenthicc.tui.input.renderer import build_prompt  # noqa: PLC0415

        frag   = self._trigger.fragment if self._trigger else ""
        tchar  = self._trigger.char if self._trigger else ""
        prompt = build_prompt(
            self._buf.buf, self._buf.cursor,
            mention_suffix=tchar + frag if self._trigger else "",
            in_trigger=self._trigger is not None,
        )
        result_lines: list[RenderableType] = [Text.from_markup(prompt)]

        if self._matches and self._trigger:
            handler = self._trigger.handler
            cols    = shutil.get_terminal_size((80, 24)).columns
            # 4-char prefix "  ▶ " / "    " is added by us below
            avail_w = max(cols - 4, 20)

            item_line_lists = [
                handler.get_lines(item, avail_w) for item in self._matches
            ]

            # Clamp scroll so selected item is fully visible within _MAX_LINES.
            self._clamp_scroll(item_line_lists)

            # Render items from scroll position until line budget exhausted.
            lines_used = 0
            for idx in range(self._scroll, len(self._matches)):
                item_lines = item_line_lists[idx]
                if lines_used + len(item_lines) > self._MAX_LINES:
                    break
                selected  = idx == self._selected
                indicator = "▶" if selected else " "
                style     = "reverse" if selected else ""
                for li, line_text in enumerate(item_lines):
                    prefix = f"  {indicator} " if li == 0 else "    "
                    result_lines.append(Text(prefix + line_text, style=style))
                lines_used += len(item_lines)

        if self._hint:
            result_lines.append(Text(f"  {self._hint}", style="dim"))

        return Group(*result_lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ESC:
                if self._trigger:
                    buf = self._trigger.handler.on_cancel(
                        self._trigger.fragment, self._buf.buf
                    )
                    from agenthicc.tui.trigger import TriggerResult  # noqa: PLC0415
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
                        from agenthicc.tui.trigger import TriggerResult  # noqa: PLC0415
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
                        self._hint = self._trigger.handler.get_hint(
                            self._matches[self._selected]
                        )

            case Key.DOWN:
                if self._matches:
                    self._selected = (self._selected + 1) % len(self._matches)
                    if self._trigger:
                        self._hint = self._trigger.handler.get_hint(
                            self._matches[self._selected]
                        )

            case Key.BACKSPACE:
                if self._trigger and self._trigger.fragment:
                    self._trigger.fragment = self._trigger.fragment[:-1]
                    self._update_matches()
                else:
                    self._complete(None)

            case Key.CHAR if ch:
                if self._trigger:
                    if ch == " ":
                        # Space commits the selected item so the user can type
                        # arguments without a second Enter.
                        item = self._matches[self._selected] if self._matches else None
                        result = self._trigger.handler.on_select(
                            item, self._trigger.fragment, self._buf.buf
                        )
                        from agenthicc.tui.trigger import TriggerResult  # noqa: PLC0415
                        spaced = TriggerResult(
                            buffer=result.buffer + [" "],
                            submit=result.submit,
                            cursor=result.cursor,
                        )
                        self._complete(spaced)
                    else:
                        # Any char (including "@", "#", etc.) extends the fragment.
                        self._trigger.fragment += ch
                        self._update_matches()

        return True  # overlay always consumes all keys

    # ── scroll helpers ─────────────────────────────────────────────────────────

    def _clamp_scroll(self, item_line_lists: list[list[str]]) -> None:
        """Adjust self._scroll so the selected item is fully visible."""
        if not item_line_lists:
            return

        sel = self._selected

        # If selected item is above the window, scroll up.
        if sel < self._scroll:
            self._scroll = sel
            return

        # Count lines from _scroll to sel (inclusive) to check if sel is visible.
        lines_to_sel_end = sum(
            len(item_line_lists[i]) for i in range(self._scroll, sel + 1)
        )
        if lines_to_sel_end <= self._MAX_LINES:
            return  # already fully visible

        # Selected item is below the window — advance scroll until it fits.
        while self._scroll <= sel:
            self._scroll += 1
            lines_to_sel_end = sum(
                len(item_line_lists[i]) for i in range(self._scroll, sel + 1)
            )
            if lines_to_sel_end <= self._MAX_LINES:
                break
