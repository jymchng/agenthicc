"""Command and skill triggers — implements PRD-36, PRD-37, PRD-38, PRD-69."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agenthicc.commands.command import BusyPolicy, Command
from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerResult

if TYPE_CHECKING:
    from agenthicc.workflows.registry import WorkflowRegistry

_NAME_COL = 24  # characters reserved for the command name column


class SlashCommandTrigger(TriggerHandlerBase):
    """Trigger handler for "/" that opens the slash-command dropdown."""

    char = "/"
    label = "Command"
    skill_only = False
    include_aliases = False

    def __init__(
        self,
        registry: UnifiedCommandRegistry | None = None,
        workflow_registry: WorkflowRegistry | None = None,
    ) -> None:
        self._registry = registry  # UnifiedCommandRegistry | None
        # Keep the live registry reference rather than taking a snapshot.  The
        # TUI reload path replaces the registry contents in place, so newly
        # discovered workflows become available without rebuilding triggers.
        self._workflow_registry = workflow_registry

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        if self._registry is None:
            return []

        argument_matches = self._get_argument_matches(fragment)
        if argument_matches is not None:
            return argument_matches

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
                description = self._display_description(cmd)
                short_desc = description[:36] + "…" if len(description) > 36 else description
                display = f"{name:<{_NAME_COL}} {short_desc}"
                results.append(
                    MatchItem(
                        display=display,
                        value=name,
                        hint=self._format_hint(cmd, name, busy=ctx.busy),
                        label=name,
                        detail=self._detail(cmd, ctx.busy),
                    )
                )
        return results

    def _get_argument_matches(self, fragment: str) -> list[MatchItem] | None:
        """Return argument completions when *fragment* contains a command.

        The trigger fragment intentionally includes everything after ``/``.
        For example, ``workflow co`` is parsed as command ``/workflow`` and
        argument prefix ``co``.  Returning ``None`` means that the caller
        should perform the normal command-name lookup instead of treating the
        fragment as an argument-bearing command.
        """
        if " " not in fragment or self._registry is None:
            return None

        command_part, argument_part = fragment.split(" ", 1)
        if not command_part:
            return []

        command = self._registry.get(self.char + command_part)
        if command is None or _is_skill_command(command) is not self.skill_only:
            return []

        completions: list[str]
        if command.name == "/workflow":
            if self._workflow_registry is None:
                return []
            # Workflow names are identifiers, not free-form text.  Do not
            # return a partially matched name: the selected value must be a
            # complete command that TUISession.route() can execute.
            prefix = argument_part.strip()
            if " " in prefix:
                return []
            completions = [
                name for name in self._workflow_registry.names() if name.startswith(prefix)
            ]
        elif command.completions_factory is not None:
            try:
                completions = command.completions_factory(argument_part.strip())
            except Exception:  # noqa: BLE001
                # A plugin completion provider must never break the input
                # loop.  It simply contributes no suggestions for this turn.
                return []
        else:
            return []

        try:
            unique_completions = sorted(set(completions))
        except (TypeError, ValueError):
            # A malformed plugin result is handled like a failed provider;
            # completion must never take down the interactive input loop.
            return []

        results: list[MatchItem] = []
        for completion in unique_completions:
            if not isinstance(completion, str) or not completion:
                continue
            value = f"{command.name} {completion}"
            workflow = (
                self._workflow_registry.get(completion)
                if command.name == "/workflow" and self._workflow_registry is not None
                else None
            )
            detail = workflow.description if workflow is not None else f"Use {value}"
            results.append(
                MatchItem(
                    display=f"{value:<{_NAME_COL}} {detail}",
                    value=value,
                    hint=f"  ↑ {value}  —  {detail}",
                    label=value,
                    detail=detail,
                )
            )
        return results

    def has_argument_completions(self, item: MatchItem | None) -> bool:
        """Whether selecting *item* can continue into argument completion."""
        if item is None or self._registry is None:
            return False
        command = self._registry.get(item.value)
        return command is not None and (
            command.completions_factory is not None
            or (command.name == "/workflow" and self._workflow_registry is not None)
        )

    def _display_description(self, cmd: Command) -> str:
        """Return the description used by the picker and its fallback row."""
        return cmd.description

    @staticmethod
    def _busy_label(cmd: Command) -> str:
        try:
            policy = cmd.policy_for_args("")
        except Exception:  # noqa: BLE001
            policy = BusyPolicy.QUEUE
        return {
            BusyPolicy.IMMEDIATE_READ_ONLY: "runs now",
            BusyPolicy.IMMEDIATE_CONTROL: "control runs now",
            BusyPolicy.QUEUE: "queues while busy",
            BusyPolicy.REJECT: "unavailable while busy",
        }.get(policy, "queues while busy")

    def _format_hint(self, cmd: Command, name: str | None = None, *, busy: bool = False) -> str:
        display_name = name or cmd.name
        availability = f"  • {self._busy_label(cmd)}" if busy else ""
        description = self._display_description(cmd)
        if cmd.argument_hint:
            return f"  ↑ {display_name} {cmd.argument_hint}  —  {description}{availability}"
        return f"  ↑ {display_name}  —  {description}{availability}"

    def _detail(self, cmd: Command, busy: bool) -> str:
        description = self._display_description(cmd)
        if not busy:
            return description
        return f"{description}  [{self._busy_label(cmd)}]"

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

    _DESCRIPTION_LIMIT = 220

    def _display_description(self, cmd: Command) -> str:
        """Compact skill metadata into a readable picker description."""
        description = " ".join(cmd.description.split())
        if len(description) <= self._DESCRIPTION_LIMIT:
            return description
        cutoff = description.rfind(" ", 0, self._DESCRIPTION_LIMIT - 1)
        if cutoff < 1:
            cutoff = self._DESCRIPTION_LIMIT - 1
        return description[:cutoff].rstrip() + "…"
