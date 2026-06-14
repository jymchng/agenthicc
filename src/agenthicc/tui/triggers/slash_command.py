"""Slash-command trigger — implements PRD-36, PRD-37, PRD-38."""
from __future__ import annotations
from agenthicc.tui.trigger import TriggerHandler, TriggerContext, MatchItem

class SlashCommandTrigger:
    """Trigger handler for "/" that opens the slash-command dropdown."""
    char = "/"

    def __init__(self, registry=None) -> None:
        self._registry = registry  # UnifiedCommandRegistry | None (PRD-44)

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        if self._registry is None:
            return []
        partial = "/" + fragment
        cmds = self._registry.matches(partial)
        results = []
        for cmd in cmds:
            # Keep description short so the full row fits in one terminal line.
            # The renderer also truncates, but trimming here keeps display clean.
            desc = cmd.description[:36] + "…" if len(cmd.description) > 36 else cmd.description
            display = f"{cmd.name:<22} {desc}"
            hint = self._format_hint(cmd)
            results.append(MatchItem(display=display, value=cmd.name, hint=hint))
        return results

    def _format_hint(self, cmd) -> str:
        if cmd.argument_hint:
            return f"  ↑ {cmd.name} {cmd.argument_hint}  —  {cmd.description}"
        return f"  ↑ {cmd.name}  —  {cmd.description}"

    def on_select(self, item, fragment, buf):
        if item is None:
            return buf + ["/"] + list(fragment)
        return buf + list(item.value)

    def on_cancel(self, fragment, buf):
        return buf + ["/"] + list(fragment)

    def can_activate(self, buf: list[str]) -> bool:
        # Commands are always top-level: only activate on an empty buffer.
        # A '/' typed mid-sentence (e.g. inside '@docs/') is a literal character.
        return not buf

    def get_hint(self, item) -> str | None:
        return item.hint if item and item.hint else None
