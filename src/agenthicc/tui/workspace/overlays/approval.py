"""ApprovalOverlay — shown in the Live block while an agent tool awaits approval (PRD-78).

Options are selectable with Up/Down and confirmed with Enter.
Hotkeys (y/a/A/n) still work for quick access.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.tools.approval import ApprovalRequest, ApprovalService

_BORDER_CHAR = "─"

# (hotkey, label_template, respond kwargs)
# {caps} in label_template is replaced with the capability string at render time.
_OPTIONS: list[tuple[str, str, dict]] = [
    ("y", "Allow once", dict(allowed=True)),
    ("a", "Allow all {caps} this turn", dict(allowed=True, remember=True)),
    ("A", "Allow all {caps} this session", dict(allowed=True, remember_all=True)),
    ("n", "Deny", dict(allowed=False)),
]


def _cap_str(capabilities: frozenset) -> str:
    """Format capability set as a short human-readable string."""
    parts = []
    for c in sorted(capabilities):
        # ToolCapability inherits from str — .value gives the raw string ("write").
        parts.append(getattr(c, "value", str(c)))
    return ", ".join(parts)


class ApprovalOverlay(Overlay):
    """Approval prompt shown while the agent is paused waiting for user input."""

    name = "approval"

    def __init__(
        self,
        req: ApprovalRequest,
        service: ApprovalService,
        close_fn: Callable[[], None],
    ) -> None:
        self._req = req
        self._service = service
        self._close = close_fn
        self._selected = 0  # index into _OPTIONS
        self._scroll = 0  # first visible option index

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._selected = 0
        self._scroll = 0

    def on_unmount(self) -> None:
        pass

    def render(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        cols = shutil.get_terminal_size((80, 24)).columns
        req = self._req
        cap_tags = _cap_str(req.capabilities)

        lines: list[RenderableType] = []

        # ── header ────────────────────────────────────────────────────────────
        lines.append(
            Text.from_markup(
                f"[bold yellow]  ⚠  Tool Approval Required  [{_e(cap_tags)}][/bold yellow]"
            )
        )
        lines.append(Text(_BORDER_CHAR * min(cols, 66), style="dim"))

        # ── tool name ─────────────────────────────────────────────────────────
        lines.append(Text.from_markup(f"  [bold]{_e(req.tool_name)}[/bold]"))

        # ── truncated args — up to 3 key/value pairs ─────────────────────────
        inp = req.tool_input or {}
        for key_name, val in list(inp.items())[:3]:
            if isinstance(val, str):
                display = val[:80] + ("…" if len(val) > 80 else "")
            else:
                try:
                    display = json.dumps(val, ensure_ascii=False)[:80]
                except Exception:  # noqa: BLE001
                    display = repr(val)[:80]
            lines.append(Text.from_markup(f"  [dim]{_e(key_name)}:[/dim] {_e(display)}"))

        lines.append(Text(""))

        # ── selectable options ────────────────────────────────────────────────
        n = len(_OPTIONS)
        # Clamp scroll so selected is visible within a 6-line window.
        max_visible = 6
        if self._selected < self._scroll:
            self._scroll = self._selected
        elif self._selected >= self._scroll + max_visible:
            self._scroll = self._selected - max_visible + 1

        for idx in range(self._scroll, min(self._scroll + max_visible, n)):
            hotkey, label_tmpl, _ = _OPTIONS[idx]
            label = label_tmpl.format(caps=cap_tags)
            selected = idx == self._selected
            indicator = "▶" if selected else " "
            style = "reverse" if selected else ""
            lines.append(
                Text(
                    f"  {indicator} [{hotkey}] {label}",
                    style=style,
                )
            )

        # scroll hint
        if self._scroll + max_visible < n:
            lines.append(Text("  ↓ more…", style="dim"))

        lines.append(Text(""))
        lines.append(Text("  ↑↓ navigate  Enter confirm", style="dim"))

        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        n = len(_OPTIONS)

        match key:
            case Key.UP:
                self._selected = (self._selected - 1) % n
            case Key.DOWN:
                self._selected = (self._selected + 1) % n
            case Key.ENTER:
                self._execute(self._selected)
            case Key.ESC:
                # ESC always denies
                self._respond(dict(allowed=False))
            case Key.CHAR if ch:
                # Hotkey fast-path
                for idx, (hotkey, _, _) in enumerate(_OPTIONS):
                    if ch == hotkey:
                        self._execute(idx)
                        break
            case _:
                pass  # consume but ignore

        return True  # overlay always consumes all keys

    # ── helpers ───────────────────────────────────────────────────────────────

    def _execute(self, idx: int) -> None:
        _, _, kwargs = _OPTIONS[idx]
        self._respond(kwargs)

    def _respond(self, kwargs: dict) -> None:
        self._service.respond(**kwargs)
        self._close()
