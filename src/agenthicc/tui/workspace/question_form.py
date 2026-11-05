"""QuestionForm — the answer model behind every ask_user surface (AGI-01).

An ``ask_user`` request is a list of questions, each with options and an
always-present "Other — type your answer". The foreground overlay
(:class:`~agenthicc.tui.workspace.overlays.questions.QuestionsOverlay`) and the
agents hub's answer view present the same request, so they share this model:
which question has focus, which option is highlighted, what has been
answered, and whether the form is complete. Presentation (wrapping,
viewports, scrolling) stays with each surface.

Answers are delivered as ``{question_id: answer}``. A typed answer may differ
from what the surface shows: the delivered text can carry an ``@`` mention's
context prefix, while the label shows what was typed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = ["OTHER_LABEL", "Question", "QuestionForm", "QuestionState"]

#: The free-text choice every question offers last.
OTHER_LABEL = "Other — type your answer"


def _option_text(option: object) -> str:
    """An option as plain text.

    Models sometimes pass objects (``{"id": "a", "label": "Frontend"}``)
    instead of strings: prefer ``label``, then ``text``, ``name`` and
    ``value``, then the first non-empty value, then ``str()``.
    """
    if isinstance(option, str):
        return option
    if isinstance(option, Mapping):
        for key in ("label", "text", "name", "value"):
            if option.get(key):
                return str(option[key])
        for value in option.values():
            if value:
                return str(value)
    return str(option)


@dataclass(frozen=True)
class Question:
    """One question as the agent asked it."""

    id: str
    text: str
    options: tuple[str, ...]

    @classmethod
    def parse_all(cls, raw: object) -> list[Question]:
        """The well-formed questions in an ``ask_user`` ``questions`` value."""
        questions: list[Question] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, Mapping):
                continue
            options = item.get("options", [])
            questions.append(
                cls(
                    id=str(item.get("id", "")),
                    text=str(item.get("text", "")),
                    options=tuple(_option_text(option) for option in options)
                    if isinstance(options, list)
                    else (),
                )
            )
        return questions


@dataclass
class QuestionState:
    """One question's progress."""

    cursor: int = 0
    answered: bool = False
    #: What is delivered to the agent.
    answer: str = ""
    #: What the operator typed or chose, for display.
    label: str = ""


@dataclass
class QuestionForm:
    """Focus, highlighted options and answers for one ``ask_user`` request."""

    questions: Sequence[Question]
    current: int = 0
    states: list[QuestionState] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.states) != len(self.questions):
            self.states = [QuestionState() for _ in self.questions]

    # ── queries ───────────────────────────────────────────────────────────

    @property
    def question(self) -> Question:
        return self.questions[self.current]

    @property
    def state(self) -> QuestionState:
        return self.states[self.current]

    def options(self, index: int | None = None) -> list[str]:
        """The options of question *index* (default: the current one), then Other."""
        question = self.questions[self.current if index is None else index]
        return [*question.options, OTHER_LABEL]

    def on_other(self, index: int | None = None) -> bool:
        """Whether the highlighted option of question *index* is Other."""
        position = self.current if index is None else index
        return self.states[position].cursor == len(self.options(position)) - 1

    def typed(self, index: int | None = None) -> bool:
        """Whether question *index* was answered by typing rather than choosing."""
        position = self.current if index is None else index
        state = self.states[position]
        return state.answered and state.label not in self.questions[position].options

    @property
    def complete(self) -> bool:
        return bool(self.questions) and all(state.answered for state in self.states)

    def answers(self) -> dict[str, str]:
        """The delivered answers, keyed by question ID."""
        return {
            question.id: state.answer
            for question, state in zip(self.questions, self.states, strict=True)
        }

    # ── movement ──────────────────────────────────────────────────────────

    def move(self, delta: int) -> None:
        """Highlight the option *delta* away, wrapping."""
        count = len(self.options())
        self.state.cursor = (self.state.cursor + delta) % count

    def go(self, delta: int) -> None:
        """Focus the question *delta* away, without wrapping."""
        self.current = max(0, min(len(self.questions) - 1, self.current + delta))

    def advance(self) -> None:
        """Focus the next unanswered question, if any."""
        count = len(self.questions)
        for step in range(1, count + 1):
            index = (self.current + step) % count
            if not self.states[index].answered:
                self.current = index
                return

    # ── answering ─────────────────────────────────────────────────────────

    def choose(self) -> bool:
        """Answer the current question with its highlighted option.

        Returns ``False`` (and changes nothing) when the highlight is on
        Other, which needs :meth:`answer_typed` instead.
        """
        if self.on_other():
            return False
        option = self.options()[self.state.cursor]
        self.state.answer = option
        self.state.label = option
        self.state.answered = True
        return True

    def answer_typed(self, typed: str, delivered: str | None = None) -> None:
        """Answer the current question with free text.

        *delivered* is what the agent receives (for example *typed* with an
        ``@`` mention's context prefix); it defaults to *typed*.
        """
        self.state.label = typed
        self.state.answer = typed if delivered is None else delivered
        self.state.answered = True
        self.state.cursor = len(self.options()) - 1
