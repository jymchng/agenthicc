"""ApprovalOverlay — a tool or workspace-access approval in a :class:`Dialog` (PRD-78, TUX-07).

The dialog explains the action (the whole command, a diff, the paths, the
host; see :mod:`.approval_body`), says why approval is needed, and offers
four numbered choices:

1. Yes
2. Yes, and allow <commands> for the rest of this turn
3. Yes, and allow <commands> this session
4. No, and tell the agent what to do instead — an embedded composer opens in
   the dialog, and the text reaches the model with the denial.

``1``–``4`` choose and confirm; ``y``/``a``/``A``/``n`` remain aliases (``n``
denies at once); Esc denies. A request from a subagent worker names it in the
badge, and ``o`` opens that worker's transcript first (AGX-08 §5.5).
"""

from __future__ import annotations

from dataclasses import dataclass
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
from agenthicc.tui.workspace.overlays.approval_body import approval_content, grant_noun

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.text import Text

    from agenthicc.tools.approval import ApprovalRequest, ApprovalRequester, InteractiveApprovals
    from agenthicc.tui.input.embedded import EmbeddedComposer

__all__ = ["ApprovalChoice", "ApprovalOverlay", "approval_options"]


class _Grant(Enum):
    ONCE = auto()
    TURN = auto()
    SESSION = auto()
    DENY = auto()


@dataclass(frozen=True)
class ApprovalChoice:
    """One option of an approval dialog and the response it sends."""

    label: str
    grant: _Grant

    @property
    def allowed(self) -> bool:
        return self.grant is not _Grant.DENY


_ALIASES = {
    "dialog.allow_once": _Grant.ONCE,
    "dialog.allow_turn": _Grant.TURN,
    "dialog.allow_session": _Grant.SESSION,
    "dialog.deny": _Grant.DENY,
}
_TELL = "No, and tell the agent what to do instead"


def approval_options(req: ApprovalRequest) -> list[ApprovalChoice]:
    """The four choices, worded for what the grant covers."""
    if req.workspace_access:
        return [
            ApprovalChoice("Yes, this target once", _Grant.ONCE),
            ApprovalChoice("Yes, and allow this target for the rest of this turn", _Grant.TURN),
            ApprovalChoice("Yes, and allow this target this session", _Grant.SESSION),
            ApprovalChoice(_TELL, _Grant.DENY),
        ]
    noun = grant_noun(req.capabilities)
    return [
        ApprovalChoice("Yes", _Grant.ONCE),
        ApprovalChoice(f"Yes, and allow {noun} for the rest of this turn", _Grant.TURN),
        ApprovalChoice(f"Yes, and allow {noun} this session", _Grant.SESSION),
        ApprovalChoice(_TELL, _Grant.DENY),
    ]


class ApprovalOverlay(Overlay):
    """Approval prompt shown while the agent is paused waiting for user input."""

    name = "approval"

    def __init__(
        self,
        req: ApprovalRequest,
        service: InteractiveApprovals,
        close_fn: Callable[[], None],
        *,
        open_requester: Callable[[ApprovalRequester], None] | None = None,
        settings: DialogSettings = DEFAULT_DIALOG_SETTINGS,
    ) -> None:
        self._req = req
        self._service = service
        self._close = close_fn
        self._open_requester = open_requester
        self._settings = settings
        self._choices = approval_options(req)
        self._composer: EmbeddedComposer | None = None
        self.dialog = self._build_dialog()

    def _build_dialog(self) -> Dialog:
        req = self._req
        content = approval_content(req, self._settings.workspace)
        requester = req.requester
        badge: str | None = None
        token = ""
        conditions = {"approval"}
        if requester is not None:
            badge = requester.label
            token = "agent"
            if self._open_requester is not None:
                badge = f"{requester.label} {_sep()} o open transcript"
                conditions.add("requester")
        dialog = Dialog(
            content.title,
            settings=self._settings,
            badge=badge,
            badge_token=token,
            cancel_label="no",
        )
        dialog.summary = content.summary
        dialog.reason = req.reason
        dialog.options = [
            DialogOption(choice.label, destructive=not choice.allowed) for choice in self._choices
        ]
        dialog.conditions = frozenset(conditions)
        dialog.set_body(id(content), lambda: list(content.body))
        if content.details is not None:
            details = content.details
            dialog.set_details(lambda: list(details))
        return dialog

    # ── Overlay interface ──────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.dialog.selected = 0
        self.dialog.scroll = 0
        self._stop_composing()

    def on_unmount(self) -> None:
        self._stop_composing()

    def set_viewport(self, width: int, height: int) -> None:
        self.dialog.set_viewport(width, height)

    def render(self) -> RenderableType:
        return self.dialog.render()

    def handle_key(self, key: Key, ch: str) -> bool:
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
                self._choose(self._choices[result.index])
            case DialogKey.CANCEL:
                self._respond(_Grant.DENY)
            case DialogKey.ALIAS:
                self._respond(_ALIASES[result.action])
            case DialogKey.OPEN_REQUESTER:
                if self._req.requester is not None and self._open_requester is not None:
                    self._open_requester(self._req.requester)
            case _:
                pass
        return True  # overlay always consumes all keys

    # ── helpers ───────────────────────────────────────────────────────────────

    def _choose(self, choice: ApprovalChoice) -> None:
        if choice.grant is _Grant.DENY:
            self._start_composing()
        else:
            self._respond(choice.grant)

    def _start_composing(self) -> None:
        from agenthicc.tui.input.embedded import ANSWER_EXCLUDED, ComposerContext, EmbeddedComposer

        context = self._settings.composer or ComposerContext(cwd=Path.cwd())
        self._composer = EmbeddedComposer(
            context,
            on_submit=self._submit_instructions,
            redraw=self._settings.redraw,
            exclude=ANSWER_EXCLUDED(),
        )
        dialog = self.dialog
        dialog.footer = self._composer_rows
        dialog.legend_override = ("Enter send", "Ctrl+J newline", "Esc back")

    def _stop_composing(self) -> None:
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
        rows = [Text(""), Text(" Tell the agent what to do instead:", style=palette().muted)]
        return rows + render_lines(composer.render(), width)

    async def _submit_instructions(self, text: str) -> bool:
        self._respond(_Grant.DENY, message=text.strip())
        return True

    def _respond(self, grant: _Grant, *, message: str = "") -> None:
        scoped = bool(self._req.workspace_access)
        scope_grant = (
            {
                _Grant.ONCE: "target_once",
                _Grant.TURN: "target_turn",
                _Grant.SESSION: "target_session",
            }.get(grant)
            if scoped
            else None
        )
        self._service.respond(
            allowed=grant is not _Grant.DENY,
            remember=grant is _Grant.TURN and not scoped,
            remember_all=grant is _Grant.SESSION and not scoped,
            message=message,
            scope_grant=scope_grant,
        )
        self._stop_composing()
        self._close()


def _sep() -> str:
    from agenthicc.tui.theme import glyphs

    return glyphs().sep
