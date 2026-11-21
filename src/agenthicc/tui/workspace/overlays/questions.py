"""QuestionsOverlay — ``ask_user`` questions in a :class:`Dialog` (PRD-100, TUX-07).

Shown when an agent calls ask_user(). The title is "The agent asks"; the body
is the current question (``1 of 3`` when there are several) and the options
are the dialog's numbered options, with "Other" last. ``↑↓`` choose, Enter or
``1``–``9`` confirm, ``←``/``→`` move between questions, ``[``/``]`` scroll a
long question. A single confirm on the last unanswered question submits all
answers.

There is no countdown. When the request carries a deadline, the reason line
reads "Answer within 5 min."

State machine:
    SELECTING  — choose an option; Esc cancels the request
    TYPING     — free-text entry for the "Other" option; Esc goes back with
                 the draft kept

The answer model (focus, highlighted options, answers) is a
:class:`~agenthicc.tui.workspace.question_form.QuestionForm`, shared with the
agents hub (AGI-01). TYPING uses an
:class:`~agenthicc.tui.input.embedded.EmbeddedComposer`, the main composer with
its own pickers, so paste, newline, ``@`` mentions and ``/`` commands work in an
answer as they do in the main input panel (AGI-05). In TYPING, Page Up/Page
Down scroll the question so ``[`` and ``]`` stay available in the answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.dialog import (
    DEFAULT_DIALOG_SETTINGS,
    Dialog,
    DialogKey,
    DialogOption,
    DialogSettings,
)
from agenthicc.tui.workspace.overlay import Overlay
from agenthicc.tui.workspace.question_form import Question, QuestionForm

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from rich.console import RenderableType
    from rich.text import Text

    from agenthicc.tools.approval import ApprovalRequest, InteractiveApprovals
    from agenthicc.tui.input.embedded import ComposerContext, EmbeddedComposer

__all__ = ["AnswerContext", "QUESTIONS_TITLE", "QuestionsOverlay", "question_summary"]

QUESTIONS_TITLE = "The agent asks"
#: How the free-text choice reads in the dialog (glyph-safe).
OTHER_DISPLAY = "Other (type your answer)"


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


class _Mode(Enum):
    SELECTING = auto()
    TYPING = auto()


def _deadline_text(timeout_s: float | None) -> str:
    if timeout_s is None or timeout_s <= 0:
        return ""
    if timeout_s < 90:
        return f"Answer within {max(1, int(timeout_s + 0.5))} s."
    return f"Answer within {int(timeout_s / 60 + 0.5)} min."


def question_summary(req: ApprovalRequest) -> tuple[str, int]:
    """The first question and its option count (with Other), for the announcer."""
    questions = Question.parse_all((req.tool_input or {}).get("questions", []))
    if not questions:
        return "no questions", 0
    first = questions[0]
    text = " ".join(first.text.split())
    if len(questions) > 1:
        text = f"question 1 of {len(questions)}, {text}"
    return text, len(first.options) + 1


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
        settings: DialogSettings = DEFAULT_DIALOG_SETTINGS,
    ) -> None:
        super().__init__()
        self._req = req
        self._service = service
        self._close = close_fn
        self._answers = answers
        self._settings = settings
        self._mode = _Mode.SELECTING
        raw_value = (req.tool_input or {}).get("questions", [])
        self._form = QuestionForm(Question.parse_all(raw_value))
        self._composer: EmbeddedComposer | None = None
        #: Text typed for a question and left with Esc, kept for its next visit.
        self._drafts: dict[int, str] = {}
        title = QUESTIONS_TITLE
        if req.requester is not None:
            title = f"{QUESTIONS_TITLE} {_sep()} {req.requester.label}"
        self.dialog = Dialog(title, settings=settings, cancel_label="cancel")
        self.dialog.reason = _deadline_text(req.timeout_s)
        self.dialog.summary, _count = question_summary(req)

    # ── views used by rendering and tests ────────────────────────────────────

    @property
    def _questions(self) -> list[Question]:
        return list(self._form.questions)

    @property
    def _current(self) -> int:
        return self._form.current

    @property
    def _prompt_text(self) -> str:
        return self._composer.text if self._composer is not None else ""

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._mode = _Mode.SELECTING
        self._form = QuestionForm(list(self._form.questions))
        self._drafts.clear()
        self._close_composer()

    def on_unmount(self) -> None:
        self._close_composer()

    def set_viewport(self, width: int, height: int) -> None:
        self.dialog.set_viewport(width, height)

    def render(self) -> RenderableType:
        self._sync_dialog()
        return self.dialog.render()

    def handle_key(self, key: Key, ch: str) -> bool:
        if self._mode == _Mode.TYPING:
            return self._handle_typing(key, ch)
        return self._handle_selecting(key, ch)

    # ── presentation ──────────────────────────────────────────────────────────

    def _option_label(self, q_idx: int, opt_idx: int) -> str:
        options = self._form.options(q_idx)
        if opt_idx == len(options) - 1:
            if self._form.typed(q_idx):
                text = " ".join(self._form.states[q_idx].label.split())
                if len(text) > 40:
                    text = text[:39] + "..."
                return f'Other: "{text}"'
            return OTHER_DISPLAY
        return options[opt_idx]

    def _sync_dialog(self) -> None:
        """Point the dialog at the current question, its options and its cursor."""
        from agenthicc.tui.theme import glyphs

        dialog = self.dialog
        form = self._form
        if not form.questions:
            dialog.options = []
            dialog.set_body("empty", self._empty_body)
            dialog.conditions = frozenset()
            return
        index = form.current
        state = form.state
        options = form.options(index)
        ok = glyphs().ok
        dialog.options = [
            DialogOption(
                self._option_label(index, position),
                mark=ok
                if state.answered
                and (
                    (position == len(options) - 1 and form.typed(index))
                    or (position < len(options) - 1 and state.answer == options[position])
                )
                else "",
            )
            for position in range(len(options))
        ]
        dialog.selected = state.cursor
        dialog.conditions = frozenset({"questions"}) if len(form.questions) > 1 else frozenset()
        answered = tuple(item.answered for item in form.states)
        dialog.set_body((index, answered), lambda: self._question_body(index))
        dialog.legend_override = (
            ("Enter confirm", "Ctrl+J newline", "@ mention", "Esc back")
            if self._mode == _Mode.TYPING
            else ()
        )

    def _question_body(self, index: int) -> list[RenderableType]:
        from rich.text import Text

        from agenthicc.tui.theme import glyphs, palette

        p, g = palette(), glyphs()
        form = self._form
        body: list[RenderableType] = []
        total = len(form.questions)
        if total > 1:
            dots = " ".join(g.bullet if state.answered else g.sep for state in form.states)
            body.append(Text(f"Question {index + 1} of {total}   {dots}", style=p.muted))
            body.append(Text(""))
        # Text() keeps model content literal: "[red]" must not become markup.
        body.append(Text(form.questions[index].text))
        return body

    @staticmethod
    def _empty_body() -> list[RenderableType]:
        from rich.text import Text

        from agenthicc.tui.theme import palette

        return [Text("no questions provided", style=palette().muted)]

    # ── SELECTING ─────────────────────────────────────────────────────────────

    def _handle_selecting(self, key: Key, ch: str) -> bool:
        form = self._form
        if not form.questions:
            if key == Key.ESC:
                self._respond(allowed=False, message="", outcome="cancelled")
                self._close()
            return True
        self._sync_dialog()
        result = self.dialog.handle_key(key, ch)
        form.state.cursor = self.dialog.selected
        match result.kind:
            case DialogKey.PREVIOUS:
                form.go(-1)
            case DialogKey.NEXT:
                form.go(1)
            case DialogKey.CHOOSE:
                if form.on_other():
                    self._start_typing()
                else:
                    form.choose()
                    if form.complete:
                        self._submit()
                    else:
                        form.advance()
            case DialogKey.CANCEL:
                self._respond(allowed=False, message="", outcome="cancelled")
                self._close()
            case _:
                pass
        return True

    def _submit(self) -> None:
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
        # Test doubles that only record ``respond``.
        self._service.respond(allowed=allowed, message=message)

    # ── TYPING ────────────────────────────────────────────────────────────────

    def _start_typing(self) -> None:
        """Open the composer for the current question's free-text answer."""
        from agenthicc.tui.input.embedded import (
            ANSWER_EXCLUDED,
            ComposerContext,
            EmbeddedComposer,
        )

        context = (
            self._answers.composer
            if self._answers
            else self._settings.composer or ComposerContext(cwd=Path.cwd())
        )
        redraw = self._answers.redraw if self._answers else self._settings.redraw
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
        self.dialog.footer = self._composer_rows
        self._mode = _Mode.TYPING

    def _close_composer(self) -> None:
        if self._composer is not None:
            self._composer.close()
            self._composer = None
        self.dialog.footer = None
        self.dialog.legend_override = ()

    def _composer_rows(self, width: int) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.theme import palette
        from agenthicc.tui.workspace.dialog import render_lines

        composer = self._composer
        if composer is None:
            return []
        rows = [Text(""), Text(" Your answer:", style=palette().muted)]
        return rows + render_lines(composer.render(), width)

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

    def _handle_typing(self, key: Key, ch: str) -> bool:
        composer = self._composer
        if composer is None:
            self._mode = _Mode.SELECTING
            return True
        match key:
            case Key.PAGE_UP if not composer.picker_open:
                self.dialog.scroll_by(-self.dialog.page_rows)
            case Key.PAGE_DOWN if not composer.picker_open:
                self.dialog.scroll_by(self.dialog.page_rows)
            case Key.ESC if not composer.picker_open:
                # Leave the answer; what was typed is kept for the next visit.
                if composer.text:
                    self._drafts[self._form.current] = composer.text
                self._close_composer()
                self._mode = _Mode.SELECTING
            case _:
                composer.feed(key, ch)
        return True


def _sep() -> str:
    from agenthicc.tui.theme import glyphs

    return glyphs().sep
