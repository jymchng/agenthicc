"""QuestionsOverlay — multi-question overlay with selectable options (PRD-100).

Shown when an agent calls ask_user().  The user navigates questions with ←/→
and options with ↑/↓.  Each question has LLM-chosen options plus an "Other"
free-text fallback.  A single Enter on the last unanswered question submits
all answers.

State machine:
    SELECTING  — ↑↓ option, ←→ question, Enter confirm/submit, Esc cancel
    TYPING     — free-text entry for the "Other" option of the current question
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.overlays.prompt import PromptOverlay

_BORDER       = "─"
_OTHER_LABEL  = "Other — type your answer"


def _str_option(opt: Any) -> str:
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
    id:      str
    text:    str
    options: list[str]


@dataclass
class _QState:
    cursor:   int  = 0      # highlighted option index
    answer:   str  = ""     # confirmed answer (option label or typed text)
    answered: bool = False


class _Mode(Enum):
    SELECTING = auto()
    TYPING    = auto()


class QuestionsOverlay(PromptOverlay):
    """Multi-question overlay driven by ask_user()."""

    name = "questions"

    def __init__(
        self,
        req:      Any,
        service:  Any,
        close_fn: Callable[[], None],
    ) -> None:
        super().__init__()
        self._req     = req
        self._service = service
        self._close   = close_fn
        self._mode    = _Mode.SELECTING
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

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        super().on_mount()
        self._mode    = _Mode.SELECTING
        self._current = 0
        for s in self._states:
            s.cursor   = 0
            s.answer   = ""
            s.answered = False

    def on_unmount(self) -> None:
        pass

    def render(self) -> Any:
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
        answers = {
            q.id: s.answer
            for q, s in zip(self._questions, self._states)
        }
        self._service.respond(allowed=True, message=json.dumps(answers))
        self._close()

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _render_selecting(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415

        cols     = shutil.get_terminal_size((80, 24)).columns
        border_w = min(cols, 66)
        lines: list[Any] = []

        n_ans   = sum(1 for s in self._states if s.answered)
        n_total = len(self._questions)

        # Header
        lines.append(Text.from_markup(
            f"[bold cyan]  ❓ Questions[/bold cyan]"
            f"[dim]  ({n_ans} of {n_total} answered)[/dim]"
        ))
        lines.append(Text(_BORDER * border_w, style="dim"))

        # Navigation + dot indicators
        q_idx = self._current
        left  = "◀ " if q_idx > 0           else "  "
        right = " ▶" if q_idx < n_total - 1 else "  "
        nav   = f"{left}Question {q_idx + 1} of {n_total}{right}"
        dots  = " ".join("●" if s.answered else "○" for s in self._states)
        gap   = max(1, border_w - 4 - len(nav) - len(dots))
        lines.append(Text(f"  {nav}" + " " * gap + dots, style="dim"))
        lines.append(Text(""))

        # Question text
        q  = self._questions[q_idx]
        st = self._states[q_idx]
        lines.append(Text(f"  {q.text}"))
        lines.append(Text(""))

        # Options
        opts = self._opts(q_idx)
        for i, opt in enumerate(opts):
            is_cursor  = i == st.cursor
            is_other   = self._is_other_idx(q_idx, i)
            is_answer  = (
                st.answered and (
                    (is_other and self._is_free_text_answer(q_idx))
                    or (not is_other and st.answer == opt)
                )
            )
            label = self._opt_label(q_idx, i)

            if is_cursor:
                indicator = "▶"
                style     = "reverse"
            elif is_answer:
                indicator = "✓"
                style     = ""
            else:
                indicator = " "
                style     = "dim"

            lines.append(Text(f"  {indicator} {label}", style=style))

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
        st    = self._states[q_idx]
        opts  = self._opts(q_idx)
        n     = len(opts)

        match key:
            case Key.UP:
                st.cursor = (st.cursor - 1) % n
            case Key.DOWN:
                st.cursor = (st.cursor + 1) % n
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
                    st.answer   = opts[st.cursor]
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

    def _render_typing(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415

        cols     = shutil.get_terminal_size((80, 24)).columns
        border_w = min(cols, 66)
        q        = self._questions[self._current]
        lines: list[Any] = []

        lines.append(Text.from_markup(
            f"[bold cyan]  ❓ Question {self._current + 1} of {len(self._questions)}"
            f"[/bold cyan][dim] — type your answer[/dim]"
        ))
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
                    st          = self._states[self._current]
                    st.answer   = text
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

    def _render_empty(self) -> Any:
        from rich.console import Group  # noqa: PLC0415
        from rich.text import Text      # noqa: PLC0415
        cols = shutil.get_terminal_size((80, 24)).columns
        return Group(
            Text.from_markup("[bold cyan]  ❓ Questions[/bold cyan]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  [dim](no questions provided)[/dim]"),
            Text(_BORDER * min(cols, 66), style="dim"),
            Text("  Esc close", style="dim"),
        )
