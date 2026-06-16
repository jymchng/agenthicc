"""PlanApprovalOverlay — three-option plan review overlay with text prompt (PRD-86).

Shown when a workflow's human_review phase calls ApprovalService with
kind="plan_review".  The user sees the plan content from the prior phase and
can approve, reject with typed feedback, or approve with typed instructions.

State machine:
    SELECTING  — ↑↓ navigate, Enter select, Esc deny
        ↓ Enter on option 1 or 2
    PROMPTING  — type feedback/instructions, Enter submit, Esc back
"""
from __future__ import annotations

import shutil
from enum import Enum, auto
from typing import Any, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay

_BORDER = "─"
_PLAN_VISIBLE_LINES = 10   # plan lines shown in the viewport at once

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
        self._plan_scroll    = 0   # index of first visible plan line (PRD-88)

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        super().on_mount()
        self._state       = _State.SELECTING
        self._selected    = 0
        self._plan_scroll = 0

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

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _render_selecting(self) -> Any:
        from rich.console import Group        # noqa: PLC0415
        from rich.text import Text            # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        cols       = shutil.get_terminal_size((80, 24)).columns
        border_w   = min(cols, 66)
        lines: list[Any] = []

        lines.append(Text.from_markup("[bold cyan]  📋 Plan Review[/bold cyan]"))
        lines.append(Text(_BORDER * border_w, style="dim"))

        # ── scrollable plan viewport ──────────────────────────────────────────
        plan_text: str = self._req.tool_input.get("plan", "") if self._req.tool_input else ""
        if plan_text:
            plan_lines  = plan_text.splitlines()
            total       = len(plan_lines)
            scroll      = max(0, min(self._plan_scroll, max(0, total - _PLAN_VISIBLE_LINES)))
            self._plan_scroll = scroll   # clamp in case terminal was resized
            visible     = plan_lines[scroll : scroll + _PLAN_VISIBLE_LINES]

            for ln in visible:
                lines.append(Text.from_markup(f"  [dim]{_e(ln[:cols - 4])}[/dim]"))

            # Scroll position indicator (only when plan overflows viewport)
            if total > _PLAN_VISIBLE_LINES:
                first  = scroll + 1
                last   = min(scroll + _PLAN_VISIBLE_LINES, total)
                above  = scroll > 0
                below  = last < total
                prefix = "↑ · " if above else ""
                suffix = " · ↓" if below else ""
                mid    = f"lines {first}–{last} of {total}"
                label  = f"  {prefix}{mid}{suffix}"
                lines.append(Text(label, style="dim"))
        else:
            lines.append(Text("  [no plan content]", style="dim"))

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
        plan_text: str = self._req.tool_input.get("plan", "") if self._req.tool_input else ""
        total = len(plan_text.splitlines()) if plan_text else 0

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
