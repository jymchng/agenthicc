"""Command dataclass, CommandContext, and related type aliases (PRD-44, PRD-45)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.tui.menu import MenuWidget

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

    text: str               # full text the user submitted, e.g. "/model gpt-4o"
    args: str               # everything after the command name, e.g. "gpt-4o"
    model: Any              # model name string (or legacy TranscriptModel)
    console: Any            # Rich Console
    config: Any             # AgenthiccConfig (live, mutable)
    session_id: str = ""

    skills: dict = field(default_factory=dict)  # slug → SkillDef
    command_registry: Any = None                # UnifiedCommandRegistry
    mode_manager: Any = None                    # ModeManager
    set_pending_skill: Any = None               # callable(body: str) → None
    set_pending_menu: Any = None                # callable(Overlay) → None  (workspace.overlays.show)
    close_overlay: Any = None                   # callable() → None        (workspace.overlays.hide)
    set_pending_replay: Any = None              # callable(session_id: str) → None


# A handler takes a CommandContext and returns True if it handled the command.
CommandHandler = Callable[[CommandContext], bool]

# A menu factory takes a CommandContext and returns a MenuWidget.
MenuFactory = Callable[[CommandContext], "MenuWidget"]

# A completions factory takes the args fragment and returns matching completions.
CompletionsFactory = Callable[[str], list[str]]


@dataclass
class Command:
    """Complete specification for a single slash command."""

    name: str                                      # canonical name, e.g. "/config"
    description: str                               # shown in dropdown right column
    group: str = "Built-in"                        # "Built-in" | "Skills" | "Plugins" | "MCP"
    aliases: tuple[str, ...] = field(default_factory=tuple)  # e.g. ("/cfg",)
    argument_hint: str = ""                        # e.g. "[section.key=value]"

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

    def display_row(self) -> tuple[str, str, str]:
        """Return (name, argument_hint, description) for the /help table."""
        return self.name, self.argument_hint, self.description
