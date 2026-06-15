"""TriggerPickerOverlay — @mention / /command picker in the Live region (PRD-62 §3.4)."""
from __future__ import annotations

from typing import Any, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.input.buffer import InputBuffer


class TriggerPickerOverlay(Overlay):
    """Shows a dropdown picker for @mention and /command triggers.

    Rendered inside the always-on Live block — no pause/restart required.
    The picker is seeded with ``initial_buf`` (current text + trigger char),
    activates the trigger immediately, and calls ``on_complete`` when done.
    """

    name = "trigger_picker"
    _MAX_VISIBLE = 8

    def __init__(
        self,
        initial_buf: list[str],
        registry: Any,
        cwd: Any,
        on_complete: Callable[[str | None], None],
    ) -> None:
        from agenthicc.tui.trigger import TriggerContext   # noqa: PLC0415
        self._buf       = InputBuffer(initial_buf)
        self._registry  = registry
        self._cwd       = cwd
        self._complete  = on_complete
        self._ctx       = TriggerContext(cwd=cwd, history=[])
        self._trigger:  Any = None
        self._matches:  list = []
        self._selected: int = 0
        self._hint:     str | None = None
        self._init_trigger()

    def _init_trigger(self) -> None:
        """Activate the trigger from the last char in initial_buf."""
        buf = self._buf.buf
        if not buf:
            return
        last_char = buf[-1]
        handler = self._registry.get(last_char) if self._registry else None
        if handler is None:
            return

        # Find the position of the trigger char
        for i in range(len(buf) - 1, -1, -1):
            ch = buf[i]
            if ch in (self._registry.chars if self._registry else set()):
                pre      = buf[:i]
                fragment = "".join(buf[i + 1:])
                if handler.can_activate(pre):
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
        self._hint     = self._trigger.handler.get_hint(
            self._matches[0] if self._matches else None
        )

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> Any:
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
        lines = [Text.from_markup(prompt)]

        n      = min(self._MAX_VISIBLE, len(self._matches))
        scroll = max(0, min(self._selected - n + 1, len(self._matches) - n))
        for i, item in enumerate(self._matches[scroll:scroll + n]):
            actual    = scroll + i
            indicator = "▶" if actual == self._selected else " "
            style     = "reverse" if actual == self._selected else ""
            display   = item.display[:60] if hasattr(item, "display") else str(item)
            lines.append(Text(f"  {indicator} {display}", style=style))

        if self._hint:
            lines.append(Text(f"  [dim]{self._hint}[/dim]", style=""))

        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ESC:
                self._complete(None)
            case Key.ENTER | Key.TAB:
                item = self._matches[self._selected] if self._matches else None
                if self._trigger:
                    result_buf = self._trigger.handler.on_select(
                        item, self._trigger.fragment, self._buf.buf
                    )
                    self._complete("".join(result_buf))
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
            case Key.AT:
                if self._trigger:
                    self._trigger.fragment += "@"
                    self._update_matches()
            case Key.CHAR if ch:
                if self._trigger:
                    self._trigger.fragment += ch
                    self._update_matches()
        return True  # overlay consumes all keys
