"""One-line conversation notices of setting changes (TUX-05 §2.5).

``model_changed`` is a presentation event of the TUI conversation (not a
kernel event): the payload names the provider, model, profile and the
previous ``provider/model`` label, and never a credential. It renders as::

    ◆ model → claude-sonnet-4-6 (from next turn)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agenthicc.tui.workspace.appender import register_renderer

if TYPE_CHECKING:
    from agenthicc.tui.conversation_store import ConversationEvent
    from agenthicc.tui.workspace.appender import ScrollBufferAppender

__all__ = ["MODEL_CHANGED", "model_changed_text"]

MODEL_CHANGED = "model_changed"


def model_changed_text(payload: dict[str, object]) -> str:
    """The notice's plain text for a ``model_changed`` payload."""
    from agenthicc.runners.model_switch import short_model
    from agenthicc.tui.theme import glyphs

    g = glyphs()
    model = short_model(str(payload.get("model", "")))
    profile = str(payload.get("profile", ""))
    name = f"{profile} ({model})" if profile else model
    return f"{g.diamond} model {g.right} {name} (from next turn)"


@register_renderer(MODEL_CHANGED)
def _render_model_changed(self: ScrollBufferAppender, ev: ConversationEvent) -> None:
    from rich.text import Text

    from agenthicc.tui.theme import palette

    p = palette()
    text = model_changed_text(ev.payload)
    line = Text(" ")
    line.append(text[:1], style=p.accent)
    line.append(text[1:], style=p.muted)
    self._console.print(line, highlight=False)
