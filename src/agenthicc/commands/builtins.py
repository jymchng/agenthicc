"""Built-in slash commands (PRD-44, PRD-45)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .command import BusyPolicy, Command, CommandContext, CommandHandler, UsageSnapshot
from .registry import UnifiedCommandRegistry

if TYPE_CHECKING:
    from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
    from agenthicc.tui.workspace.overlay import Overlay

__all__ = ["BUILTIN_COMMANDS", "build_builtin_registry", "_make_skill_handler"]


# ── individual handlers ───────────────────────────────────────────────────────


def _cmd_replay(ctx: CommandContext) -> bool:
    """Replay a previous session's conversation in the scroll buffer."""
    from agenthicc.tui.runtime.session_log import (  # noqa: PLC0415
        get_session_log_path,
        find_latest_session_for_cwd,
    )

    session_id: str = ctx.args.strip()
    if not session_id:
        # No ID provided — use the most recent session for the current directory,
        # excluding the current session itself.
        session_id = find_latest_session_for_cwd() or ""
        if not session_id or session_id == ctx.session_id:
            ctx.console.print(
                "[red]Usage:[/red] /replay [dim]<session-id>[/dim]  "
                "— no previous session found for this directory."
            )
            return True

    path = get_session_log_path(session_id)
    if not path.exists():
        ctx.console.print(
            f"[red]Session [bold]{session_id[:16]}[/bold] not found.[/red]\n"
            "[dim]Check ~/.agenthicc/sessions/ for available session IDs.[/dim]"
        )
        return True

    if ctx.set_pending_replay is not None:
        ctx.set_pending_replay(session_id)
    return True


def _cmd_cancel(ctx: CommandContext) -> bool:
    if ctx.cancel_active is not None and ctx.cancel_active():
        ctx.console.print("[yellow]Cancellation requested for the active run.[/yellow]")
    else:
        ctx.console.print("[dim]No active intent to cancel.[/dim]")
    return True


def _cmd_usage(ctx: CommandContext) -> bool:
    """Show the local usage snapshot without contacting the provider."""
    provider = ctx.usage_snapshot
    snapshot = provider() if provider is not None else None
    if not isinstance(snapshot, UsageSnapshot):
        ctx.console.print("Usage is unavailable in this command context.", markup=False)
        return True
    state = "running" if snapshot.active_run else "idle"
    ctx.console.print(
        "Usage: "
        f"input={snapshot.input_tokens:,} "
        f"output={snapshot.output_tokens:,} "
        f"total={snapshot.total_tokens:,} "
        f"cost=${snapshot.cost_usd:.4f} "
        f"state={state} "
        f"queued={snapshot.queue_depth}",
        markup=False,
    )
    return True


def _cmd_clear(ctx: CommandContext) -> bool:
    ctx.console.clear()
    return True


def _cmd_expand(ctx: CommandContext) -> bool:
    ctx.console.print("[dim]/expand: not available in this mode.[/dim]")
    return True


def _help_menu(ctx: CommandContext) -> "Overlay":
    from agenthicc.tui.workspace.overlays.help import HelpOverlay  # noqa: PLC0415

    on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
    registry = ctx.command_registry or UnifiedCommandRegistry()
    return HelpOverlay(registry, on_close, initial_query=ctx.args)


def _cmd_history(ctx: CommandContext) -> bool:
    ctx.console.print("[dim]/history: not available in this mode.[/dim]")
    return True


def _cmd_init(ctx: CommandContext) -> bool:
    """Preview or explicitly write project guidance in ``AGENTS.md``."""
    from pathlib import Path  # noqa: PLC0415

    from agenthicc.project_bootstrap import (  # noqa: PLC0415
        BootstrapError,
        BootstrapWriteError,
        build_bootstrap_plan,
        write_bootstrap_plan,
    )

    tokens = set((ctx.args or "").split())
    write_requested = "write" in tokens or "--write" in tokens
    force = "force" in tokens or "--force" in tokens
    try:
        plan = build_bootstrap_plan(Path.cwd())
    except BootstrapError as exc:
        ctx.console.print(f"error: {exc}", markup=False)
        return True

    preview = plan.preview()
    if preview:
        ctx.console.print(
            preview,
            markup=False,
            end="" if preview.endswith("\n") else "\n",
        )
    if not plan.changed:
        return True
    if not write_requested:
        ctx.console.print(
            "Preview only. Review the diff, then run /init write to create AGENTS.md.",
            markup=False,
        )
        return True
    if plan.exists and not force:
        ctx.console.print(
            "Refusing to overwrite existing AGENTS.md. Review the diff, then run "
            "/init write --force.",
            markup=False,
        )
        return True
    try:
        target = write_bootstrap_plan(plan, force=force)
    except BootstrapWriteError as exc:
        ctx.console.print(f"error: {exc}", markup=False)
        return True
    ctx.console.print(f"Updated {target}", markup=False)
    return True


def _cmd_mcp(ctx: CommandContext) -> bool:
    ctx.console.print("[dim]/mcp: no MCP servers configured or not available.[/dim]")
    return True


def _read_only_without_args(args: str) -> BusyPolicy:
    """Make an argument-free query immediate while actions remain queued."""
    return BusyPolicy.IMMEDIATE_READ_ONLY if not args.strip() else BusyPolicy.QUEUE


def _mcp_busy_policy(args: str) -> BusyPolicy:
    """Allow only local MCP status inspection in the immediate lane."""
    return (
        BusyPolicy.IMMEDIATE_READ_ONLY
        if args.strip().lower() in {"", "status"}
        else BusyPolicy.QUEUE
    )


def _reloadable_list_policy(args: str) -> BusyPolicy:
    """List commands are safe; reload and malformed action forms defer."""
    return BusyPolicy.IMMEDIATE_READ_ONLY if not args.strip() else BusyPolicy.QUEUE


def _cmd_model(ctx: CommandContext) -> bool:
    try:
        from rich.table import Table  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415
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
            display_env_var = PROVIDER_API_KEY_ENVVAR.get(provider, "—")
            key_set = (
                "✓ set"
                if (provider == "ollama" or os.environ.get(display_env_var))
                else "✗ not set"
            )
            key_style = "green" if "✓" in key_set else "dim red"
            active = "◀ active" if provider == current_provider else ""
            table.add_row(
                f"[bold]{provider}[/bold]" if active else provider,
                PROVIDER_DEFAULT_MODELS.get(provider, "—"),
                display_env_var,
                Text(key_set, style=key_style),
            )
        ctx.console.print(table, markup=True)
        ctx.console.print(
            Text.assemble(
                ("Active: ", "dim"),
                (current_provider, "cyan bold"),
                (" / ", "dim"),
                (current_model, "bold"),
            )
        )
        ctx.console.print(
            Text(
                "  Set provider: /model <provider> [model]\n"
                "  Example:  /model anthropic claude-sonnet-4-6\n"
                "  Note: restart required for the change to take effect.",
                style="dim",
            )
        )
        return True

    provider = parts[1].lower()
    model_override = parts[2] if len(parts) > 2 else ""
    if provider not in SUPPORTED_PROVIDERS:
        ctx.console.print(
            Text(
                f"Unknown provider: {provider!r}\nSupported: {', '.join(SUPPORTED_PROVIDERS)}",
                style="red",
            )
        )
        return True
    env_var: str | None = PROVIDER_API_KEY_ENVVAR.get(provider)
    if provider != "ollama" and env_var and not os.environ.get(env_var):
        ctx.console.print(
            Text(
                f"Warning: {env_var} is not set — agent calls will fail.\n"
                f'  export {env_var}="your-api-key"',
                style="yellow",
            )
        )
    effective_model = model_override or PROVIDER_DEFAULT_MODELS.get(provider, "")
    ctx.console.print(
        Text.assemble(
            ("Switched to ", "dim"),
            (provider, "cyan bold"),
            (" / ", "dim"),
            (effective_model, "bold"),
            (
                "\n  Add to .agenthicc/agenthicc.toml to persist:\n"
                f'  [execution]\n  provider = "{provider}"\n'
                f'  model = "{effective_model}"',
                "dim",
            ),
        )
    )
    return True


def _cmd_skills(ctx: CommandContext) -> bool:
    requested = ctx.args.strip().lower()
    if requested:
        if requested != "reload":
            ctx.console.print("Usage: /skills [reload]", markup=False)
            return True
        if ctx.reload_skills is None:
            ctx.console.print(
                "Skill reload is only available in an interactive session.",
                markup=False,
            )
            return True

        before = set(ctx.skills)
        try:
            discovery: SkillDiscoveryResult = ctx.reload_skills()
        except Exception as exc:  # noqa: BLE001
            message = str(exc).strip() or type(exc).__name__
            ctx.console.print(f"Skill reload failed: {message}", markup=False)
            return True

        after = set(ctx.skills)
        added = sorted(after - before)
        removed = sorted(before - after)
        changes: list[str] = []
        if added:
            changes.append(f"added: {', '.join(added)}")
        if removed:
            changes.append(f"removed: {', '.join(removed)}")
        suffix = f" ({'; '.join(changes)})" if changes else ""
        ctx.console.print(
            f"Reloaded {len(after)} skill(s){suffix}.",
            markup=False,
        )
        for diagnostic in discovery.diagnostics:
            if diagnostic.severity != "info":
                ctx.console.print(f"Skill reload: {diagnostic}", markup=False)
        return True

    visible_skills = {
        slug: skill for slug, skill in ctx.skills.items() if _skill_allowed_for_context(ctx, skill)
    }
    try:
        from rich.table import Table  # noqa: PLC0415
        from rich import box as _rbox  # noqa: PLC0415

        table = Table(title="Available Skills", box=_rbox.SIMPLE)
        table.add_column("Command", style="bold cyan")
        table.add_column("Name")
        table.add_column("Description")
        if not visible_skills:
            table.add_row("—", "(no skills found)", "")
        else:
            for slug, skill in sorted(visible_skills.items()):
                command_names = ", ".join(f"${name}" for name in skill.command_names)
                table.add_row(
                    command_names,
                    skill.name,
                    (getattr(skill, "description", "") or "")[:80] or "—",
                )
        ctx.console.print(table)
    except ImportError:
        for slug, skill in sorted(visible_skills.items()):
            command_names = ", ".join(f"${name}" for name in skill.command_names)
            ctx.console.print(f"  {command_names}  {skill.name}")
    return True


def _cmd_status(ctx: CommandContext) -> bool:
    ctx.console.print(f"  [dim]Session:[/dim] {ctx.session_id or '(new)'}")
    ctx.console.print(f"  [dim]Model:[/dim] {ctx.model or '(unknown)'}")
    return True


def _cmd_commands(ctx: CommandContext) -> bool:
    requested = ctx.args.strip()
    if requested:
        if requested != "reload":
            ctx.console.print("Usage: /commands [reload]", markup=False)
            return True
        if ctx.reload_commands is None:
            ctx.console.print(
                "Command reload is only available in an interactive session.",
                markup=False,
            )
            return True
        try:
            _ok, message = ctx.reload_commands()
        except Exception as exc:  # noqa: BLE001
            message = f"Command reload failed; existing commands kept: {type(exc).__name__}: {exc}"
        ctx.console.print(message, markup=False)
        return True

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


def _menu_config(ctx: CommandContext) -> "Overlay":
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
            try:
                modes = mode_manager._registry.all()
                for m in modes:
                    marker = " < active" if m.name == mode_manager.active_name else ""
                    table.add_row(m.name, m.badge, m.description + marker)
            except AttributeError:
                legacy_modes = mode_manager._registry.all_modes()  # type: ignore[attr-defined]
                for m in legacy_modes:
                    marker = " < active" if m.name == mode_manager.active_name else ""
                    table.add_row(m.name, m.label, m.description + marker)
            ctx.console.print(table)
        except Exception:  # noqa: BLE001
            ctx.console.print(f"  Active mode: {mode_manager.active_name}")
        ctx.console.print("  [dim]Use Shift+Tab to cycle, or /mode <name>[/dim]")
    else:
        try:
            new_mode = mode_manager.set_by_name(args)
        except AttributeError:
            new_mode = mode_manager.set(args)  # type: ignore[attr-defined]
        if new_mode:
            ctx.console.print(f"  {new_mode.badge} [dim]Switched to {new_mode.name} mode.[/dim]")
        else:
            ctx.console.print(f"  [red]Unknown mode: {args!r}[/red]")
    return True


# ── PRD-45: skill handler factory ─────────────────────────────────────────────


def _skill_allowed_for_context(ctx: CommandContext, skill: "SkillDef") -> bool:
    """Apply frontmatter and configured per-agent skill permissions."""

    from agenthicc.skills.loader import SkillPermissionSet  # noqa: PLC0415

    permissions = None
    agents = getattr(ctx.config, "agents", None)
    resolver = getattr(agents, "skill_permissions_for", None)
    if callable(resolver):
        candidate = resolver(ctx.active_agent)
        if isinstance(candidate, SkillPermissionSet):
            permissions = candidate
    return skill.is_allowed_for(ctx.active_agent, permissions)


def _make_skill_handler(slug: str, skill: "SkillDef") -> CommandHandler:
    """Return a CommandHandler that invokes a skill via the pending-skill mechanism."""
    from agenthicc.skills.runner import process_skill_body  # noqa: PLC0415

    def _handler(ctx: CommandContext) -> bool:
        import os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        if not _skill_allowed_for_context(ctx, skill):
            ctx.console.print(
                f"Skill ${slug} is not permitted for agent {ctx.active_agent!r}.",
                markup=False,
            )
            return True

        args = ctx.args.split() if ctx.args.strip() else []
        body = process_skill_body(
            skill,
            args=args,
            cwd=Path(os.getcwd()),
            session_id=ctx.session_id,
        )
        framed = f"[Skill ${slug} — execute the following instructions:]\n\n{body}"
        if ctx.set_pending_skill is not None:
            ctx.set_pending_skill(framed)
        ctx.console.print(f"  [dim]Invoking skill [bold]${slug}[/bold][/dim]")
        return True

    return _handler


# ── built-in command list ─────────────────────────────────────────────────────

BUILTIN_COMMANDS: list[Command] = [
    Command(
        name="/replay",
        description="Replay a previous session's conversation in the scroll buffer",
        argument_hint="[session-id]",
        group="Built-in",
        handler=_cmd_replay,
    ),
    Command(
        name="/cancel",
        description="Cancel the currently running intent",
        aliases=("/interrupt",),
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_cancel,
    ),
    Command(
        name="/clear",
        description="Clear the transcript display",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_clear,
    ),
    Command(
        name="/commands",
        description="List all registered commands with their source",
        argument_hint="[reload]",
        group="Built-in",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_reloadable_list_policy,
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
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_expand,
    ),
    Command(
        name="/help",
        description="List available commands",
        argument_hint="[/command]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        menu_factory=_help_menu,
    ),
    Command(
        name="/history",
        description="Browse the event log",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_history,
    ),
    Command(
        name="/init",
        description="Inspect the project and preview AGENTS.md guidance",
        argument_hint="[write] [--force]",
        group="Built-in",
        handler=_cmd_init,
    ),
    Command(
        name="/mcp",
        description="Show MCP server status",
        group="MCP",
        argument_hint="[connect <url> [transport]]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_mcp_busy_policy,
        handler=_cmd_mcp,
    ),
    Command(
        name="/model",
        description="Show or switch LLM provider/model",
        argument_hint="[provider] [model]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_read_only_without_args,
        handler=_cmd_model,
    ),
    Command(
        name="/models",
        description="List all available LLM providers",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_model,
    ),
    Command(
        name="/skills",
        description="List or reload available skills",
        argument_hint="[reload]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_reloadable_list_policy,
        handler=_cmd_skills,
    ),
    Command(
        name="/status",
        description="Show running agents and their tasks",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_status,
    ),
    Command(
        name="/usage",
        description="Show local token, cost, and active-run usage",
        group="Built-in",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_usage,
    ),
    Command(
        name="/mode",
        description="Show or switch operational mode",
        argument_hint="[Auto|Plan|Ask|Review|Safe|Debug]",
        group="Built-in",
        handler=_cmd_mode,
    ),
    Command(
        name="/workflow",
        description="Switch the active workflow within the current mode",
        argument_hint="<name> | reset",
        group="Built-in",
        # No handler: /workflow is intercepted in TUISession.route() before
        # dispatch_slash() so it can access session-local state.  The entry
        # exists here solely so the trigger picker displays and completes it.
        handler=None,
    ),
    Command(
        name="/compact",
        description="Summarise conversation history to free context-window space",
        group="Built-in",
        # No handler: /compact is intercepted in TUISession.route() before
        # dispatch_slash() so it can access session memory directly.
        handler=None,
    ),
]


def build_builtin_registry() -> UnifiedCommandRegistry:
    """Return a UnifiedCommandRegistry pre-loaded with all built-in commands."""
    reg = UnifiedCommandRegistry()
    reg.register_many(BUILTIN_COMMANDS)
    return reg
