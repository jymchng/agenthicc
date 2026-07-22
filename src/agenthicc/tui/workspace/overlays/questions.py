"""QuestionsOverlay — multi-question overlay with selectable options (PRD-100).

Shown when an agent calls ask_user().  The user navigates questions with ←/→
and options with ↑/↓.  Each question has LLM-chosen options plus an "Other"
free-text fallback.  A single Enter on the last unanswered question submits
all answers.

State machine:
    SELECTING  — ↑↓ option, ←→ question, Enter confirm/submit, Esc cancel
    TYPING     — free-text entry for the "Other" option of the current question

Height stability
----------------
The options area is always rendered as exactly ``_opt_rows`` lines (computed
dynamically from the terminal height), padded with blank rows when a question
has fewer options than the tallest question.  This keeps the overlay height
constant while navigating between questions.

Fixed overhead = 19 lines: workspace (blank+status+top-border=5) +
overlay chrome (header+top-border+nav+2-blanks+question+blank-after-options+
bottom-border+hint=9) + workspace (bottom-border+footer+margin=5).
opt_rows = min(max_opts_any_question, max(1, rows − 19)).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from rich.console import RenderableType
    from agenthicc.tools.approval import ApprovalRequest, ApprovalService

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay

_BORDER = "─"
_OTHER_LABEL = "Other — type your answer"


def _str_option(opt: object) -> str:
    """Normalise an option to a plain string.

    The LLM sometimes passes dicts (e.g. {'id': 'a', 'label': 'Frontend …'})
    instead of plain strings.  Prefer 'label', then 'text', 'name', 'value',
    then the first value of the dict, then str() as last resort.
    """
    if isinstance(opt, str):
        return opt
    if isinstance(opt, dict):
        for key in ("label", "text", "name", "value"):
            if opt.get(key):
                return str(opt[key])
        for v in opt.values():
            if v:
                return str(v)
    return str(opt)


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    options: list[str]


@dataclass
class _QState:
    cursor: int = 0  # highlighted option index (absolute)
    answer: str = ""  # confirmed answer (option label or typed text)
    answered: bool = False
    opt_scroll: int = 0  # index of first visible option in the viewport


class _Mode(Enum):
    SELECTING = auto()
    TYPING = auto()


class QuestionsOverlay(PromptOverlay):
    """Multi-question overlay driven by ask_user()."""

    name = "questions"

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
        self._mode = _Mode.SELECTING
        self._current = 0

        raw: list[dict] = (req.tool_input or {}).get("questions", [])
        self._questions: list[Question] = [
            Question(
                id=q["id"],
                text=q["text"],
                options=[_str_option(o) for o in q["options"]],
            )
            for q in raw
        ]
        self._states: list[_QState] = [_QState() for _ in self._questions]

        # Cached from last render — read by _handle_selecting.
        self._opt_rows: int = 2

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        super().on_mount()
        self._mode = _Mode.SELECTING
        self._current = 0
        for s in self._states:
            s.cursor = 0
            s.answer = ""
            s.answered = False
            s.opt_scroll = 0

    def on_unmount(self) -> None:
        pass

    def render(self) -> RenderableType:
        if not self._questions:
            return self._render_empty()
        if self._mode == _Mode.TYPING:
            return self._render_typing()
        return self._render_selecting()

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._mode == _Mode.TYPING:
            return self._handle_typing(key, ch)
        return self._handle_selecting(key, ch)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _opts(self, q_idx: int) -> list[str]:
        """Options for question q_idx with 'Other' appended."""
        return self._questions[q_idx].options + [_OTHER_LABEL]

    def _is_other_idx(self, q_idx: int, opt_idx: int) -> bool:
        return opt_idx == len(self._opts(q_idx)) - 1

    def _is_free_text_answer(self, q_idx: int) -> bool:
        """True when question was answered via free text (not one of the preset options)."""
        st = self._states[q_idx]
        return st.answered and st.answer not in self._questions[q_idx].options

    def _opt_label(self, q_idx: int, opt_idx: int) -> str:
        if self._is_other_idx(q_idx, opt_idx) and self._is_free_text_answer(q_idx):
            text = self._states[q_idx].answer
            if len(text) > 40:
                text = text[:39] + "…"
            return f'Other: "{text}"'
        return self._opts(q_idx)[opt_idx]

    def _all_answered(self) -> bool:
        return all(s.answered for s in self._states)

    def _advance(self) -> None:
        """Move focus to the next unanswered question."""
        n = len(self._questions)
        for i in range(1, n + 1):
            idx = (self._current + i) % n
            if not self._states[idx].answered:
                self._current = idx
                return

    def _submit(self) -> None:
        import json  # noqa: PLC0415

        answers = {q.id: s.answer for q, s in zip(self._questions, self._states)}
        self._service.respond(allowed=True, message=json.dumps(answers))
        self._close()

    def _clamp_opt_scroll(self, q_idx: int) -> None:
        """Ensure opt_scroll keeps the cursor inside the visible options window."""
        st = self._states[q_idx]
        n = len(self._opts(q_idx))
        opt_rows = self._opt_rows
        # Cursor below viewport → scroll down.
        if st.cursor >= st.opt_scroll + opt_rows:
            st.opt_scroll = st.cursor - opt_rows + 1
        # Cursor above viewport → scroll up.
        if st.cursor < st.opt_scroll:
            st.opt_scroll = st.cursor
        # Clamp scroll to valid range.
        st.opt_scroll = max(0, min(st.opt_scroll, max(0, n - opt_rows)))

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _render_selecting(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        term = shutil.get_terminal_size((80, 24))
        cols = term.columns
        rows = term.lines
        border_w = min(cols, 66)

        # Max options (including "Other") across all questions.
        max_opts = max((len(self._opts(i)) for i in range(len(self._questions))), default=2)
        # Overhead = 17 lines (18 minus the 1-row workspace Layout margin).
        # Overhead = 19 fixed rows; floor at 1 so the footer always fits.
        opt_rows = min(max_opts, max(1, rows - 19))
        self._opt_rows = opt_rows  # read by _handle_selecting

        lines: list[RenderableType] = []

        n_ans = sum(1 for s in self._states if s.answered)
        n_total = len(self._questions)

        # Header
        lines.append(
            Text.from_markup(
                f"[bold cyan]  ❓ Questions[/bold cyan][dim]  ({n_ans} of {n_total} answered)[/dim]"
            )
        )
        lines.append(Text(_BORDER * border_w, style="dim"))

        # Navigation + dot indicators
        q_idx = self._current
        left = "◀ " if q_idx > 0 else "  "
        right = " ▶" if q_idx < n_total - 1 else "  "
        nav = f"{left}Question {q_idx + 1} of {n_total}{right}"
        dots = " ".join("●" if s.answered else "○" for s in self._states)
        gap = max(1, border_w - 4 - len(nav) - len(dots))
        lines.append(Text(f"  {nav}" + " " * gap + dots, style="dim"))
        lines.append(Text(""))

        # Question text
        q = self._questions[q_idx]
        st = self._states[q_idx]
        lines.append(Text(f"  {q.text}"))
        lines.append(Text(""))

        # Options viewport — always exactly opt_rows lines.
        opts = self._opts(q_idx)
        n = len(opts)
        self._clamp_opt_scroll(q_idx)
        scroll = st.opt_scroll
        end = min(scroll + opt_rows, n)
        shown = end - scroll

        for i in range(scroll, end):
            opt = opts[i]
            is_cursor = i == st.cursor
            is_other = self._is_other_idx(q_idx, i)
            is_answer = st.answered and (
                (is_other and self._is_free_text_answer(q_idx))
                or (not is_other and st.answer == opt)
            )
            label = self._opt_label(q_idx, i)

            if is_cursor:
                indicator = "▶"
                style = "reverse"
            elif is_answer:
                indicator = "✓"
                style = ""
            else:
                indicator = " "
                style = "dim"

            lines.append(Text(f"  {indicator} {label}", style=style))

        # Pad to exactly opt_rows for height stability.
        for _ in range(opt_rows - shown):
            lines.append(Text(""))

        lines.append(Text(""))
        lines.append(Text(_BORDER * border_w, style="dim"))

        if self._all_answered():
            hint = "  ↑↓ option   ←→ question   Enter SUBMIT ALL   Esc cancel"
        else:
            hint = "  ↑↓ option   ←→ question   Enter confirm   Esc cancel"
        lines.append(Text(hint, style="dim"))

        return Group(*lines)

    def _handle_selecting(self, key: Key, ch: str) -> bool:
        if not self._questions:
            if key == Key.ESC:
                self._service.respond(allowed=False, message="")
                self._close()
            return True

        q_idx = self._current
        st = self._states[q_idx]
        opts = self._opts(q_idx)
        n = len(opts)
        opt_rows = self._opt_rows

        match key:
            case Key.UP:
                st.cursor = (st.cursor - 1) % n
                if st.cursor == n - 1:  # wrapped to bottom
                    st.opt_scroll = max(0, n - opt_rows)
                elif st.cursor < st.opt_scroll:  # scrolled above viewport
                    st.opt_scroll = st.cursor
            case Key.DOWN:
                st.cursor = (st.cursor + 1) % n
                if st.cursor == 0:  # wrapped to top
                    st.opt_scroll = 0
                elif st.cursor >= st.opt_scroll + opt_rows:  # scrolled below
                    st.opt_scroll = st.cursor - opt_rows + 1
            case Key.LEFT:
                self._current = max(0, self._current - 1)
            case Key.RIGHT:
                self._current = min(len(self._questions) - 1, self._current + 1)
            case Key.ENTER:
                if self._is_other_idx(q_idx, st.cursor):
                    # Enter TYPING; pre-fill if previously answered via free text.
                    if self._is_free_text_answer(q_idx):
                        self._buf.set(list(st.answer))
                    else:
                        self._buf.clear()
                    self._mode = _Mode.TYPING
                else:
                    st.answer = opts[st.cursor]
                    st.answered = True
                    if self._all_answered():
                        self._submit()
                    else:
                        self._advance()
            case Key.ESC:
                self._service.respond(allowed=False, message="")
                self._close()
            case _:
                pass

        return True

    # ── TYPING ────────────────────────────────────────────────────────────────

    def _render_typing(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        cols = shutil.get_terminal_size((80, 24)).columns
        border_w = min(cols, 66)
        q = self._questions[self._current]
        lines: list[RenderableType] = []

        lines.append(
            Text.from_markup(
                f"[bold cyan]  ❓ Question {self._current + 1} of {len(self._questions)}"
                f"[/bold cyan][dim] — type your answer[/dim]"
            )
        )
        lines.append(Text(_BORDER * border_w, style="dim"))
        lines.append(Text(""))
        lines.append(Text(f"  {q.text}"))
        lines.append(Text(""))
        lines.append(Text.from_markup(f"  {self._render_prompt_line()}"))
        lines.append(Text(""))
        lines.append(Text(_BORDER * border_w, style="dim"))
        lines.append(Text("  Enter confirm   Esc back", style="dim"))

        return Group(*lines)

    def _handle_typing(self, key: Key, ch: str) -> bool:
        match key:
            case Key.ENTER:
                text = self._prompt_text.strip()
                if text:
                    st = self._states[self._current]
                    st.answer = text
                    st.answered = True
                    self._buf.clear()
                    self._mode = _Mode.SELECTING
                    if self._all_answered():
                        self._submit()
                    else:
                        self._advance()
                # If text is empty, stay in TYPING so the user provides something.
            case Key.ESC:
                self._buf.clear()
                self._mode = _Mode.SELECTING
            case _:
                self._handle_prompt_key(key, ch)
        return True

    # ── fallback when no questions parsed ─────────────────────────────────────

    def _render_empty(self) -> RenderableType:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        cols = shutil.get_terminal_size((80, 24)).columns
        return Group(
            Text.from_markup("[bold cyan]  ❓ Questions[/bold cyan]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  [dim](no questions provided)[/dim]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  Esc close", style="dim"),
        )
