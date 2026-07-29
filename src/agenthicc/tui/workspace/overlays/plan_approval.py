"""PlanApprovalOverlay — plan review overlay with execution-mode choices.

Shown when a workflow's human_review phase calls ApprovalService with
kind="plan_review".  The user sees the plan content from the prior phase and
can approve, reject with typed feedback, or approve with typed instructions.

State machine:
    SELECTING       — ↑↓ navigate, Enter select, Esc deny
        ↓ Enter on a prompted option
    PROMPTING       — type feedback/instructions, Enter submit, Esc back
        ↓ Enter on "Approve — add instructions" for a mode choice
    MODE_SELECTING  — choose Safe or YOLO for the execute phase

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
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text
    from agenthicc.tools.approval import ApprovalRequest, ApprovalService

_BORDER = "─"
_PLAN_VISIBLE_LINES = 20  # plan lines shown in the viewport at once

# (label, allowed, needs_prompt, execute_mode)
_OptionSpec = tuple[str, bool, bool, str | None]
_OPTIONS: list[tuple[str, bool, bool]] = [
    ("Approve", True, False),
    ("Reject — add feedback", False, True),
    ("Approve — add instructions", True, True),
]


class _State(Enum):
    SELECTING = auto()
    PROMPTING = auto()
    MODE_SELECTING = auto()


class PlanApprovalOverlay(PromptOverlay):
    """Plan review overlay with optional Safe/YOLO execution choices."""

    name = "plan_approval"

    def __init__(
        self,
        req: ApprovalRequest,
        service: ApprovalService,
        close_fn: Callable[[], None],
    ) -> None:
        super().__init__()
        self._req = req
        self._service = service
        self._close = close_fn
        self._state = _State.SELECTING
        self._selected = 0
        self._pending_option = 0  # option index carried into PROMPTING
        self._pending_message = ""
        self._mode_selected = 0
        self._plan_scroll = 0  # index of first visible rendered line

        # Pre-rendered line cache — rebuilt on mount and on terminal width change.
        self._rendered_lines: list[Text] = []
        self._render_width: int = 0
        # Cached from last render — read by _handle_selecting.
        self._plan_visible: int = _PLAN_VISIBLE_LINES

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        super().on_mount()
        self._state = _State.SELECTING
        self._selected = 0
        self._pending_message = ""
        self._mode_selected = 0
        self._plan_scroll = 0
        self._rendered_lines = []  # force rebuild on first render
        self._render_width = 0
        self._plan_visible = _PLAN_VISIBLE_LINES

    def on_unmount(self) -> None:
        pass

    def render(self) -> RenderableType:
        if self._state == _State.PROMPTING:
            return self._render_prompting()
        if self._state == _State.MODE_SELECTING:
            return self._render_mode_selecting()
        return self._render_selecting()

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._state == _State.PROMPTING:
            return self._handle_prompting(key, ch)
        if self._state == _State.MODE_SELECTING:
            return self._handle_mode_selecting(key, ch)
        return self._handle_selecting(key, ch)

    def _option_specs(self) -> list[_OptionSpec]:
        """Return the options for this request.

        Legacy plan-review requests (including create_workflow's design
        review) retain their original three-option contract.  Code-plan
        requests opt into explicit execution-mode choices through
        ``ApprovalRequest.mode_options``.
        """
        if not self._req.mode_options:
            return [(label, allowed, prompt, None) for label, allowed, prompt in _OPTIONS]
        return [
            ("Approve - Safe", True, False, "Safe"),
            ("Approve - YOLO", True, False, "Yolo"),
            ("Reject — add feedback", False, True, None),
            ("Approve — add instructions", True, True, None),
        ]

    def _mode_values(self) -> list[str]:
        """Return supported, canonical modes advertised by the request."""
        values: list[str] = []
        for value in self._req.mode_options:
            canonical = {"safe": "Safe", "yolo": "Yolo"}.get(value.casefold())
            if canonical is not None and canonical not in values:
                values.append(canonical)
        return values or ["Safe", "Yolo"]

    def _respond(
        self,
        *,
        allowed: bool,
        message: str = "",
        mode: str | None = None,
    ) -> None:
        """Respond while preserving the old call shape for legacy overlays."""
        if mode is None:
            self._service.respond(allowed=allowed, message=message)
        else:
            self._service.respond(allowed=allowed, message=message, mode=mode)

    # ── pre-rendering ──────────────────────────────────────────────────────────

    def _build_rendered_lines(self, plan_text: str, width: int) -> None:
        """Render plan_text as Markdown into a flat list[Text], one item per
        terminal line.  Cached by width so terminal resizes invalidate it.
        """
        from io import StringIO  # noqa: PLC0415
        from rich.console import Console  # noqa: PLC0415
        from rich.markdown import Markdown  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        buf = StringIO()
        con = Console(
            file=buf,
            width=width,
            highlight=False,
            force_terminal=True,
            color_system="truecolor",
        )
        con.print(Markdown(plan_text), end="")
        raw = buf.getvalue()
        self._rendered_lines = [Text.from_ansi(ln) for ln in raw.splitlines()] or [Text("")]
        self._render_width = width

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _render_selecting(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        term = shutil.get_terminal_size((80, 24))
        cols = term.columns
        rows = term.lines
        border_w = min(cols, 66)
        # Overhead = 19 fixed rows surrounding the plan content:
        #   workspace (blank+status+top-border) = 5
        #   overlay   (header+top-border+indicator+bottom-border+
        #              3-options+bottom-border+hint) = 7 + 2 = 9
        #   workspace (bottom-border+footer+margin) = 5
        # plan_visible = max(1, rows − 19) — floor at 1 so the footer always fits.
        plan_visible = min(_PLAN_VISIBLE_LINES, max(1, rows - 19))
        self._plan_visible = plan_visible
        lines: list[RenderableType] = []

        lines.append(Text.from_markup("[bold cyan]  📋 Plan Review[/bold cyan]"))
        lines.append(Text(_BORDER * border_w, style="dim"))

        # ── scrollable plan viewport ──────────────────────────────────────────
        raw_plan = self._req.tool_input.get("plan", "") if self._req.tool_input else ""
        plan_text = raw_plan if isinstance(raw_plan, str) else str(raw_plan)
        if plan_text:
            # Rebuild the pre-rendered cache if cols changed or not yet built.
            content_width = cols - 4
            if not self._rendered_lines or self._render_width != content_width:
                self._build_rendered_lines(plan_text, content_width)

            total = len(self._rendered_lines)
            scroll = max(0, min(self._plan_scroll, max(0, total - plan_visible)))
            self._plan_scroll = scroll  # clamp in case terminal was resized

            visible = self._rendered_lines[scroll : scroll + plan_visible]
            for ln in visible:
                # Prepend 2-space indent; append_text preserves all ANSI spans.
                prefixed = Text("  ")
                prefixed.append_text(ln)
                lines.append(prefixed)
            # Pad to exactly plan_visible rows so the overlay height is
            # constant on every redraw.  Varying height causes the Rich Live
            # block to under-clear the previous render, bleeding old content.
            for _ in range(plan_visible - len(visible)):
                lines.append(Text(""))

            # Indicator row — always emitted (blank when not needed) to keep
            # the total line count fixed regardless of scroll position.
            if total > plan_visible:
                first = scroll + 1
                last = min(scroll + plan_visible, total)
                above = scroll > 0
                below = last < total
                prefix = "↑ · " if above else ""
                suffix = " · ↓" if below else ""
                mid = f"lines {first}–{last} of {total}"
                lines.append(Text(f"  {prefix}{mid}{suffix}", style="dim"))
            else:
                lines.append(Text(""))  # fixed-height placeholder
        else:
            lines.append(Text("  [no plan content]", style="dim"))
            for _ in range(plan_visible - 1):
                lines.append(Text(""))
            lines.append(Text(""))  # indicator row placeholder

        lines.append(Text(_BORDER * border_w, style="dim"))

        # ── options ───────────────────────────────────────────────────────────
        for idx, (label, _, _, _) in enumerate(self._option_specs()):
            selected = idx == self._selected
            indicator = "▶" if selected else " "
            style = "reverse" if selected else ""
            lines.append(Text(f"  {indicator} {label}", style=style))

        lines.append(Text(_BORDER * border_w, style="dim"))
        lines.append(
            Text(
                "  ↑↓ options  [ up  ] down  Enter select  Esc deny",
                style="dim",
            )
        )

        return Group(*lines)

    def _handle_selecting(self, key: Key, ch: str) -> bool:
        total = len(self._rendered_lines)

        n = len(self._option_specs())
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
                max_scroll = max(0, total - self._plan_visible)
                self._plan_scroll = min(max_scroll, self._plan_scroll + 1)
            case _:
                pass
        return True

    def _execute_option(self, idx: int) -> None:
        _, allowed, needs_prompt, mode = self._option_specs()[idx]
        if not needs_prompt:
            self._respond(allowed=allowed, message="", mode=mode)
            self._close()
        else:
            # Prompted options enter PROMPTING.  For a mode-aware request,
            # approving with instructions continues to MODE_SELECTING after
            # the text is entered.
            self._pending_option = idx
            self._buf.clear()
            self._state = _State.PROMPTING

    # ── PROMPTING ─────────────────────────────────────────────────────────────

    def _render_prompting(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415

        cols = shutil.get_terminal_size((80, 24)).columns
        label = self._option_specs()[self._pending_option][0]
        lines: list[RenderableType] = []

        lines.append(
            Text.from_markup(f"[bold cyan]  📋 Plan Review[/bold cyan][dim] › {_e(label)}[/dim]")
        )
        lines.append(Text(_BORDER * min(cols, 66), style="dim"))
        lines.append(Text.from_markup(f"  {self._render_prompt_line()}"))
        lines.append(Text(_BORDER * min(cols, 66), style="dim"))
        lines.append(Text("  Enter submit  Esc back", style="dim"))

        return Group(*lines)

    def _handle_prompting(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ENTER:
                _, allowed, _, mode = self._option_specs()[self._pending_option]
                if allowed and mode is None and self._req.mode_options:
                    self._pending_message = self._prompt_text
                    self._mode_selected = 0
                    self._buf.clear()
                    self._state = _State.MODE_SELECTING
                else:
                    self._respond(allowed=allowed, message=self._prompt_text, mode=mode)
                    self._close()
            case Key.ESC:
                # Back to SELECTING without submitting
                self._buf.clear()
                self._state = _State.SELECTING
            case _:
                self._handle_prompt_key(key, ch)
        return True

    # ── MODE_SELECTING ──────────────────────────────────────────────────────

    def _render_mode_selecting(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.markup import escape as _e  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        cols = shutil.get_terminal_size((80, 24)).columns
        modes = self._mode_values()
        label = self._option_specs()[self._pending_option][0]
        lines: list[RenderableType] = [
            Text.from_markup(f"[bold cyan]  📋 Plan Review[/bold cyan][dim] › {_e(label)}[/dim]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  Choose the execute mode:", style="bold"),
        ]
        for idx, mode in enumerate(modes):
            selected = idx == self._mode_selected
            lines.append(
                Text(f"  {'▶' if selected else ' '} {mode}", style="reverse" if selected else "")
            )
        lines.extend(
            [
                Text(_BORDER * min(cols, 66), style="dim"),
                Text("  ↑↓ modes  Enter select  Esc back", style="dim"),
            ]
        )
        return Group(*lines)

    def _handle_mode_selecting(self, key: Key, ch: str) -> bool:
        modes = self._mode_values()
        match key:
            case Key.UP:
                self._mode_selected = (self._mode_selected - 1) % len(modes)
            case Key.DOWN:
                self._mode_selected = (self._mode_selected + 1) % len(modes)
            case Key.ENTER:
                self._respond(
                    allowed=True,
                    message=self._pending_message,
                    mode=modes[self._mode_selected],
                )
                self._close()
            case Key.ESC:
                self._buf.clear()
                for char in self._pending_message:
                    self._buf.insert(char)
                self._state = _State.PROMPTING
            case _:
                pass
        return True
