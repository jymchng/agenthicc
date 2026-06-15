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
    ctx.console.print("[dim]/expand: not available in this mode.[/dim]")
    return True


def _cmd_help(ctx: CommandContext) -> bool:
    try:
        from rich.table import Table   # noqa: PLC0415
        from rich import box as _rbox  # noqa: PLC0415
        registry = ctx.command_registry
        if registry is not None:
            for group in registry.groups():
                table = Table(title=group, box=_rbox.SIMPLE)
                table.add_column("Command", style="bold")
                table.add_column("Arguments", style="dim")
                table.add_column("Description")
                for cmd in registry.commands_for_group(group):
                    table.add_row(cmd.name, cmd.argument_hint or "", cmd.description)
                ctx.console.print(table)
        else:
            ctx.console.print("[dim]No command registry available.[/dim]")
    except ImportError:
        if ctx.command_registry:
            for cmd in ctx.command_registry.all_commands():
                ctx.console.print(f"  {cmd.name}  {cmd.description}")
    return True


def _cmd_history(ctx: CommandContext) -> bool:
    ctx.console.print("[dim]/history: not available in this mode.[/dim]")
    return True


def _cmd_mcp(ctx: CommandContext) -> bool:
    ctx.console.print("[dim]/mcp: no MCP servers configured or not available.[/dim]")
    return True


def _cmd_model(ctx: CommandContext) -> bool:
    try:
        from rich.table import Table   # noqa: PLC0415
        from rich.text import Text     # noqa: PLC0415
        from rich import box as _rbox  # noqa: PLC0415
        from agenthicc.config import (  # noqa: PLC0415
            SUPPORTED_PROVIDERS,
            PROVIDER_API_KEY_ENVVAR,
            PROVIDER_DEFAULT_MODELS,
        )
        import os  # noqa: PLC0415
    except ImportError:
        ctx.console.print(f"  Model: {ctx.model}")
        return True

    cfg = ctx.config
    parts = ctx.text.split()

    if len(parts) == 1 or parts[0] == "/models":
        current_provider = cfg.execution.provider
        current_model = cfg.execution.effective_model()
        table = Table(title="LLM Providers", box=_rbox.SIMPLE)
        table.add_column("Provider", style="cyan")
        table.add_column("Default Model")
        table.add_column("API Key Env")
        table.add_column("Status")
        for provider in SUPPORTED_PROVIDERS:
            env_var = PROVIDER_API_KEY_ENVVAR.get(provider, "—")
            key_set = "✓ set" if (provider == "ollama" or os.environ.get(env_var)) else "✗ not set"
            key_style = "green" if "✓" in key_set else "dim red"
            active = "◀ active" if provider == current_provider else ""
            table.add_row(
                f"[bold]{provider}[/bold]" if active else provider,
                PROVIDER_DEFAULT_MODELS.get(provider, "—"),
                env_var,
                Text(key_set, style=key_style),
            )
        ctx.console.print(table, markup=True)
        ctx.console.print(Text.assemble(
            ("Active: ", "dim"), (current_provider, "cyan bold"),
            (" / ", "dim"), (current_model, "bold"),
        ))
        ctx.console.print(Text(
            "  Set provider: /model <provider> [model]\n"
            "  Example:  /model anthropic claude-sonnet-4-6\n"
            "  Note: restart required for the change to take effect.",
            style="dim",
        ))
        return True

    provider = parts[1].lower()
    model_override = parts[2] if len(parts) > 2 else ""
    if provider not in SUPPORTED_PROVIDERS:
        ctx.console.print(Text(
            f"Unknown provider: {provider!r}\nSupported: {', '.join(SUPPORTED_PROVIDERS)}",
            style="red",
        ))
        return True
    env_var = PROVIDER_API_KEY_ENVVAR.get(provider)
    if provider != "ollama" and env_var and not os.environ.get(env_var):
        ctx.console.print(Text(
            f"Warning: {env_var} is not set — agent calls will fail.\n"
            f"  export {env_var}=\"your-api-key\"",
            style="yellow",
        ))
    effective_model = model_override or PROVIDER_DEFAULT_MODELS.get(provider, "")
    ctx.console.print(Text.assemble(
        ("Switched to ", "dim"), (provider, "cyan bold"), (" / ", "dim"), (effective_model, "bold"),
        ("\n  Add to .agenthicc/agenthicc.toml to persist:\n"
         f"  [execution]\n  provider = \"{provider}\"\n"
         f"  model = \"{effective_model}\"", "dim"),
    ))
    return True


def _cmd_skills(ctx: CommandContext) -> bool:
    try:
        from rich.table import Table  # noqa: PLC0415
        from rich import box as _rbox  # noqa: PLC0415
        table = Table(title="Available Skills", box=_rbox.SIMPLE)
        table.add_column("Command", style="bold cyan")
        table.add_column("Name")
        table.add_column("Description")
        if not ctx.skills:
            table.add_row("—", "(no skills found)", "")
        else:
            for slug, skill in sorted(ctx.skills.items()):
                table.add_row(f"/{slug}", skill.name,
                              (getattr(skill, "description", "") or "")[:80] or "—")
        ctx.console.print(table)
    except ImportError:
        for slug, skill in sorted(ctx.skills.items()):
            ctx.console.print(f"  /{slug}  {skill.name}")
    return True


def _cmd_status(ctx: CommandContext) -> bool:
    ctx.console.print(f"  [dim]Session:[/dim] {ctx.session_id or '(new)'}")
    ctx.console.print(f"  [dim]Model:[/dim] {ctx.model or '(unknown)'}")
    return True


def _cmd_commands(ctx: CommandContext) -> bool:
    registry = ctx.command_registry
    if registry is None:
        ctx.console.print("[dim]No command registry available.[/dim]")
        return True
    try:
        from rich.table import Table  # noqa: PLC0415
        from rich import box as _rbox  # noqa: PLC0415
        table = Table(title="Registered Commands", box=_rbox.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Group")
        table.add_column("Source", style="dim")
        table.add_column("Description")
        for cmd in registry.all_commands():
            table.add_row(cmd.name, cmd.group, cmd.source_id, cmd.description)
        ctx.console.print(table)
    except ImportError:
        for cmd in registry.all_commands():
            ctx.console.print(f"{cmd.name}  [{cmd.group}]  {cmd.description}")
    return True


def _menu_config(ctx: CommandContext) -> object:
    from agenthicc.tui.workspace.overlays.config_menu import ConfigMenuOverlay  # noqa: PLC0415
    on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
    return ConfigMenuOverlay(ctx.config, on_close)


def _cmd_mode(ctx: CommandContext) -> bool:
    mode_manager = ctx.mode_manager
    if mode_manager is None:
        ctx.console.print("[dim]Mode system not available.[/dim]")
        return True
    args = (ctx.args or "").strip()
    if not args:
        try:
            from rich.table import Table  # noqa: PLC0415
            from rich import box as _rbox  # noqa: PLC0415
            table = Table(title="Modes", box=_rbox.SIMPLE)
            table.add_column("Mode")
            table.add_column("Label")
            table.add_column("Description")
            modes = mode_manager._registry.all_modes()
            for m in modes:
                marker = " < active" if m.name == mode_manager.active_name else ""
                table.add_row(m.name, m.label, m.description + marker)
            ctx.console.print(table)
        except Exception:  # noqa: BLE001
            ctx.console.print(f"  Active mode: {mode_manager.active_name}")
        ctx.console.print("  [dim]Use Shift+Tab to cycle, or /mode <name>[/dim]")
    else:
        new_mode = mode_manager.set(args)
        if new_mode:
            ctx.console.print(f"  {new_mode.badge} [dim]Switched to {new_mode.name} mode.[/dim]")
        else:
            ctx.console.print(f"  [red]Unknown mode: {args!r}[/red]")
    return True


# ── PRD-45: skill handler factory ─────────────────────────────────────────────


def _make_skill_handler(slug: str, skill: object) -> object:
    """Return a CommandHandler that invokes a skill via the pending-skill mechanism."""
    from agenthicc.skills.runner import process_skill_body  # noqa: PLC0415

    def _handler(ctx: CommandContext) -> bool:
        import os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        args = ctx.args.split() if ctx.args.strip() else []
        body = process_skill_body(
            skill,
            args=args,
            cwd=Path(os.getcwd()),
            session_id=ctx.session_id,
        )
        framed = f"[Skill /{slug} — execute the following instructions:]\n\n{body}"
        if ctx.set_pending_skill is not None:
            ctx.set_pending_skill(framed)
        ctx.console.print(f"  [dim]Invoking skill [bold]/{slug}[/bold][/dim]")
        return True

    return _handler


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
