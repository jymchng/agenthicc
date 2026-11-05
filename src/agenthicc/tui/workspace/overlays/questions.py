"""QuestionsOverlay — multi-question overlay with selectable options (PRD-100).

Shown when an agent calls ask_user().  The user navigates questions with ←/→
and options with ↑/↓.  Each question has LLM-chosen options plus an "Other"
free-text fallback.  A single Enter on the last unanswered question submits
all answers.

State machine:
    SELECTING  — ↑↓ option, ←→ question, Enter confirm/submit, Esc cancel
    TYPING     — free-text entry for the "Other" option of the current question

The answer model (focus, highlighted options, answers) is a
:class:`~agenthicc.tui.workspace.question_form.QuestionForm`, shared with the
agents hub (AGI-01). TYPING uses an
:class:`~agenthicc.tui.input.embedded.EmbeddedComposer`, the main composer with
its own pickers, so paste, newline, ``@`` mentions and ``/`` commands work in an
answer as they do in the main input panel (AGI-05).

Height stability
----------------
The question and options areas are always rendered as fixed-size viewports
computed from the terminal dimensions. Both viewports are padded with blank
rows, and the question viewport has a separate indicator row. This keeps the
overlay height constant while navigating, scrolling, and changing questions.

Question text is wrapped into literal Rich ``Text`` rows and cached by
terminal width. The selecting view uses ``[`` and ``]`` for one-line question
scrolling; the typing view reserves Page Up/Page Down for question scrolling so
literal brackets remain available in the answer buffer.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text
    from agenthicc.tools.approval import ApprovalRequest, InteractiveApprovals

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.question_form import OTHER_LABEL, Question, QuestionForm

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from agenthicc.tui.input.embedded import ComposerContext, EmbeddedComposer

_BORDER = "─"
_OTHER_LABEL = OTHER_LABEL
_MAX_QUESTION_ROWS = 12
_LAYOUT_OVERHEAD = 20


@dataclass(frozen=True)
class AnswerContext:
    """What a typed answer can reach (AGI-05).

    ``composer`` scopes the ``@`` and ``/`` pickers. ``run_command`` runs a
    submitted ``/command`` the way the main composer would while a turn is
    busy, and returns whether it handled the text. ``expand`` resolves the
    answer's ``@`` mentions into the context prefix a message would carry.
    ``redraw`` repaints the overlay after an asynchronous key.
    """

    composer: ComposerContext
    run_command: Callable[[str], bool] = field(default=lambda _text: False)
    expand: Callable[[str], Awaitable[str]] | None = None
    redraw: Callable[[], None] = field(default=lambda: None)


@dataclass
class _QView:
    """Presentation state for one question: its two scroll offsets."""

    opt_scroll: int = 0
    question_scroll: int = 0


class _Mode(Enum):
    SELECTING = auto()
    TYPING = auto()


class QuestionsOverlay(Overlay):
    """Multi-question overlay driven by ask_user()."""

    name = "questions"

    def __init__(
        self,
        req: ApprovalRequest,
        service: InteractiveApprovals,
        close_fn: Callable[[], None],
        *,
        answers: AnswerContext | None = None,
    ) -> None:
        super().__init__()
        self._req = req
        self._service = service
        self._close = close_fn
        self._answers = answers
        self._mode = _Mode.SELECTING
        raw_value = (req.tool_input or {}).get("questions", [])
        self._form = QuestionForm(Question.parse_all(raw_value))
        self._views: list[_QView] = [_QView() for _ in self._form.questions]
        self._composer: EmbeddedComposer | None = None
        #: Text typed for a question and left with Esc, kept for its next visit.
        self._drafts: dict[int, str] = {}

        # Cached from the last render — read by the keyboard handlers. Question
        # lines are presentation-only and are never included in the answer or
        # approval payload.
        self._opt_rows: int = 2
        self._question_rows: int = 1
        self._question_width: int = 0
        self._question_lines: dict[int, list[Text]] = {}

    # ── compatibility views used by rendering ────────────────────────────────

    @property
    def _questions(self) -> list[Question]:
        return list(self._form.questions)

    @property
    def _current(self) -> int:
        return self._form.current

    @property
    def _states(self) -> list[_QView]:
        return self._views

    @property
    def _prompt_text(self) -> str:
        return self._composer.text if self._composer is not None else ""

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._mode = _Mode.SELECTING
        self._form = QuestionForm(list(self._form.questions))
        self._views = [_QView() for _ in self._form.questions]
        self._drafts.clear()
        self._close_composer()
        self._question_width = 0
        self._question_lines.clear()

    def on_unmount(self) -> None:
        self._close_composer()

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
        return self._form.options(q_idx)

    def _is_other_idx(self, q_idx: int, opt_idx: int) -> bool:
        return opt_idx == len(self._opts(q_idx)) - 1

    def _is_free_text_answer(self, q_idx: int) -> bool:
        """True when question was answered via free text (not one of the preset options)."""
        return self._form.typed(q_idx)

    def _opt_label(self, q_idx: int, opt_idx: int) -> str:
        if self._is_other_idx(q_idx, opt_idx) and self._is_free_text_answer(q_idx):
            text = " ".join(self._form.states[q_idx].label.split())
            if len(text) > 40:
                text = text[:39] + "…"
            return f'Other: "{text}"'
        return self._opts(q_idx)[opt_idx]

    def _all_answered(self) -> bool:
        return self._form.complete

    def _advance(self) -> None:
        """Move focus to the next unanswered question."""
        self._form.advance()

    def _submit(self) -> None:
        import json

        self._respond(allowed=True, message=json.dumps(self._form.answers()), outcome="answered")
        self._close()

    def _respond(self, *, allowed: bool, message: str, outcome: str) -> None:
        """Answer this exact request, rejecting stale overlay callbacks."""
        method = type(self._service).__dict__.get("respond_for_request")
        if callable(method):
            self._service.respond_for_request(
                self._req.request_id or self._req.tool_use_id,
                allowed,
                message=message,
                outcome=outcome,
            )
            return
        # Compatibility with test doubles and older approval adapters.
        self._service.respond(allowed=allowed, message=message)

    def _remaining_seconds(self) -> int | None:
        deadline = self._req.deadline_at
        if not isinstance(deadline, (int, float)):
            return None
        return max(0, int(deadline - time.time() + 0.999))

    def _prepare_question_lines(self, width: int) -> None:
        """Wrap every question at *width* and invalidate stale width caches."""
        from rich.console import Console
        from rich.text import Text

        width = max(1, width)
        if self._question_width != width:
            self._question_width = width
            self._question_lines.clear()
        if len(self._question_lines) == len(self._questions):
            return

        console = Console(width=width, highlight=False, color_system=None)
        for index, question in enumerate(self._questions):
            # Text() deliberately treats model content as literal text. In
            # particular, a question containing ``[red]`` must not inject
            # Rich markup into the waiting modal.
            wrapped = list(
                Text(question.text).wrap(
                    console,
                    width=width,
                    overflow="fold",
                    no_wrap=False,
                )
            )
            self._question_lines[index] = wrapped or [Text("")]

    def _selecting_layout(self, rows: int) -> tuple[int, int]:
        """Return fixed ``(question_rows, option_rows)`` for SELECTING."""
        max_question_lines = max(
            (len(lines) for lines in self._question_lines.values()),
            default=1,
        )
        max_options = max((len(self._opts(i)) for i in range(len(self._questions))), default=1)
        # Reserve one row for the question range indicator. The remaining
        # content budget is split deterministically so both viewports retain
        # at least one row even on a very short terminal.
        budget = max(3, rows - _LAYOUT_OVERHEAD)
        option_rows = min(max_options, max(1, budget // 2))
        question_rows = min(_MAX_QUESTION_ROWS, max(1, max_question_lines))
        question_rows = min(question_rows, max(1, budget - option_rows - 1))
        return question_rows, option_rows

    def _typing_layout(self, rows: int, question_lines: int) -> int:
        """Return the fixed question viewport height for TYPING."""
        budget = max(1, rows - _LAYOUT_OVERHEAD)
        return min(_MAX_QUESTION_ROWS, max(1, question_lines), budget)

    def _prepare_question_view(self, *, selecting: bool) -> tuple[list[Text], int]:
        """Prepare the current question and return its lines and row budget."""
        term = shutil.get_terminal_size((80, 24))
        width = max(1, term.columns - 4)
        self._prepare_question_lines(width)
        q_idx = self._current
        if selecting:
            question_rows, option_rows = self._selecting_layout(term.lines)
            self._question_rows = question_rows
            self._opt_rows = option_rows
        else:
            question_rows = self._typing_layout(term.lines, len(self._question_lines[q_idx]))
            self._question_rows = question_rows
        # A resize can happen while any question is selected. Clamp every
        # presentation offset now so returning to another question cannot
        # reveal a stale range from the previous terminal geometry.
        for index in range(len(self._questions)):
            self._clamp_question_scroll(index)
        return self._question_lines[q_idx], question_rows

    def _clamp_question_scroll(self, q_idx: int) -> None:
        """Keep the current question offset inside its prepared viewport."""
        lines = self._question_lines.get(q_idx, [])
        state = self._states[q_idx]
        max_scroll = max(0, len(lines) - self._question_rows)
        state.question_scroll = max(0, min(state.question_scroll, max_scroll))

    def _render_question_view(self, q_idx: int, rows: int) -> list[RenderableType]:
        """Render a fixed-height, indented question viewport and indicator."""
        from rich.text import Text

        lines = self._question_lines[q_idx]
        state = self._states[q_idx]
        self._clamp_question_scroll(q_idx)
        scroll = state.question_scroll
        visible = lines[scroll : scroll + rows]
        rendered: list[RenderableType] = []
        for line in visible:
            prefixed = Text("  ")
            prefixed.append_text(line)
            rendered.append(prefixed)
        rendered.extend(Text("") for _ in range(rows - len(visible)))

        if len(lines) > rows:
            first = scroll + 1
            last = min(scroll + rows, len(lines))
            above = scroll > 0
            below = last < len(lines)
            prefix = "↑ · " if above else ""
            suffix = " · ↓" if below else ""
            rendered.append(
                Text(f"  {prefix}lines {first}–{last} of {len(lines)}{suffix}", style="dim")
            )
        else:
            rendered.append(Text(""))
        return rendered

    def _scroll_question(self, delta: int) -> None:
        """Move the current question viewport without changing answer state."""
        selecting = self._mode == _Mode.SELECTING
        self._prepare_question_view(selecting=selecting)
        state = self._states[self._current]
        max_scroll = max(0, len(self._question_lines[self._current]) - self._question_rows)
        state.question_scroll = max(0, min(state.question_scroll + delta, max_scroll))

    def _clamp_opt_scroll(self, q_idx: int) -> None:
        """Ensure opt_scroll keeps the cursor inside the visible options window."""
        cursor = self._form.states[q_idx].cursor
        view = self._views[q_idx]
        n = len(self._opts(q_idx))
        opt_rows = self._opt_rows
        # Cursor below viewport → scroll down.
        if cursor >= view.opt_scroll + opt_rows:
            view.opt_scroll = cursor - opt_rows + 1
        # Cursor above viewport → scroll up.
        if cursor < view.opt_scroll:
            view.opt_scroll = cursor
        # Clamp scroll to valid range.
        view.opt_scroll = max(0, min(view.opt_scroll, max(0, n - opt_rows)))

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _render_selecting(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        term = shutil.get_terminal_size((80, 24))
        cols = term.columns
        border_w = min(cols, 66)
        question_lines, question_rows = self._prepare_question_view(selecting=True)

        lines: list[RenderableType] = []

        n_ans = sum(1 for state in self._form.states if state.answered)
        n_total = len(self._questions)

        # Header
        lines.append(
            Text.from_markup(
                f"[bold cyan]  ❓ Questions[/bold cyan][dim]  ({n_ans} of {n_total} answered)[/dim]"
            )
        )
        remaining = self._remaining_seconds()
        if remaining is not None:
            lines.append(Text(f"  Waiting for your answer · {remaining}s remaining", style="dim"))
        lines.append(Text(_BORDER * border_w, style="dim"))

        # Navigation + dot indicators
        q_idx = self._current
        left = "◀ " if q_idx > 0 else "  "
        right = " ▶" if q_idx < n_total - 1 else "  "
        nav = f"{left}Question {q_idx + 1} of {n_total}{right}"
        dots = " ".join("●" if state.answered else "○" for state in self._form.states)
        gap = max(1, border_w - 4 - len(nav) - len(dots))
        lines.append(Text(f"  {nav}" + " " * gap + dots, style="dim"))
        lines.append(Text(""))

        # Question text
        st = self._form.states[q_idx]
        view = self._views[q_idx]
        lines.extend(self._render_question_view(q_idx, question_rows))
        lines.append(Text(""))

        # Options viewport — always exactly opt_rows lines.
        opts = self._opts(q_idx)
        n = len(opts)
        opt_rows = self._opt_rows
        self._clamp_opt_scroll(q_idx)
        scroll = view.opt_scroll
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

        scroll_hint = "   [/] scroll" if len(question_lines) > question_rows else ""
        if self._all_answered():
            hint = f"  ↑↓ option   ←→ question{scroll_hint}   Enter SUBMIT ALL   Esc cancel"
        else:
            hint = f"  ↑↓ option   ←→ question{scroll_hint}   Enter confirm   Esc cancel"
        lines.append(Text(hint, style="dim"))

        return Group(*lines)

    def _handle_selecting(self, key: Key, ch: str) -> bool:
        if not self._questions:
            if key == Key.ESC:
                self._respond(allowed=False, message="", outcome="cancelled")
                self._close()
            return True

        form = self._form
        q_idx = form.current
        st = form.state
        view = self._views[q_idx]
        n = len(self._opts(q_idx))
        opt_rows = self._opt_rows

        match key:
            case Key.CHAR if ch == "[":
                self._scroll_question(-1)
            case Key.CHAR if ch == "]":
                self._scroll_question(1)
            case Key.UP:
                form.move(-1)
                if st.cursor == n - 1:  # wrapped to bottom
                    view.opt_scroll = max(0, n - opt_rows)
                elif st.cursor < view.opt_scroll:  # scrolled above viewport
                    view.opt_scroll = st.cursor
            case Key.DOWN:
                form.move(1)
                if st.cursor == 0:  # wrapped to top
                    view.opt_scroll = 0
                elif st.cursor >= view.opt_scroll + opt_rows:  # scrolled below
                    view.opt_scroll = st.cursor - opt_rows + 1
            case Key.LEFT:
                form.go(-1)
            case Key.RIGHT:
                form.go(1)
            case Key.ENTER:
                if form.on_other():
                    self._start_typing()
                else:
                    form.choose()
                    if form.complete:
                        self._submit()
                    else:
                        form.advance()
            case Key.ESC:
                self._respond(allowed=False, message="", outcome="cancelled")
                self._close()
            case _:
                pass

        return True

    # ── TYPING ────────────────────────────────────────────────────────────────

    def _start_typing(self) -> None:
        """Open the composer for the current question's free-text answer."""
        from pathlib import Path

        from agenthicc.tui.input.embedded import (
            ANSWER_EXCLUDED,
            ComposerContext,
            EmbeddedComposer,
        )

        context = self._answers.composer if self._answers else ComposerContext(cwd=Path.cwd())
        redraw = self._answers.redraw if self._answers else (lambda: None)
        self._close_composer()
        composer = EmbeddedComposer(
            context,
            on_submit=self._confirm_typed,
            redraw=redraw,
            exclude=ANSWER_EXCLUDED(),
        )
        index = self._form.current
        draft = self._drafts.get(index)
        if draft is None and self._form.typed(index):
            draft = self._form.states[index].label
        if draft:
            composer.set_text(draft)
        self._composer = composer
        self._mode = _Mode.TYPING

    def _close_composer(self) -> None:
        if self._composer is not None:
            self._composer.close()
            self._composer = None

    async def _confirm_typed(self, text: str) -> bool:
        """Handle the composer's submission: a command, or this question's answer."""
        typed = text.strip()
        if not typed:
            return False
        answers = self._answers
        if typed.startswith("/") and answers is not None and answers.run_command(typed):
            return True  # the command ran (or was refused) in place of an answer
        delivered = text
        if answers is not None and answers.expand is not None:
            delivered = await answers.expand(text)
        form = self._form
        form.answer_typed(text, delivered)
        self._drafts.pop(form.current, None)
        self._mode = _Mode.SELECTING
        self._close_composer()
        if form.complete:
            self._submit()
        else:
            form.advance()
        if answers is not None:
            answers.redraw()
        return True

    def _render_typing(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        cols = shutil.get_terminal_size((80, 24)).columns
        border_w = min(cols, 66)
        question_lines, question_rows = self._prepare_question_view(selecting=False)
        lines: list[RenderableType] = []

        lines.append(
            Text.from_markup(
                f"[bold cyan]  ❓ Question {self._current + 1} of {len(self._questions)}"
                f"[/bold cyan][dim] — type your answer[/dim]"
            )
        )
        lines.append(Text(_BORDER * border_w, style="dim"))
        lines.append(Text(""))
        lines.extend(self._render_question_view(self._current, question_rows))
        lines.append(Text(""))
        if self._composer is not None:
            lines.extend(self._composer.render())
        lines.append(Text(""))
        lines.append(Text(_BORDER * border_w, style="dim"))
        scroll_hint = "   PageUp/PageDown scroll" if len(question_lines) > question_rows else ""
        lines.append(
            Text(
                f"  Enter confirm   Ctrl+J newline   @ mention   / command   Esc back{scroll_hint}",
                style="dim",
            )
        )

        return Group(*lines)

    def _handle_typing(self, key: Key, ch: str) -> bool:
        composer = self._composer
        if composer is None:
            self._mode = _Mode.SELECTING
            return True
        match key:
            case Key.PAGE_UP if not composer.picker_open:
                self._scroll_question(-self._question_rows)
            case Key.PAGE_DOWN if not composer.picker_open:
                self._scroll_question(self._question_rows)
            case Key.ESC if not composer.picker_open:
                # Leave the answer; what was typed is kept for the next visit.
                if composer.text:
                    self._drafts[self._form.current] = composer.text
                self._close_composer()
                self._mode = _Mode.SELECTING
            case _:
                composer.feed(key, ch)
        return True

    # ── fallback when no questions parsed ─────────────────────────────────────

    def _render_empty(self) -> RenderableType:
        from rich.console import Group
        from rich.text import Text

        cols = shutil.get_terminal_size((80, 24)).columns
        return Group(
            Text.from_markup("[bold cyan]  ❓ Questions[/bold cyan]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  [dim](no questions provided)[/dim]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  Esc close", style="dim"),
        )
