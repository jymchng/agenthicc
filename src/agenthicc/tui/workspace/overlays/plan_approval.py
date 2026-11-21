"""PlanApprovalOverlay — plan review in a :class:`Dialog` (TUX-07).

Shown when a workflow's human-review phase calls the approval service with
``kind="plan_review"``. The body is the rendered Markdown plan: it uses the
dialog's full height and scrolls (``[``/``]``, Page Up/Down), and a short plan
shrinks the dialog to fit.

Options, for a request that offers execution modes:

1. Approve and run in Safe mode
2. Approve and run in Yolo mode
3. Keep planning — the agent refines the plan and asks again
4. Reject, with feedback — the embedded composer opens in the dialog

A request without modes offers "Approve" in place of the first two. ``Tab``
adds a note to any choice through the same composer (it replaces the old
"Approve — add instructions" option). Esc is the safe exit "not now": the
dialog folds to one line and the plan stays pending; Enter reopens it.
"""

from __future__ import annotations

from dataclasses import dataclass
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

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text

    from agenthicc.tools.approval import ApprovalRequest, InteractiveApprovals
    from agenthicc.tui.input.embedded import EmbeddedComposer

__all__ = [
    "KEEP_PLANNING_MESSAGE",
    "PLAN_TITLE",
    "PlanApprovalOverlay",
    "PlanChoice",
    "plan_options",
]

PLAN_TITLE = "Approve the plan"

#: What "Keep planning" tells the agent (the plan is neither approved nor rejected).
KEEP_PLANNING_MESSAGE = (
    "Keep planning: the user has not approved the plan yet. Refine it, then ask for approval again."
)


@dataclass(frozen=True)
class PlanChoice:
    """One option of the plan review and the response it sends."""

    label: str
    allowed: bool
    mode: str | None = None
    #: Opens the composer before responding (the reply is the feedback).
    needs_text: bool = False
    message: str = ""
    destructive: bool = False


def _mode_values(req: ApprovalRequest) -> list[str]:
    """Supported, canonical modes advertised by the request."""
    values: list[str] = []
    for value in req.mode_options:
        canonical = {"safe": "Safe", "yolo": "Yolo"}.get(value.casefold())
        if canonical is not None and canonical not in values:
            values.append(canonical)
    return values


def plan_options(req: ApprovalRequest) -> list[PlanChoice]:
    """The choices for *req*: one approval per offered mode, else plain "Approve"."""
    modes = _mode_values(req)
    approvals = (
        [PlanChoice(f"Approve and run in {mode} mode", True, mode) for mode in modes]
        if modes
        else [PlanChoice("Approve", True)]
    )
    return [
        *approvals,
        PlanChoice("Keep planning", False, message=KEEP_PLANNING_MESSAGE),
        PlanChoice("Reject, with feedback", False, needs_text=True, destructive=True),
    ]


class PlanApprovalOverlay(Overlay):
    """Plan review with Safe/Yolo execution choices."""

    name = "plan_approval"

    def __init__(
        self,
        req: ApprovalRequest,
        service: InteractiveApprovals,
        close_fn: Callable[[], None],
        *,
        settings: DialogSettings = DEFAULT_DIALOG_SETTINGS,
    ) -> None:
        self._req = req
        self._service = service
        self._close = close_fn
        self._settings = settings
        self._choices = plan_options(req)
        self._composer: EmbeddedComposer | None = None
        self._composing: PlanChoice | None = None
        #: Esc folded the dialog ("not now"); the plan stays pending.
        self.folded = False
        dialog = Dialog(PLAN_TITLE, settings=settings, cancel_label="not now")
        dialog.summary = "the plan is shown below"
        dialog.reason = req.reason
        dialog.options = [
            DialogOption(choice.label, destructive=choice.destructive) for choice in self._choices
        ]
        dialog.conditions = frozenset({"note"})
        raw = (req.tool_input or {}).get("plan", "")
        plan = raw if isinstance(raw, str) else str(raw)
        dialog.set_body(plan, lambda: self._plan_body(plan))
        self.dialog = dialog

    @staticmethod
    def _plan_body(plan: str) -> list[RenderableType]:
        from rich.markdown import Markdown
        from rich.text import Text

        from agenthicc.tui.theme import palette

        if not plan.strip():
            return [Text("no plan content", style=palette().muted)]
        return [Markdown(plan)]

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.dialog.selected = 0
        self.dialog.scroll = 0
        self.folded = False
        self._stop_composing()

    def on_unmount(self) -> None:
        self._stop_composing()

    def set_viewport(self, width: int, height: int) -> None:
        self.dialog.set_viewport(width, height)

    def render(self) -> RenderableType:
        if self.folded:
            return self._render_folded()
        return self.dialog.render()

    def _render_folded(self) -> RenderableType:
        from rich.text import Text

        from agenthicc.tui.theme import glyphs, palette

        p, g = palette(), glyphs()
        return Text.assemble(
            (f"{g.bullet} ", p.warning),
            ("The plan is waiting for your review", "bold"),
            (f"  {g.sep} Enter reopen", p.muted),
        )

    def handle_key(self, key: Key, ch: str) -> bool:
        if self.folded:
            if key is Key.ENTER:
                self.folded = False
            return True
        composer = self._composer
        if composer is not None:
            if key is Key.ESC and not composer.picker_open:
                self._stop_composing()
            else:
                composer.feed(key, ch)
            return True
        result = self.dialog.handle_key(key, ch)
        match result.kind:
            case DialogKey.CHOOSE:
                choice = self._choices[result.index]
                if choice.needs_text:
                    self._start_composing(choice)
                else:
                    self._respond(choice, choice.message)
            case DialogKey.NOTE:
                self._start_composing(self._choices[result.index])
            case DialogKey.CANCEL:
                self.folded = True
            case _:
                pass
        return True

    # ── composer ──────────────────────────────────────────────────────────────

    @property
    def _prompt_text(self) -> str:
        return self._composer.text if self._composer is not None else ""

    def _start_composing(self, choice: PlanChoice) -> None:
        from agenthicc.tui.input.embedded import ANSWER_EXCLUDED, ComposerContext, EmbeddedComposer

        context = self._settings.composer or ComposerContext(cwd=Path.cwd())
        self._composing = choice
        self._composer = EmbeddedComposer(
            context, on_submit=self._submit, redraw=self._settings.redraw, exclude=ANSWER_EXCLUDED()
        )
        self.dialog.footer = self._composer_rows
        self.dialog.legend_override = ("Enter send", "Ctrl+J newline", "Esc back")

    def _stop_composing(self) -> None:
        if self._composer is not None:
            self._composer.close()
            self._composer = None
        self._composing = None
        self.dialog.footer = None
        self.dialog.legend_override = ()

    def _composer_rows(self, width: int) -> list[Text]:
        from rich.text import Text

        from agenthicc.tui.theme import palette
        from agenthicc.tui.workspace.dialog import render_lines

        composer, choice = self._composer, self._composing
        if composer is None or choice is None:
            return []
        prompt = "What should change?" if choice.needs_text else f"A note with {choice.label!r}:"
        rows = [Text(""), Text(f" {prompt}", style=palette().muted)]
        return rows + render_lines(composer.render(), width)

    async def _submit(self, text: str) -> bool:
        choice = self._composing
        if choice is None:
            return False
        note = text.strip()
        message = note if choice.needs_text or not choice.message else f"{choice.message}\n\n{note}"
        self._respond(choice, message if note else choice.message)
        return True

    def _respond(self, choice: PlanChoice, message: str) -> None:
        if choice.mode is None:
            self._service.respond(allowed=choice.allowed, message=message)
        else:
            self._service.respond(allowed=choice.allowed, message=message, mode=choice.mode)
        self._stop_composing()
        self._close()
