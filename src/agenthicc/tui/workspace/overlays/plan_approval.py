"""PlanApprovalOverlay — three-option plan review overlay with text prompt (PRD-86).

Shown when a workflow's human_review phase calls ApprovalService with
kind="plan_review".  The user sees the plan content from the prior phase and
can approve, reject with typed feedback, or approve with typed instructions.

State machine:
    SELECTING  — ↑↓ navigate, Enter select, Esc deny
        ↓ Enter on option 1 or 2
    PROMPTING  — type feedback/instructions, Enter submit, Esc back

Height-stability contract
-------------------------
Rich's Live block (transient=True) clears exactly as many terminal lines as
the previous render produced.  If the render height varies between redraws,
leftover lines from taller renders bleed through.

Fix: pre-render the full plan Markdown once (_build_rendered_lines) into a
flat list[Text], one Text per terminal line.  The viewport always slices
exactly _PLAN_VISIBLE_LINES items from that list, so the content area
contributes a constant number of lines every render regardless of scroll
position.  Padding rows and a fixed indicator row ensure the total Group
height is identical on every redraw.  The cache is invalidated when the
terminal width changes.
"""
from __future__ import annotations

import shutil
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay

if TYPE_CHECKING:
    from rich.text import Text

_BORDER = "─"
_PLAN_VISIBLE_LINES = 20   # plan lines shown in the viewport at once

# (label, allowed, needs_prompt)
_OPTIONS: list[tuple[str, bool, bool]] = [
    ("Approve",                      True,  False),
    ("Reject — add feedback",        False, True),
    ("Approve — add instructions",   True,  True),
]


class _State(Enum):
    SELECTING = auto()
    PROMPTING = auto()


class PlanApprovalOverlay(PromptOverlay):
    """Plan review overlay: shows plan content and 3 selectable options."""

    name = "plan_approval"

    def __init__(
        self,
        req: Any,                    # ApprovalRequest with kind="plan_review"
        service: Any,                # ApprovalService
        close_fn: Callable[[], None],
    ) -> None:
        super().__init__()
        self._req            = req
        self._service        = service
        self._close          = close_fn
        self._state          = _State.SELECTING
        self._selected       = 0
        self._pending_option = 0   # option index carried into PROMPTING
        self._plan_scroll    = 0   # index of first visible rendered line

        # Pre-rendered line cache — rebuilt on mount and on terminal width change.
        self._rendered_lines: list[Text] = []
        self._render_width:   int        = 0

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        super().on_mount()
        self._state          = _State.SELECTING
        self._selected       = 0
        self._plan_scroll    = 0
        self._rendered_lines = []   # force rebuild on first render
        self._render_width   = 0

    def on_unmount(self) -> None:
        pass

    def render(self) -> Any:
        if self._state == _State.PROMPTING:
            return self._render_prompting()
        return self._render_selecting()

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._state == _State.PROMPTING:
            return self._handle_prompting(key, ch)
        return self._handle_selecting(key, ch)

    # ── pre-rendering ──────────────────────────────────────────────────────────

    def _build_rendered_lines(self, plan_text: str, width: int) -> None:
        """Render plan_text as Markdown into a flat list[Text], one item per
        terminal line.  Cached by width so terminal resizes invalidate it.
        """
        from io import StringIO                 # noqa: PLC0415
        from rich.console import Console       # noqa: PLC0415
        from rich.markdown import Markdown     # noqa: PLC0415
        from rich.text import Text             # noqa: PLC0415

        buf = StringIO()
        con = Console(
            file=buf, width=width, highlight=False,
            force_terminal=True, color_system="truecolor",
        )
        con.print(Markdown(plan_text), end="")
        raw = buf.getvalue()
        self._rendered_lines = [Text.from_ansi(ln) for ln in raw.splitlines()] or [Text("")]
        self._render_width   = width

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _render_selecting(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415

        cols     = shutil.get_terminal_size((80, 24)).columns
        border_w = min(cols, 66)
        lines: list[Any] = []

        lines.append(Text.from_markup("[bold cyan]  📋 Plan Review[/bold cyan]"))
        lines.append(Text(_BORDER * border_w, style="dim"))

        # ── scrollable plan viewport ──────────────────────────────────────────
        plan_text: str = self._req.tool_input.get("plan", "") if self._req.tool_input else ""
        if plan_text:
            # Rebuild the pre-rendered cache if cols changed or not yet built.
            content_width = cols - 4
            if not self._rendered_lines or self._render_width != content_width:
                self._build_rendered_lines(plan_text, content_width)

            total  = len(self._rendered_lines)
            scroll = max(0, min(self._plan_scroll, max(0, total - _PLAN_VISIBLE_LINES)))
            self._plan_scroll = scroll   # clamp in case terminal was resized

            visible = self._rendered_lines[scroll : scroll + _PLAN_VISIBLE_LINES]
            for ln in visible:
                # Prepend 2-space indent; append_text preserves all ANSI spans.
                prefixed = Text("  ")
                prefixed.append_text(ln)
                lines.append(prefixed)
            # Pad to exactly _PLAN_VISIBLE_LINES rows so the overlay height is
            # constant on every redraw.  Varying height causes the Rich Live
            # block to under-clear the previous render, bleeding old content.
            for _ in range(_PLAN_VISIBLE_LINES - len(visible)):
                lines.append(Text(""))

            # Indicator row — always emitted (blank when not needed) to keep
            # the total line count fixed regardless of scroll position.
            if total > _PLAN_VISIBLE_LINES:
                first  = scroll + 1
                last   = min(scroll + _PLAN_VISIBLE_LINES, total)
                above  = scroll > 0
                below  = last < total
                prefix = "↑ · " if above else ""
                suffix = " · ↓" if below else ""
                mid    = f"lines {first}–{last} of {total}"
                lines.append(Text(f"  {prefix}{mid}{suffix}", style="dim"))
            else:
                lines.append(Text(""))   # fixed-height placeholder
        else:
            lines.append(Text("  [no plan content]", style="dim"))
            for _ in range(_PLAN_VISIBLE_LINES - 1):
                lines.append(Text(""))
            lines.append(Text(""))   # indicator row placeholder

        lines.append(Text(_BORDER * border_w, style="dim"))

        # ── options ───────────────────────────────────────────────────────────
        for idx, (label, _, _) in enumerate(_OPTIONS):
            selected  = idx == self._selected
            indicator = "▶" if selected else " "
            style     = "reverse" if selected else ""
            lines.append(Text(f"  {indicator} {label}", style=style))

        lines.append(Text(_BORDER * border_w, style="dim"))
        lines.append(Text(
            "  ↑↓ options  [ up  ] down  Enter select  Esc deny",
            style="dim",
        ))

        return Group(*lines)

    def _handle_selecting(self, key: Key, ch: str) -> bool:
        total = len(self._rendered_lines)

        n = len(_OPTIONS)
        match key:
            case Key.UP:
                self._selected = (self._selected - 1) % n
            case Key.DOWN:
                self._selected = (self._selected + 1) % n
            case Key.ENTER:
                self._execute_option(self._selected)
            case Key.ESC:
                self._service.respond(allowed=False, message="")
                self._close()
            case Key.CHAR if ch == "[":
                self._plan_scroll = max(0, self._plan_scroll - 1)
            case Key.CHAR if ch == "]":
                max_scroll = max(0, total - _PLAN_VISIBLE_LINES)
                self._plan_scroll = min(max_scroll, self._plan_scroll + 1)
            case _:
                pass
        return True

    def _execute_option(self, idx: int) -> None:
        label, allowed, needs_prompt = _OPTIONS[idx]
        if not needs_prompt:
            # Option 0 — Approve immediately
            self._service.respond(allowed=allowed, message="")
            self._close()
        else:
            # Options 1 & 2 — enter PROMPTING state
            self._pending_option = idx
            self._buf.clear()
            self._state = _State.PROMPTING

    # ── PROMPTING ─────────────────────────────────────────────────────────────

    def _render_prompting(self) -> Any:
        from rich.console import Group        # noqa: PLC0415
        from rich.text import Text            # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        cols  = shutil.get_terminal_size((80, 24)).columns
        label = _OPTIONS[self._pending_option][0]
        lines: list[Any] = []

        lines.append(Text.from_markup(
            f"[bold cyan]  📋 Plan Review[/bold cyan]"
            f"[dim] › {_e(label)}[/dim]"
        ))
        lines.append(Text(_BORDER * min(cols, 66), style="dim"))
        lines.append(Text.from_markup(f"  {self._render_prompt_line()}"))
        lines.append(Text(_BORDER * min(cols, 66), style="dim"))
        lines.append(Text("  Enter submit  Esc back", style="dim"))

        return Group(*lines)

    def _handle_prompting(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ENTER:
                label, allowed, _ = _OPTIONS[self._pending_option]
                self._service.respond(allowed=allowed, message=self._prompt_text)
                self._close()
            case Key.ESC:
                # Back to SELECTING without submitting
                self._buf.clear()
                self._state = _State.SELECTING
            case _:
                self._handle_prompt_key(key, ch)
        return True
