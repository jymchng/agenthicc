"""One-line conversation notices of setting changes (TUX-05 §2.5).

``model_changed`` is a presentation event of the TUI conversation (not a
kernel event): the payload names the provider, model, profile and the
previous ``provider/model`` label, and never a credential. The scroll buffer
(``tui.workspace.appender``) renders it as::

    ◆ model → claude-sonnet-4-6 (from next turn)
"""

from __future__ import annotations

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
