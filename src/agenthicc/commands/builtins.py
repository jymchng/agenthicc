"""Built-in slash commands (PRD-44, PRD-45)."""
from __future__ import annotations

from .command import Command, CommandContext
from .registry import UnifiedCommandRegistry

__all__ = ["BUILTIN_COMMANDS", "build_builtin_registry", "_make_skill_handler"]


# ── individual handlers ───────────────────────────────────────────────────────


def _cmd_cancel(ctx: CommandContext) -> bool:
    ctx.console.print("[dim]No active intent to cancel.[/dim]")
    return True


def _cmd_clear(ctx: CommandContext) -> bool:
    ctx.console.clear()
    return True


def _cmd_expand(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._expand(ctx.text, ctx.model, ctx.console)
    return True


def _cmd_help(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    # Pass the unified command registry through renderer if available so _help()
    # can render a grouped table from it.
    handler = _H(renderer=ctx.renderer)
    # Expose the unified registry via the renderer attribute _help() already reads.
    if ctx.renderer is not None and not hasattr(ctx.renderer, "_command_registry"):
        # Attach on the renderer so _help() can iterate grouped commands.
        _cmd_registry = getattr(ctx.renderer, "_cmd_registry", None)
        if _cmd_registry is not None:
            ctx.renderer._command_registry = _cmd_registry
    handler._help(ctx.console)
    return True


def _cmd_history(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._history(ctx.model, ctx.console)
    return True


def _cmd_mcp(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    handler = _H(renderer=ctx.renderer)
    if hasattr(handler, "_mcp"):
        handler._mcp(ctx.text, ctx.console)
    else:
        ctx.console.print("[dim]MCP: no servers configured.[/dim]")
    return True


def _cmd_model(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._model(ctx.text, ctx.console)
    return True


def _cmd_skills(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._list_skills(ctx.console)
    return True


def _cmd_status(ctx: CommandContext) -> bool:
    from agenthicc.tui.app import SlashCommandHandler as _H  # noqa: PLC0415
    _H(renderer=ctx.renderer)._status(ctx.model, ctx.console)
    return True


def _cmd_commands(ctx: CommandContext) -> bool:
    """List all registered commands with their source (PRD-45 debug command)."""
    try:
        from rich.table import Table  # noqa: PLC0415
        from rich import box as rich_box  # noqa: PLC0415
        _RICH_AVAILABLE = True
    except ImportError:
        _RICH_AVAILABLE = False

    registry = getattr(ctx.renderer, "_cmd_registry", None) if ctx.renderer else None
    if registry is None:
        ctx.console.print("[dim]No command registry available.[/dim]")
        return True

    if _RICH_AVAILABLE:
        table = Table(title="Registered Commands", box=rich_box.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Group")
        table.add_column("Source", style="dim")
        table.add_column("Description")
        for cmd in registry.all_commands():
            table.add_row(cmd.name, cmd.group, cmd.source_id, cmd.description)
        ctx.console.print(table)
    else:
        for cmd in registry.all_commands():
            ctx.console.print(f"{cmd.name}  [{cmd.group}]  ({cmd.source_id})  {cmd.description}")
    return True


def _menu_config(ctx: CommandContext) -> object:
    from agenthicc.tui.widgets.config_menu import ConfigurationMenu  # noqa: PLC0415
    return ConfigurationMenu(ctx.config, ctx.console)


# ── PRD-45: skill handler factory ─────────────────────────────────────────────


def _make_skill_handler(slug: str, skill: object, renderer: object) -> object:
    """Return a CommandHandler that invokes a skill via the pending-skill mechanism."""
    from agenthicc.skills.runner import process_skill_body  # noqa: PLC0415

    def _handler(ctx: CommandContext) -> bool:
        import os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        args = ctx.args.split() if ctx.args.strip() else []
        session_id = getattr(getattr(renderer, "_status", None), "resume_id", "") or ""
        body = process_skill_body(
            skill,
            args=args,
            cwd=Path(os.getcwd()),
            session_id=session_id,
        )
        renderer._pending_skill = body  # type: ignore[attr-defined]
        ctx.console.print(f"  [dim]Invoking skill [bold]/{slug}[/bold][/dim]")
        return True

    return _handler


def _cmd_mode(ctx: CommandContext) -> bool:
    mode_manager = getattr(ctx.renderer, "_mode_manager", None)
    mode_registry = getattr(ctx.renderer, "_mode_registry", None)
    if mode_manager is None:
        ctx.console.print("[dim]Mode system not available.[/dim]")
        return True
    args = (ctx.args or "").strip()
    if not args:
        from rich.table import Table  # noqa: PLC0415
        from rich import box as _rbox  # noqa: PLC0415
        table = Table(title="Modes", box=_rbox.SIMPLE)
        table.add_column("Mode")
        table.add_column("Label")
        table.add_column("Description")
        modes = mode_registry.all_modes() if mode_registry else mode_manager._registry.all_modes()
        for m in modes:
            marker = " < active" if m.name == mode_manager.active_name else ""
            table.add_row(m.name, m.label, m.description + marker)
        ctx.console.print(table)
        ctx.console.print("  [dim]Use Shift+Tab to cycle, or /mode <name>[/dim]")
    else:
        new_mode = mode_manager.set(args)
        if new_mode:
            ctx.console.print(f"  {new_mode.badge} [dim]Switched to {new_mode.name} mode.[/dim]")
        else:
            ctx.console.print(f"  [red]Unknown mode: {args!r}[/red]")
    return True


# ── built-in command list ─────────────────────────────────────────────────────

BUILTIN_COMMANDS: list[Command] = [
    Command(
        name="/cancel",
        description="Cancel the currently running intent",
        handler=_cmd_cancel,
    ),
    Command(
        name="/clear",
        description="Clear the transcript display",
        handler=_cmd_clear,
    ),
    Command(
        name="/commands",
        description="List all registered commands with their source",
        group="Built-in",
        handler=_cmd_commands,
    ),
    Command(
        name="/config",
        description="Open configuration editor",
        group="Built-in",
        menu_factory=_menu_config,
    ),
    Command(
        name="/expand",
        description="Expand tool output or @mention",
        argument_hint="[tool-id-or-@path]",
        handler=_cmd_expand,
    ),
    Command(
        name="/help",
        description="List available commands",
        handler=_cmd_help,
    ),
    Command(
        name="/history",
        description="Browse the event log",
        handler=_cmd_history,
    ),
    Command(
        name="/mcp",
        description="Show MCP server status",
        group="MCP",
        argument_hint="[connect <url> [transport]]",
        handler=_cmd_mcp,
    ),
    Command(
        name="/model",
        description="Show or switch LLM provider/model",
        argument_hint="[provider] [model]",
        handler=_cmd_model,
    ),
    Command(
        name="/models",
        description="List all available LLM providers",
        handler=_cmd_model,
    ),
    Command(
        name="/skills",
        description="List available skills",
        handler=_cmd_skills,
    ),
    Command(
        name="/status",
        description="Show running agents and their tasks",
        handler=_cmd_status,
    ),
    Command(
        name="/mode",
        description="Show or switch operational mode",
        argument_hint="[Auto|Plan|Ask|Review|Safe|Debug]",
        group="Built-in",
        handler=_cmd_mode,
    ),
]


def build_builtin_registry() -> UnifiedCommandRegistry:
    """Return a UnifiedCommandRegistry pre-loaded with all built-in commands."""
    reg = UnifiedCommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
    return reg
