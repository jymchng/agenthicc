"""ApprovalOverlay — shown in the Live block while an agent tool awaits approval (PRD-78).

Rendered by Workspace._build() via OverlayHost whenever
AppState.pending_approval is non-None.  Key routing goes through the
normal OverlayCapability → OverlayHost.handle_key() path.

Key bindings:
    y        Allow this call once
    a        Allow all remaining calls of the same capability class this turn
    A        Allow all remaining calls of the same capability class this session
    n / Esc  Deny — model receives {"ok": false, "error": "..."}
"""
from __future__ import annotations

import json
import shutil
from typing import Any, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay

_BORDER_CHAR = "─"


class ApprovalOverlay(Overlay):
    """Approval prompt shown while the agent is paused waiting for user input."""

    name = "approval"

    def __init__(
        self,
        req: Any,                    # ApprovalRequest (avoid circular import)
        service: Any,                # ApprovalService
        close_fn: Callable[[], None],
    ) -> None:
        self._req     = req
        self._service = service
        self._close   = close_fn

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        pass

    def render(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        cols     = shutil.get_terminal_size((80, 24)).columns
        req      = self._req
        cap_tags = ", ".join(sorted(str(c) for c in req.capabilities))

        lines: list[Any] = []

        # Header
        header = f"  ⚠  Tool Approval Required  [{_e(cap_tags)}]"
        lines.append(Text.from_markup(
            f"[bold yellow]{_e(header)}[/bold yellow]"
        ))
        lines.append(Text(_BORDER_CHAR * min(cols, 66), style="dim"))

        # Tool name
        lines.append(Text.from_markup(
            f"  [bold]{_e(req.tool_name)}[/bold]"
        ))

        # Truncated args — up to 3 key-value pairs, values up to 80 chars
        inp = req.tool_input or {}
        for key_name, val in list(inp.items())[:3]:
            if isinstance(val, str):
                display = val[:80] + ("…" if len(val) > 80 else "")
            else:
                try:
                    display = json.dumps(val, ensure_ascii=False)[:80]
                except Exception:  # noqa: BLE001
                    display = repr(val)[:80]
            lines.append(Text.from_markup(
                f"  [dim]{_e(key_name)}:[/dim] {_e(display)}"
            ))

        lines.append(Text(""))

        # Key bindings
        lines.append(Text.from_markup("  [bold]y[/bold] [dim]Allow once[/dim]"))
        lines.append(Text.from_markup(
            f"  [bold]a[/bold] [dim]Allow all {_e(cap_tags)} this turn[/dim]"
        ))
        lines.append(Text.from_markup(
            f"  [bold]A[/bold] [dim]Allow all {_e(cap_tags)} this session[/dim]"
        ))
        lines.append(Text.from_markup("  [bold]n / Esc[/bold] [dim]Deny[/dim]"))

        return Group(*lines)

    def handle_key(self, key: Key, ch: str) -> bool:
        match (key, ch):
            case (Key.CHAR, "y"):
                self._service.respond(True)
                self._close()
            case (Key.CHAR, "a"):
                self._service.respond(True, remember=True)
                self._close()
            case (Key.CHAR, "A"):
                self._service.respond(True, remember_all=True)
                self._close()
            case (Key.CHAR, "n") | (Key.ESC, _):
                self._service.respond(False)
                self._close()
            case _:
                pass  # consume but ignore any other key
        return True   # overlay always consumes all keys
