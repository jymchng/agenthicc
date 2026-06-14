"""@mention filesystem trigger."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..trigger import MatchItem, TriggerContext
from ..mention_input import _get_matches

__all__ = ["AtMentionTrigger"]


class AtMentionTrigger:
    """Trigger handler for the '@' character — completes file paths."""

    char = "@"

    def __init__(self, base_path: str | Path | None = None) -> None:
        self._base = Path(base_path).resolve() if base_path is not None else None

    def can_trigger(self, ctx: TriggerContext) -> bool:
        return "@" in ctx.text

    def get_matches(self, fragment_or_ctx: Any, ctx: TriggerContext | None = None) -> list[MatchItem]:
        """Support both old-style (fragment, ctx) and new-style (ctx) signatures."""
        if isinstance(fragment_or_ctx, TriggerContext):
            # New-style: get_matches(ctx)
            actual_ctx = fragment_or_ctx
            fragment = actual_ctx.fragment
            base = Path(actual_ctx.cwd) if actual_ctx.cwd else (self._base or Path("."))
        else:
            # Old-style: get_matches(fragment, ctx)
            fragment = fragment_or_ctx
            actual_ctx = ctx
            if actual_ctx is not None:
                base = Path(actual_ctx.cwd) if actual_ctx.cwd else (self._base or Path("."))
            else:
                base = self._base or Path(".")
        raw = _get_matches(fragment, base)
        return [MatchItem(display=name, value=name, hint="") for name, _meta in raw]

    def on_select(self, item: MatchItem | None, fragment: str, buf: list[str]) -> list[str]:
        if item is None:
            return list(buf) + ["@"] + list(fragment)
        return list(buf) + list("@" + item.value)

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        return list(buf) + ["@"] + list(fragment)

    def get_hint(self, item: MatchItem | None) -> str | None:
        if item is None:
            return None
        return item.value if item.value else None

    def apply(self, ctx: TriggerContext, item: MatchItem) -> str:
        at_idx = ctx.text.rfind("@")
        if at_idx == -1:
            return ctx.text
        return ctx.text[:at_idx + 1] + item.value
