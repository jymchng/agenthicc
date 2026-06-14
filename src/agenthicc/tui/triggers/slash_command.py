"""Slash-command trigger."""
from __future__ import annotations
from typing import Any
from ..trigger import MatchItem, TriggerContext
from ..input_bar import CommandRegistry, build_default_registry


class SlashCommandTrigger:
    """Trigger for /command completion."""

    char = "/"

    def __init__(self, registry: CommandRegistry | None = None) -> None:
        # If None is explicitly passed, keep it None (no default registry).
        self._registry = registry

    def can_trigger(self, ctx: TriggerContext) -> bool:
        return ctx.text.startswith("/") or " /" in ctx.text

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        """Return matches for the given fragment.

        fragment is the text after the "/" (e.g. "mod" for "/mod").
        """
        if self._registry is None:
            return []
        partial = "/" + fragment if not fragment.startswith("/") else fragment
        specs = self._registry.matches(partial)
        # Sort alphabetically by name
        specs = sorted(specs, key=lambda s: s.name)
        items: list[MatchItem] = []
        for spec in specs:
            hint = ""
            if spec.argument_hint:
                hint = f"{spec.name}  {spec.description}  {spec.argument_hint}"
            else:
                hint = f"{spec.name}  {spec.description}"
            items.append(
                MatchItem(
                    display=f"{spec.name}  {spec.description}",
                    value=spec.name,
                    description=spec.description,
                    hint=hint,
                )
            )
        return items

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> list[str]:
        """Replace the /fragment in buf with item.value (or restore /fragment if item is None)."""
        if item is None:
            return list(buf) + list("/" + fragment)
        return list(buf) + list(item.value)

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        """Cancel selection — restore /fragment in buf."""
        return list(buf) + list("/" + fragment)

    def get_hint(self, item: MatchItem | None) -> str | None:
        """Return hint string for the item, or None."""
        if item is None:
            return None
        hint = getattr(item, "hint", "")
        if not hint:
            return None
        return hint

    def apply(self, ctx: TriggerContext, item: MatchItem) -> str:
        slash_idx = ctx.text.rfind("/")
        if slash_idx == -1:
            return ctx.text
        return ctx.text[:slash_idx] + item.value
