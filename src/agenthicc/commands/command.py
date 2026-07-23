"""Command dataclass, CommandContext, and related type aliases (PRD-44, PRD-45)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from agenthicc.config import AgenthiccConfig
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
    from agenthicc.tui.workspace.overlay import Overlay
    from agenthicc.tui.runtime.mode_manager import ModeManager

__all__ = [
    "Command",
    "CommandContext",
    "CommandHandler",
    "MenuFactory",
    "CompletionsFactory",
]


@dataclass
class CommandContext:
    """Runtime state available to command handler functions.

    All command-needed state is held as direct fields — never access renderer
    attributes from command handlers; that caused NameErrors and silent failures
    when the renderer was a partial duck-type.
    """

    text: str  # full text submitted, e.g. "/model gpt-4o" or "$review src/"
    args: str  # everything after the command name, e.g. "gpt-4o"
    model: str  # model label string
    console: "Console"  # Rich Console
    config: "AgenthiccConfig"  # live, mutable config
    session_id: str = ""
    active_agent: str = "default"

    skills: "dict[str, SkillDef]" = field(default_factory=dict)
    command_registry: "UnifiedCommandRegistry | None" = None
    mode_manager: "ModeManager | None" = None
    set_pending_skill: "Callable[[str], None] | None" = None
    set_pending_menu: "Callable[[Overlay], None] | None" = None
    close_overlay: "Callable[[], None] | None" = None
    set_pending_replay: "Callable[[str], None] | None" = None
    reload_skills: "Callable[[], SkillDiscoveryResult] | None" = None
    reload_commands: "Callable[[], tuple[bool, str]] | None" = None


# A handler takes a CommandContext and returns True if it handled the command.
CommandHandler = Callable[[CommandContext], bool]

# A menu factory takes a CommandContext and returns a MenuWidget.
MenuFactory = Callable[[CommandContext], "Overlay"]

# A completions factory takes the args fragment and returns matching completions.
CompletionsFactory = Callable[[str], list[str]]


@dataclass
class Command:
    """Complete specification for a command or explicit skill trigger."""

    name: str  # canonical name, e.g. "/config" or "$review-code"
    description: str  # shown in dropdown right column
    group: str = "Built-in"  # "Built-in" | "Skills" | "Plugins" | "MCP"
    aliases: tuple[str, ...] = field(default_factory=tuple)  # e.g. ("/cfg",) or ("$review",)
    argument_hint: str = ""  # e.g. "[section.key=value]"

    # Exactly one of handler / menu_factory should be set (both is also fine:
    # the menu factory takes precedence when the command is typed standalone).
    handler: CommandHandler | None = None
    menu_factory: MenuFactory | None = None

    # PRD-45: source namespacing — "builtin" | "skill:<slug>" | "plugin:<stem>" | "mcp:<alias>"
    source_id: str = "builtin"

    # PRD-45: optional sub-command completions factory
    completions_factory: CompletionsFactory | None = None

    @property
    def opens_menu(self) -> bool:
        return self.menu_factory is not None

    @property
    def is_skill(self) -> bool:
        """Whether this record belongs to the explicit skill namespace."""
        return self.group == "Skills" or self.source_id.startswith("skill:")

    def display_row(self) -> tuple[str, str, str]:
        """Return (name, argument_hint, description) for the /help table."""
        return self.name, self.argument_hint, self.description
