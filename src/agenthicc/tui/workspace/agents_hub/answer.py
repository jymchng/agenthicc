"""The agents hub's answer form for a background ask_user request (AGI-01).

A background agent's ``ask_user`` reaches the hub as a ``question`` inbox
item (or an ``input`` item, for a bare input request). The form presents it
like the foreground ``QuestionsOverlay``, using the same
:class:`~agenthicc.tui.workspace.question_form.QuestionForm`:

- one question at a time, ←/→ between questions;
- ↑/↓ over the options, plus "Other — type your answer";
- Enter chooses and moves on; on the last unanswered question it sends;
- Ctrl+S sends once every question is answered;
- Esc closes the form and keeps its progress for the next ``i``.

"Other" opens an :class:`~agenthicc.tui.input.embedded.EmbeddedComposer`
scoped to the background session's directory, so paste, newline and the
``@`` and ``/`` pickers work as in the main TUI (AGI-05). The operator never
writes JSON: the form delivers ``{"answers": {id: answer}}`` for a question
item and ``{"answer": text}`` for an input item, the shapes the inbox
validates.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.question_form import Question, QuestionForm

if TYPE_CHECKING:
    from rich.console import RenderableType

    from agenthicc.background.inbox import BackgroundInboxItem
    from agenthicc.tui.input.embedded import ComposerContext, EmbeddedComposer

__all__ = ["AnswerView", "form_for_item", "late_answer_message"]

#: Question lines shown above the options; longer questions scroll with [ and ].
_QUESTION_ROWS = 6


def form_for_item(item: BackgroundInboxItem) -> QuestionForm:
    """The form for *item*: its questions, or one free-text question for input."""
    questions = Question.parse_all(item.target.get("questions"))
    if item.kind == "question" and questions:
        return QuestionForm(questions)
    prompt = str(item.target.get("tool_name") or "Input requested")
    return QuestionForm([Question(id="", text=prompt, options=())])


@dataclass
class AnswerView:
    """One open answer form: its item, progress, and composer for typed answers."""

    item: BackgroundInboxItem
    form: QuestionForm
    title: str = ""
    composer_context: ComposerContext | None = None
    composer: EmbeddedComposer | None = None
    typing: bool = False
    sending: bool = False
    question_scroll: int = 0
    drafts: dict[int, str] = field(default_factory=dict)
    #: Keys typed before the composer's context finished loading.
    pending_keys: list[tuple[Key, str]] = field(default_factory=list)

    @property
    def free_text_only(self) -> bool:
        """An input item, or a question without options: nothing to choose."""
        return not self.form.question.options

    def response(self) -> dict[str, object]:
        """The inbox response for the form's answers."""
        if self.item.kind == "question":
            return {"answers": self.form.answers()}
        return {"answer": self.form.states[0].answer}

    # ── typing ────────────────────────────────────────────────────────────

    def start_typing(
        self,
        on_confirm: Callable[[str], Awaitable[bool]],
        redraw: Callable[[], None],
    ) -> None:
        """Open the composer for the current question (needs a composer context)."""
        from agenthicc.tui.input.embedded import ANSWER_EXCLUDED, EmbeddedComposer

        if self.composer_context is None:
            return
        self.close_composer()
        composer = EmbeddedComposer(
            self.composer_context,
            on_submit=on_confirm,
            redraw=redraw,
            exclude=ANSWER_EXCLUDED(),
        )
        index = self.form.current
        draft = self.drafts.get(index)
        if draft is None and self.form.typed(index):
            draft = self.form.states[index].label
        if draft:
            composer.set_text(draft)
        self.composer = composer
        self.typing = True
        pending, self.pending_keys = self.pending_keys, []
        for key, ch in pending:
            composer.feed(key, ch)

    def close_composer(self) -> None:
        if self.composer is not None:
            self.composer.close()
            self.composer = None
        self.typing = False

    def leave_typing(self) -> None:
        """Back to choosing, keeping what was typed for the next visit."""
        if self.composer is not None and self.composer.text:
            self.drafts[self.form.current] = self.composer.text
        self.close_composer()

    def confirm_typed(self, typed: str, delivered: str) -> None:
        self.form.answer_typed(typed, delivered)
        self.drafts.pop(self.form.current, None)
        self.close_composer()
        self.question_scroll = 0
        if not self.form.complete:
            self.form.advance()

    # ── choosing ──────────────────────────────────────────────────────────

    def handle_choice_key(self, key: Key, ch: str) -> str:
        """Apply one key while choosing; returns ``type``, ``send``, ``close`` or ``""``."""
        form = self.form
        match key:
            case Key.UP:
                form.move(-1)
            case Key.DOWN:
                form.move(1)
            case Key.LEFT:
                form.go(-1)
                self.question_scroll = 0
            case Key.RIGHT:
                form.go(1)
                self.question_scroll = 0
            case Key.CHAR if ch == "[":
                self.question_scroll = max(0, self.question_scroll - 1)
            case Key.CHAR if ch == "]":
                self.question_scroll += 1
            case Key.CTRL_S:
                return "send" if form.complete else ""
            case Key.ESC:
                return "close"
            case Key.ENTER:
                if form.on_other():
                    return "type"
                form.choose()
                self.question_scroll = 0
                if form.complete:
                    return "send"
                form.advance()
        return ""

    # ── rendering ─────────────────────────────────────────────────────────

    def render(self, width: int, *, plain: bool = False) -> list[RenderableType]:
        from rich.console import Console
        from rich.text import Text

        form = self.form
        count = len(form.questions)
        answered = sum(state.answered for state in form.states)
        lines: list[RenderableType] = []
        header = Text(no_wrap=True, overflow="ellipsis")
        header.append(f" Answer · {self.title or self.item.session_id}", style="bold cyan")
        if count > 1:
            header.append(f"   question {form.current + 1} of {count}", style="bold")
            header.append(
                "   " + " ".join("●" if state.answered else "○" for state in form.states),
                style="dim",
            )
            header.append(f"  ({answered} answered)", style="dim")
        lines.append(header)

        console = Console(width=max(20, width - 4), highlight=False, color_system=None)
        wrapped = list(Text(form.question.text).wrap(console, width=max(20, width - 4)))
        self.question_scroll = max(0, min(self.question_scroll, len(wrapped) - _QUESTION_ROWS))
        for line in wrapped[self.question_scroll : self.question_scroll + _QUESTION_ROWS]:
            shown = Text("   ")
            shown.append_text(line)
            lines.append(shown)
        if len(wrapped) > _QUESTION_ROWS:
            lines.append(
                Text(
                    f"   lines {self.question_scroll + 1}–"
                    f"{min(len(wrapped), self.question_scroll + _QUESTION_ROWS)} of "
                    f"{len(wrapped)} · [/] scroll",
                    style="dim",
                )
            )

        if self.typing or self.free_text_only:
            if self.composer is not None:
                lines.extend(self.composer.render())
            else:
                lines.append(Text("   Loading the agent's commands and mentions…", style="yellow"))
            hint = " Enter confirm · Ctrl+J newline · @ mention · / command · Esc back"
        else:
            state = form.state
            for index, option in enumerate(form.options()):
                cursor = index == state.cursor
                chosen = state.answered and (
                    (form.on_other() and index == len(form.options()) - 1 and form.typed())
                    or (not form.typed() and option == state.label)
                )
                if index == len(form.options()) - 1 and form.typed():
                    preview = " ".join(state.label.split())
                    option = f'Other: "{preview[:40]}{"…" if len(preview) > 40 else ""}"'
                marker = "▶" if cursor else ("✓" if chosen else " ")
                style = "reverse" if cursor and not plain else ("" if chosen else "dim")
                lines.append(Text(f"   {marker} {option}", style=style, no_wrap=True))
            hint = (
                " ↑↓ option · ←→ question · Enter choose · Ctrl+S send · Esc close"
                if form.complete
                else " ↑↓ option · ←→ question · Enter choose · Esc close"
            )
        if self.sending:
            hint = " Sending your answers…"
        lines.append(Text(hint, style="dim", no_wrap=True, overflow="ellipsis"))
        return lines


def late_answer_message(view: AnswerView) -> str:
    """The message that carries answers to a question that already timed out (AGI-02).

    The agent continued with its own assumption; this tells it what the
    operator chose, so it can revisit that assumption.
    """
    timeout = view.item.expires_at - view.item.created_at if view.item.expires_at else None
    waited = f" after {timeout:.0f} s" if timeout else ""
    parts = []
    for question, state in zip(view.form.questions, view.form.states, strict=True):
        if state.answered:
            parts.append(f"{question.id}: {state.answer}" if question.id else state.answer)
    return (
        f"Late answers to your earlier questions (they timed out{waited} and you continued "
        f"with an assumption): {'; '.join(parts)}. Revisit your assumption if these change it."
    )
