"""Command and skill triggers — PRD-36, PRD-37, PRD-38, TUX-05.

The ``/`` picker matches fuzzily over a command's name, aliases and
description (:meth:`UnifiedCommandRegistry.search`), breaks ties by how often
and how recently the command ran in this project (:class:`CommandUsage`), and
with an empty fragment lists the commands grouped: *Suggested* (the five
most used) and then the registry's groups. Aliases collapse into their
command's row, and a command bound to a key shows the key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from agenthicc.commands.command import BusyPolicy, Command
from agenthicc.commands.registry import UnifiedCommandRegistry
from agenthicc.tui.trigger import MatchItem, TriggerContext, TriggerHandlerBase, TriggerResult

if TYPE_CHECKING:
    from agenthicc.tui.input.command_usage import CommandUsage
    from agenthicc.tui.keymap import Keymap
    from agenthicc.workflows.registry import WorkflowRegistry

__all__ = ["SUGGESTED_GROUP", "SkillTrigger", "SlashCommandTrigger", "command_keys"]

#: The heading of the most used commands in an empty-fragment listing.
SUGGESTED_GROUP = "Suggested"
_SUGGESTED_COUNT = 5


def command_keys(name: str, keymap: Keymap | None = None) -> str:
    """The key bound to the action that does what command *name* does, or ``""``.

    *keymap* is the user's effective keymap; without one the defaults apply.
    """
    from agenthicc.tui.actions import COMMAND_ACTIONS, DEFAULT_ACTIONS

    action_name = COMMAND_ACTIONS.get(name)
    if action_name is None:
        return ""
    if keymap is not None:
        keys = keymap.action(action_name).keys
    else:
        keys = next((a.keys for a in DEFAULT_ACTIONS if a.name == action_name), ())
    return keys[0] if keys else ""


class SlashCommandTrigger(TriggerHandlerBase):
    """Trigger handler for "/" that opens the slash-command dropdown."""

    char = "/"
    label = "Command"
    skill_only = False

    def __init__(
        self,
        registry: UnifiedCommandRegistry | None = None,
        workflow_registry: WorkflowRegistry | None = None,
        *,
        usage: CommandUsage | None = None,
        keymap: Callable[[], Keymap] | None = None,
    ) -> None:
        self._registry = registry  # UnifiedCommandRegistry | None
        # Keep the live registry reference rather than taking a snapshot.  The
        # TUI reload path replaces the registry contents in place, so newly
        # discovered workflows become available without rebuilding triggers.
        self._workflow_registry = workflow_registry
        self._usage = usage
        self._keymap = keymap

    def get_matches(self, fragment: str, ctx: TriggerContext) -> list[MatchItem]:
        if self._registry is None:
            return []

        argument_matches = self._get_argument_matches(fragment)
        if argument_matches is not None:
            return argument_matches

        keymap = self._keymap() if self._keymap is not None else None
        counts = self._usage.counts() if self._usage is not None else {}
        if fragment:
            ranked = [
                cmd
                for cmd in self._registry.search(self.char + fragment, counts)
                if _is_skill_command(cmd) is self.skill_only
            ]
            return [self._row(cmd, "", ctx.busy, keymap) for cmd in ranked]

        commands = [
            cmd
            for cmd in self._registry.all_commands()
            if _is_skill_command(cmd) is self.skill_only
        ]
        by_name = {cmd.name: cmd for cmd in commands}
        suggested = [
            name
            for name, _ in sorted(counts.items(), key=lambda item: (-item[1][0], -item[1][1]))
            if name in by_name
        ][:_SUGGESTED_COUNT]
        rows = [self._row(by_name[name], SUGGESTED_GROUP, ctx.busy, keymap) for name in suggested]
        for group in self._registry.groups():
            rows.extend(
                self._row(cmd, group, ctx.busy, keymap)
                for cmd in commands
                if cmd.group == group and cmd.name not in suggested
            )
        return rows

    def record_use(self, text: str) -> None:
        """Count a submitted command toward the picker's ranking."""
        if self._usage is None or self._registry is None:
            return
        command = self._registry.get(text.split(None, 1)[0]) if text.strip() else None
        if command is not None and _is_skill_command(command) is self.skill_only:
            self._usage.record(command.name)

    def _row(self, cmd: Command, group: str, busy: bool, keymap: Keymap | None) -> MatchItem:
        detail = self._display_description(cmd)
        if cmd.aliases:
            word = "alias" if len(cmd.aliases) == 1 else "aliases"
            detail = f"{detail} ({word} {', '.join(cmd.aliases)})"
        disabled = ""
        if busy:
            # The picker and manual submission read the same busy policy.
            policy = _policy(cmd)
            if policy is BusyPolicy.REJECT:
                disabled = "not available while a turn runs"
            else:
                detail = f"{detail}  [{_BUSY_LABELS.get(policy, 'queues while busy')}]"
        return MatchItem(
            value=cmd.name,
            label=cmd.name,
            detail=detail,
            group=group,
            keys=command_keys(cmd.name, keymap),
            disabled=disabled,
        )

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

        prefix = argument_part.strip()
        completions: list[str] = []
        if command.completions_factory is not None:
            try:
                completions = command.completions_factory(prefix)
            except Exception:  # noqa: BLE001
                # A plugin completion provider must never break the input
                # loop.  It simply contributes no suggestions for this turn.
                return []
        if (
            command.name == "/workflow"
            and self._workflow_registry is not None
            and " " not in prefix
        ):
            completions.extend(
                name for name in self._workflow_registry.names() if name.startswith(prefix)
            )

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
                if command.name == "/workflow"
                and completion not in {"reload", "resume", "reset"}
                and self._workflow_registry is not None
                else None
            )
            detail = (
                "Continue a paused or interrupted workflow"
                if completion == "resume"
                else "Reload workflows from disk"
                if completion == "reload"
                else "Discard a recoverable workflow"
                if completion == "reset"
                else workflow.description
                if workflow is not None
                else f"Use {value}"
            )
            results.append(MatchItem(value=value, label=value, detail=detail))
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

    def registry_command(self, name: str) -> Command | None:
        """The command *name* (or an alias of it) names, if registered."""
        return self._registry.get(name) if self._registry is not None else None

    def argument_need(self, name: str) -> Literal["", "completions", "required"]:
        """How the command palette continues after selecting *name*."""
        command = self.registry_command(name)
        if command is None:
            return ""
        if self.has_argument_completions(MatchItem(value=command.name, label=command.name)):
            return "completions"
        return "required" if _requires_argument(command.argument_hint) else ""

    def _display_description(self, cmd: Command) -> str:
        """Return the description used by the picker and its fallback row."""
        return cmd.description

    def on_select(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> TriggerResult:
        if item is None:
            return TriggerResult(buffer=buf + [self.char] + list(fragment))
        return TriggerResult(buffer=buf + list(item.value))

    def on_enter(
        self,
        item: MatchItem | None,
        fragment: str,
        buf: list[str],
    ) -> TriggerResult:
        """Submit an exact invocation or place the highlighted completion.

        An exact command name or alias can be submitted as typed when its
        arguments are optional. An incomplete selection can continue into
        argument entry, as can a bare command with a required argument.
        """
        typed = self.char + fragment
        command = self.registry_command(typed)
        if item is None or (
            command is not None
            and command.name == item.value
            and not _requires_argument(command.argument_hint)
        ):
            return TriggerResult(buffer=buf + list(typed), submit=True)
        if " " not in item.value and self.needs_argument(item):
            return TriggerResult(buffer=buf + list(item.value + " "), keep_open=True)
        return TriggerResult(buffer=buf + list(item.value), submit=item.value == typed)

    def needs_argument(self, item: MatchItem) -> bool:
        """Whether accepting *item* can continue into argument entry."""
        if self._registry is None:
            return False
        command = self._registry.get(item.value)
        if command is None:
            return False
        return self.has_argument_completions(item) or _requires_argument(command.argument_hint)

    def can_activate(self, buf: list[str]) -> bool:
        return not buf or buf[-1] == "\n"


#: What the picker says about a command while a turn runs.
_BUSY_LABELS: dict[BusyPolicy, str] = {
    BusyPolicy.IMMEDIATE_READ_ONLY: "runs now",
    BusyPolicy.IMMEDIATE_CONTROL: "control runs now",
    BusyPolicy.QUEUE: "queues while busy",
}


def _policy(cmd: Command) -> BusyPolicy:
    """The command's policy with no arguments; a broken resolver queues."""
    try:
        return cmd.policy_for_args("")
    except Exception:  # noqa: BLE001
        return BusyPolicy.QUEUE


def _requires_argument(argument_hint: str) -> bool:
    """Whether a hint's first form names a required ``<argument>``.

    ``"<name> | resume [run-id]"`` needs one; ``"[provider] [model]"`` and
    ``""`` do not.
    """
    first = argument_hint.split("|", 1)[0]
    return any(token.startswith("<") for token in first.split())


def _is_skill_command(cmd: Command) -> bool:
    """Return whether a command record belongs to the skill namespace."""
    return cmd.is_skill


class SkillTrigger(SlashCommandTrigger):
    """Trigger handler for "$" that opens the skill-only dropdown."""

    char = "$"
    label = "Skill"
    skill_only = True

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
