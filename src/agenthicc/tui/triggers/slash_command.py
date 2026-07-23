"""Command and skill triggers — implements PRD-36, PRD-37, PRD-38, PRD-69."""

from __future__ import annotations

from agenthicc.commands.command import Command
from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerResult

_NAME_COL = 24  # characters reserved for the command name column


class SlashCommandTrigger(TriggerHandlerBase):
    """Trigger handler for "/" that opens the slash-command dropdown."""

    char = "/"
    label = "Command"
    skill_only = False
    include_aliases = False

    def __init__(self, registry: UnifiedCommandRegistry | None = None) -> None:
        self._registry = registry  # UnifiedCommandRegistry | None

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        if self._registry is None:
            return []
        partial = self.char + fragment
        cmds = [
            cmd
            for cmd in self._registry.matches(partial)
            if _is_skill_command(cmd) is self.skill_only
        ]
        results = []
        for cmd in cmds:
            names = (cmd.name, *cmd.aliases) if self.include_aliases else (cmd.name,)
            for name in names:
                if not name.startswith(partial):
                    continue
                # display: short single-line fallback for consumers without get_lines
                short_desc = (
                    cmd.description[:36] + "…" if len(cmd.description) > 36 else cmd.description
                )
                display = f"{name:<{_NAME_COL}} {short_desc}"
                results.append(
                    MatchItem(
                        display=display,
                        value=name,
                        hint=self._format_hint(cmd, name),
                        label=name,
                        detail=cmd.description,  # full, untruncated
                    )
                )
        return results

    def _format_hint(self, cmd: Command, name: str | None = None) -> str:
        display_name = name or cmd.name
        if cmd.argument_hint:
            return f"  ↑ {display_name} {cmd.argument_hint}  —  {cmd.description}"
        return f"  ↑ {display_name}  —  {cmd.description}"

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> TriggerResult:
        if item is None:
            return TriggerResult(buffer=buf + [self.char] + list(fragment))
        return TriggerResult(buffer=buf + list(item.value))

    def on_cancel(self, fragment: str, buf: list[str]) -> list[str]:
        return buf + [self.char] + list(fragment)

    def can_activate(self, buf: list[str]) -> bool:
        return not buf or buf[-1] == "\n"

    def get_hint(self, item: MatchItem | None) -> str | None:
        return item.hint if item and item.hint else None

    def get_lines(self, item: MatchItem, available_width: int) -> list[str]:
        """Two-column layout: command name left, description right with wrapping.

        When the description is short enough it fits on one line:
            "  ▶ /commands              List all registered commands"

        When too long it wraps under the description column:
            "  ▶ /commands              List all registered commands with
                                        their source and group"

        The indicator and indentation are NOT included — the overlay adds them.
        """
        name = item.label or item.value
        detail = item.detail or item.display

        # Space available for the description: total width minus name column
        # minus the 4-char indicator prefix ("  ▶ " / "    ") the overlay adds.
        indent_width = 4  # "  ▶ " or "    "
        name_field = _NAME_COL  # fixed column width for command name
        desc_col = indent_width + name_field + 1  # column where description starts
        desc_width = max(available_width - desc_col, 16)

        if len(detail) <= desc_width:
            # Fits on one line.
            return [f"{name:<{name_field}} {detail}"]

        # Wrap: break detail into chunks of desc_width.
        chunks: list[str] = []
        remaining = detail
        while remaining:
            # Try to break at a word boundary within desc_width.
            if len(remaining) <= desc_width:
                chunks.append(remaining)
                break
            cut = remaining.rfind(" ", 0, desc_width + 1)
            if cut <= 0:
                cut = desc_width
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()

        lines = [f"{name:<{name_field}} {chunks[0]}"]
        continuation_prefix = " " * (name_field + 1)  # aligns under description
        for chunk in chunks[1:]:
            lines.append(f"{continuation_prefix}{chunk}")
        return lines


def _is_skill_command(cmd: Command) -> bool:
    """Return whether a command record belongs to the skill namespace."""
    return cmd.is_skill


class SkillTrigger(SlashCommandTrigger):
    """Trigger handler for "$" that opens the skill-only dropdown."""

    char = "$"
    label = "Skill"
    skill_only = True
    include_aliases = True
