"""Built-in slash commands (PRD-44, PRD-45)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .command import BusyPolicy, Command, CommandContext, CommandHandler, UsageSnapshot
from .registry import UnifiedCommandRegistry

if TYPE_CHECKING:
    from agenthicc.runners.model_switch import ModelChoice
    from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
    from agenthicc.tui.workspace.dialog import DialogSettings
    from agenthicc.tui.workspace.overlay import Overlay
    from agenthicc.tui.workspace.picker import PickerRow

__all__ = ["BUILTIN_COMMANDS", "build_builtin_registry", "_make_skill_handler"]


# ── individual handlers ───────────────────────────────────────────────────────


def _cmd_replay(ctx: CommandContext) -> bool:
    """Replay a previous session's conversation in the scroll buffer."""
    from agenthicc.tui.runtime.session_log import (
        get_session_log_path,
        find_latest_session_for_cwd,
    )

    session_id: str = ctx.args.strip()
    if not session_id:
        # No ID provided — use the most recent session for the current directory,
        # excluding the current session itself.
        session_id = find_latest_session_for_cwd() or ""
        if not session_id or session_id == ctx.session_id:
            ctx.notify(
                "error",
                "No previous session found for this directory.",
                body="Usage: /replay <session-id>",
            )
            return True

    path = get_session_log_path(session_id)
    if not path.exists():
        ctx.notify(
            "error",
            f"Session {session_id[:16]} not found.",
            body="Check ~/.agenthicc/sessions/ for available session IDs.",
        )
        return True

    if ctx.set_pending_replay is not None:
        ctx.set_pending_replay(session_id)
    return True


def _cmd_cancel(ctx: CommandContext) -> bool:
    if ctx.cancel_active is not None and ctx.cancel_active():
        ctx.notify("info", "Cancellation requested for the active run.")
    else:
        ctx.notify("info", "No active intent to cancel.")
    return True


def _loop_busy_policy(args: str) -> BusyPolicy:
    """Keep loop controls responsive while creation waits for a safe turn."""

    action = (args.strip().split(None, 1)[0] if args.strip() else "").lower()
    if action in {"status", "stop", "pause", "resume"}:
        return BusyPolicy.IMMEDIATE_CONTROL
    return BusyPolicy.QUEUE


def _cmd_loop(ctx: CommandContext) -> bool:
    """Create or control the session-scoped recurring prompt loop."""

    manager = ctx.loop_manager
    if manager is None:
        ctx.notify("warning", "Loop scheduling is unavailable in this session.")
        return True
    ctx.console.print(manager.handle_command(ctx.args), markup=False)
    return True


def _cmd_loops(ctx: CommandContext) -> bool:
    """Open the table of every persisted loop job."""

    manager = ctx.loop_manager
    if manager is None:
        ctx.notify("warning", "Loop scheduling is unavailable in this session.")
        return True
    if ctx.args.strip():
        ctx.notify("error", "/loops takes no arguments.", body="Usage: /loops")
        return True
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.loops import LoopJobsOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(
            LoopJobsOverlay(
                manager.list_jobs,
                on_close,
                manager.run_job_now,
                manager.delete_job,
                session_id=manager.session_id,
            )
        )
        return True

    # Command-dispatch tests and non-interactive embedders may not provide an
    # overlay host. Keep the same data visible as a bounded Rich table.
    try:
        from rich import box as _rbox
        from rich.table import Table

        table = Table(title="Scheduled Loops", box=_rbox.SIMPLE)
        table.add_column("Job ID", no_wrap=True)
        table.add_column("State", no_wrap=True)
        table.add_column("Session", no_wrap=True)
        table.add_column("Payload")
        from agenthicc.runners.loop_scheduler import _redacted_preview

        records = manager.list_jobs()
        for record in records:
            table.add_row(
                record.loop_id[:12],
                record.state.value,
                "current" if record.session_id == manager.session_id else record.session_id[:12],
                _redacted_preview(record.payload, limit=100),
            )
        if not records:
            table.add_row("—", "—", "—", "No scheduled loop jobs")
        ctx.console.print(table)
    except ImportError:
        ctx.console.print("No scheduled loop jobs.", markup=False)
    return True


def _cmd_schedules(ctx: CommandContext) -> bool:
    """Open the repository schedule manager."""

    if ctx.args.strip():
        ctx.notify("error", "/schedules takes no arguments.", body="Usage: /schedules")
        return True
    manager = ctx.schedule_manager
    if manager is None:
        ctx.notify("warning", "Repository schedule management is unavailable.")
        return True
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.schedules import (
            ScheduleJobsOverlay,
        )

        ctx.set_pending_menu(
            ScheduleJobsOverlay(
                manager.store,
                manager,
                ctx.schedule_repository_root or ".",
                ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
                ctx.set_input_text if ctx.set_input_text is not None else (lambda _text: None),
            )
        )
        return True
    records = manager.store.list(repository_root=ctx.schedule_repository_root or None)
    if not records:
        ctx.console.print("No repository schedules.", markup=False)
    else:
        for record in records:
            next_run = record.next_run_at
            ctx.console.print(
                f"{record.schedule_id[:14]}  {'enabled' if record.enabled else 'paused'}  "
                f"{record.workflow_name}  next={next_run or '—'}",
                markup=False,
            )
    return True


def _cmd_schedule(ctx: CommandContext) -> bool:
    """Create or control one repository-scoped schedule."""

    import shlex as _shlex

    from agenthicc.schedules import (
        CoalescePolicy,
        format_timestamp,
        parse_recurrence,
    )
    from agenthicc.tui.runtime.mode_manager import canonical_mode_name

    manager = ctx.schedule_manager
    if manager is None:
        ctx.notify("warning", "Repository schedule management is unavailable.")
        return True
    _, separator, raw_args = ctx.text.partition(" ")
    if not separator:
        raw_args = ""
    action, _, tail = raw_args.strip().partition(" ")
    action = action.casefold()
    try:
        if action == "create":
            left, separator, prompt = tail.partition(" -- ")
            if not separator or not prompt.strip():
                raise ValueError(
                    "usage: /schedule create WORKFLOW PROFILE MODE RECURRENCE TIMEZONE -- PROMPT"
                )
            tokens = _shlex.split(left)
            if len(tokens) != 5:
                raise ValueError(
                    "usage: /schedule create WORKFLOW PROFILE MODE RECURRENCE TIMEZONE -- PROMPT"
                )
            workflow_name, profile_name, raw_mode, recurrence, timezone = tokens
            if ctx.workflow_registry is None or ctx.workflow_registry.get(workflow_name) is None:
                raise ValueError(f"workflow {workflow_name!r} is not registered")
            if profile_name not in ctx.config.providers:
                raise ValueError(f"provider profile {profile_name!r} is not configured")
            mode = canonical_mode_name(raw_mode)
            if mode not in {"Safe", "Plan", "Yolo"}:
                raise ValueError("permission ceiling must be Safe, Plan, or Yolo")
            kind, spec = parse_recurrence(recurrence)
            schedule = manager.create(
                repository_root=ctx.schedule_repository_root or ".",
                workflow_name=workflow_name,
                prompt=prompt,
                profile_name=profile_name,
                permission_ceiling=mode,
                recurrence_kind=kind,
                recurrence_spec=spec,
                timezone=timezone,
                coalesce_policy=CoalescePolicy.SKIP,
                config_path=ctx.schedule_config_path,
            )
            ctx.console.print(
                f"Created disabled schedule {schedule.schedule_id}. Next run preview: "
                f"{format_timestamp(schedule.next_run_at, schedule.timezone)} [{schedule.timezone}].",
                markup=False,
            )
            return True

        tokens = _shlex.split(tail)
        if action == "run-now" and len(tokens) == 1:
            occurrence = manager.run_now(tokens[0], actor="tui")
            ctx.console.print(
                f"Run accepted · occurrence {occurrence.occurrence_id} · session {occurrence.session_id}.",
                markup=False,
            )
        elif action == "pause" and len(tokens) == 1:
            schedule = manager.pause(tokens[0], actor="tui")
            ctx.console.print(f"Schedule paused at revision {schedule.revision}.", markup=False)
        elif action == "resume" and len(tokens) == 2:
            schedule = manager.resume(tokens[0], confirm_next_run=tokens[1], actor="tui")
            ctx.console.print(
                f"Schedule resumed; next run {format_timestamp(schedule.next_run_at, schedule.timezone)}.",
                markup=False,
            )
        elif action == "delete" and len(tokens) == 2 and tokens[1] == "--confirm":
            schedule = manager.delete(tokens[0], actor="tui")
            ctx.console.print(
                f"Schedule deleted at revision {schedule.revision}; prior runs are preserved.",
                markup=False,
            )
        else:
            raise ValueError(
                "usage: /schedule create ... | run-now ID | pause ID | "
                "resume ID NEXT-RUN | delete ID --confirm"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        ctx.notify("error", f"/schedule: {exc}")
    return True


def _cmd_agents(ctx: CommandContext) -> bool:
    """Open the agents hub over the conversation (AGX-05)."""

    from agenthicc.tui.workspace.agents_hub.model import Tab

    tab = ctx.args.strip()
    names = [item.value for item in Tab]
    if tab and tab not in names:
        ctx.notify("error", f"/agents: unknown tab {tab!r}.", body=f"Use {', '.join(names)}.")
        return True
    if ctx.open_agents_hub is None:
        ctx.notify("warning", "The agents hub needs the interactive TUI: run agenthicc agents.")
        return True
    ctx.open_agents_hub(tab)
    return True


def _cmd_resume(ctx: CommandContext) -> bool:
    """Open the agents hub on its History tab, where Enter resumes a saved session (TUX-03)."""

    if ctx.open_agents_hub is None:
        ctx.notify(
            "warning", "/resume needs the interactive TUI: run agenthicc agents --tab history."
        )
        return True
    ctx.open_agents_hub("history")
    return True


def _dialog_settings(ctx: CommandContext) -> DialogSettings:
    """The session's keymap, screen-reader setting and mode for a dialog (TUX-07)."""
    from agenthicc.tui.preferences import ui_settings
    from agenthicc.tui.workspace.dialog import DEFAULT_DIALOG_SETTINGS, DialogSettings

    app_state = ctx.app_state
    if app_state is None:
        return DEFAULT_DIALOG_SETTINGS
    ui = ui_settings(app_state)
    return DialogSettings(
        keymap=app_state.keymap(),
        plain=ui is not None and ui.screen_reader,
        mode=app_state.active_mode().name,
    )


def _release_notes_menu(ctx: CommandContext) -> Overlay:
    """The packaged changelog's entries since the last version whose notes were viewed (TUX-03)."""
    from dataclasses import replace

    from agenthicc.cli.version import package_version
    from agenthicc.release_notes import load_entries, select
    from agenthicc.tui.workspace.overlays.release_notes import ReleaseNotesOverlay
    from agenthicc.user_state import load_user_state, save_user_state

    installed = package_version()
    state = load_user_state()
    entries = select(load_entries(), installed=installed, last_seen=state.release_notes_seen)
    save_user_state(replace(state, release_notes_seen=installed))
    return ReleaseNotesOverlay(
        entries,
        version=installed,
        on_close=ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
        settings=_dialog_settings(ctx),
    )


def _cmd_dnd(ctx: CommandContext) -> bool:
    """Toggle attention notifications without hiding the status badge (AGX-10)."""

    if ctx.args.strip():
        ctx.notify("error", "/dnd takes no arguments; run it again to toggle.")
        return True
    if ctx.toggle_dnd is None:
        ctx.notify("warning", "/dnd needs the interactive TUI.")
        return True
    enabled = ctx.toggle_dnd()
    label = "on" if enabled else "off"
    ctx.notify("success", f"Do not disturb {label}")
    return True


def _cmd_agent(ctx: CommandContext) -> bool:
    """Choose the primary agent: ``/agent`` opens the picker, ``/agent NAME`` switches."""

    name = ctx.args.strip()
    if ctx.select_agent is None or ctx.open_agent_picker is None:
        ctx.console.print(
            "[dim]/agent needs the interactive TUI; use agenthicc --agent NAME.[/dim]"
        )
        return True
    if name:
        ctx.console.print(ctx.select_agent(name), markup=False)
    else:
        ctx.open_agent_picker()
    return True


def _cmd_theme(ctx: CommandContext) -> bool:
    """``/theme`` opens the picker; ``/theme NAME [--save]`` switches (TUX-01)."""
    from pathlib import Path

    from agenthicc.tui.preferences import save_ui_preference
    from agenthicc.tui.theme import (
        Theme,
        ThemeError,
        available_themes,
        current_theme,
        load_theme,
        use_theme,
    )

    cwd = Path.cwd()

    def apply(theme: Theme) -> None:
        use_theme(theme.with_glyphs(current_theme().glyphs))

    def save(name: str) -> str:
        try:
            path = save_ui_preference("theme", name)
        except OSError as exc:
            return f"Could not save the theme: {exc}"
        return f"Saved theme = {name!r} to {path}"

    parts = ctx.args.split()
    if not parts:
        if ctx.set_pending_menu is None:
            ctx.console.print("Themes: " + ", ".join(available_themes(cwd)), markup=False)
            return True
        from agenthicc.tui.workspace.overlays.theme_picker import ThemePickerOverlay

        ctx.set_pending_menu(
            ThemePickerOverlay(
                available_themes(cwd),
                current=current_theme().name,
                load=lambda name: load_theme(name, cwd),
                apply=apply,
                save=save,
                close=ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
            )
        )
        return True
    name = parts[0]
    try:
        theme = load_theme(name, cwd)
    except (ThemeError, OSError) as exc:
        ctx.notify("error", str(exc))
        return True
    apply(theme)
    message = f"Theme: {theme.name}"
    if "--save" in parts[1:]:
        message += f". {save(theme.name)}"
    ctx.notify("success", message)
    return True


def _cmd_details(ctx: CommandContext) -> bool:
    """``/details`` cycles the tool detail level; ``/details LEVEL`` sets it (TUX-06).

    The level lasts for the session and applies to the next tool call: rows
    already printed stay as they are, so the scrollback remains append-only.
    """
    from agenthicc.tui.workspace.appender import (
        TOOL_DETAIL_LEVELS,
        session_tool_detail,
        set_session_tool_detail,
    )

    requested = ctx.args.strip().lower()
    if requested and requested not in TOOL_DETAIL_LEVELS:
        ctx.console.print(f"/details takes one of: {', '.join(TOOL_DETAIL_LEVELS)}", markup=False)
        return True
    if not requested:
        current = session_tool_detail() or ctx.config.ui.tool_detail
        index = TOOL_DETAIL_LEVELS.index(current) if current in TOOL_DETAIL_LEVELS else 0
        requested = TOOL_DETAIL_LEVELS[(index + 1) % len(TOOL_DETAIL_LEVELS)]
    set_session_tool_detail(requested)
    ctx.console.print(f"Tool detail: {requested}", markup=False)
    return True


def _cmd_subagents(ctx: CommandContext) -> bool:
    """The turn's subagents and the session's earlier ones (AGX-08 §5.2)."""

    if ctx.open_subagents is None:
        ctx.notify("warning", "/subagents needs the interactive TUI.")
        return True
    ctx.open_subagents()
    return True


def _cmd_inbox(ctx: CommandContext) -> bool:
    """Show what needs you: the agents hub's Needs you tab (AGX-05)."""

    if ctx.open_agents_hub is not None:
        ctx.open_agents_hub("needs-you")
        return True
    from agenthicc.background import (
        BackgroundInteractionInbox,
        BackgroundStore,
        load_background_settings,
    )

    inbox = BackgroundInteractionInbox(BackgroundStore(load_background_settings().store_root))
    inbox.reconcile()
    items = inbox.list_items()
    if not items:
        ctx.console.print(
            "No pending background approvals, questions, or input requests.", markup=False
        )
        return True
    for item in items:
        ctx.console.print(
            f"{item.item_id}  {item.kind:8}  {item.status:9}  {item.workspace}  "
            f"{item.session_id}  {item.target.get('tool_name', item.kind)}",
            markup=False,
        )
    return True


def _cmd_usage(ctx: CommandContext) -> bool:
    """Show the local usage snapshot without contacting the provider."""
    provider = ctx.usage_snapshot
    snapshot = provider() if provider is not None else None
    if not isinstance(snapshot, UsageSnapshot):
        ctx.notify("warning", "Usage is unavailable in this command context.")
        return True
    state = "running" if snapshot.active_run else "idle"
    if snapshot.usage_status == "unavailable":
        input_text = output_text = total_text = "unknown"
    else:
        input_text = f"{snapshot.input_tokens:,}"
        output_text = f"{snapshot.output_tokens:,}"
        total_text = f"{snapshot.total_tokens:,}"
    cost_text = "unknown" if snapshot.cost_status == "unavailable" else f"${snapshot.cost_usd:.4f}"
    ctx.console.print(
        "Usage: "
        f"input={input_text} "
        f"output={output_text} "
        f"total={total_text} "
        f"cost={cost_text} "
        f"state={state} "
        f"queued={snapshot.queue_depth} "
        f"usage_status={snapshot.usage_status} "
        f"cost_status={snapshot.cost_status} "
        f"calls={snapshot.calls} "
        f"durability={snapshot.durability_status}",
        markup=False,
    )
    return True


def _cmd_clear(ctx: CommandContext) -> bool:
    ctx.console.clear()
    return True


def _cmd_expand(ctx: CommandContext) -> bool:
    ctx.notify("info", "/expand is not available in this mode.")
    return True


def _help_menu(ctx: CommandContext) -> "Overlay":
    from agenthicc.tui.workspace.overlays.help import HelpOverlay

    on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
    registry = ctx.command_registry or UnifiedCommandRegistry()
    return HelpOverlay(registry, on_close, initial_query=ctx.args)


def _cmd_history(ctx: CommandContext) -> bool:
    """Open the bounded, paginated event-history view for this session."""

    action = ctx.args.strip().casefold()
    if action == "repair":
        if ctx.repair_session_history is None:
            ctx.console.print(
                "Conversation repair is available only in an attached foreground session.",
                markup=False,
            )
            return True
        try:
            state = ctx.repair_session_history()
        except Exception as exc:  # noqa: BLE001 - report type only, never transcript data
            ctx.console.print(
                f"Conversation repair failed ({type(exc).__name__}); preserved history was not sent.",
                markup=False,
            )
            return True
        if state.provider_ready:
            status = state.status
            ctx.console.print(
                "Conversation history is valid for provider use."
                if status == "ready"
                else "Conversation history was repaired and validated; workflow state is preserved.",
                markup=False,
            )
        else:
            status = state.status
            safe_codes = ", ".join(code[:64] for code in state.issue_codes[:8]) or "unknown"
            if status == "journal_corrupt":
                message = (
                    "Conversation history is preserved but blocked from provider use "
                    f"(issues: {safe_codes}). Automatic repair will not skip or rewrite "
                    "journal records; preserve the original and restore from a known-good "
                    "backup before resuming."
                )
            else:
                message = (
                    "Conversation history is preserved but still blocked from provider use "
                    f"(issues: {safe_codes}). No journal contents were discarded."
                )
            ctx.console.print(message, markup=False)
        return True
    if action:
        ctx.notify(
            "error", f"Unknown /history action: {action!r}.", body="Usage: /history [repair]"
        )
        return True
    if not ctx.session_id:
        ctx.notify("warning", "Transcript history is unavailable without a session ID.")
        return True
    from agenthicc.tui.runtime.session_log import load_recent_transcript_page

    behavior = ctx.config.behaviour
    try:
        page = load_recent_transcript_page(
            ctx.session_id,
            last_turns=behavior.resume_transcript_turns,
            max_events=behavior.resume_transcript_max_events,
            max_bytes=behavior.resume_transcript_max_bytes,
            max_event_bytes=behavior.resume_transcript_max_event_bytes,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        ctx.notify("error", f"Unable to load transcript history: {type(exc).__name__}.")
        return True
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.transcript_history import (
            TranscriptHistoryOverlay,
        )

        ctx.set_pending_menu(
            TranscriptHistoryOverlay(
                ctx.session_id,
                page,
                ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
            )
        )
        return True
    ctx.console.print(
        f"Transcript preview loaded {len(page.entries)} events; use the interactive TUI to browse older pages.",
        markup=False,
    )
    return True


def _cmd_logs(ctx: CommandContext) -> bool:
    """Show the tail of this session's diagnostics log (TUX-08 §2.3)."""
    from agenthicc.tui.diagnostics import diagnostics_log_path, log_tail

    path = diagnostics_log_path(ctx.session_id)
    lines = log_tail(path)
    if not lines:
        ctx.notify("info", "The diagnostics log is empty.")
        return True
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.log_tail import LogTailOverlay

        ctx.set_pending_menu(
            LogTailOverlay(
                path,
                lines,
                ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
            )
        )
        return True
    ctx.console.print(f"Diagnostics log: {path}", markup=False, highlight=False)
    for line in lines:
        ctx.console.print(line, markup=False, highlight=False)
    return True


def _cmd_init(ctx: CommandContext) -> bool:
    """Preview or explicitly write project guidance in ``AGENTS.md``."""
    from pathlib import Path

    from agenthicc.project_bootstrap import (
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
        ctx.notify("error", str(exc))
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
        ctx.notify(
            "warning",
            "Refusing to overwrite existing AGENTS.md.",
            body="Review the diff, then run /init write --force.",
        )
        return True
    try:
        target = write_bootstrap_plan(plan, force=force)
    except BootstrapWriteError as exc:
        ctx.notify("error", str(exc))
        return True
    ctx.console.print(f"Updated {target}", markup=False)
    return True


_MCP_USAGE = "Usage: /mcp [status|reload|connect NAME|disconnect NAME|refresh NAME|doctor [NAME]]"


def _cmd_mcp(ctx: CommandContext) -> bool:
    manager = ctx.mcp_manager
    if manager is None:
        ctx.notify("info", "No MCP servers are configured or available.")
        return True
    parts = ctx.args.strip().split()
    action = parts[0].lower() if parts else "status"
    name = parts[1] if len(parts) > 1 else None
    if action in {"status", "list"}:
        status = manager.status()
        if not status:
            ctx.notify("info", "No MCP servers are configured.")
        else:
            ctx.console.print(json.dumps(status, sort_keys=True, indent=2), markup=False)
        return True
    if action == "reload" and name is not None:
        ctx.notify("error", f"Unknown /mcp command: {ctx.args.strip()!r}.", body=_MCP_USAGE)
        return True
    if action not in {"connect", "disconnect", "refresh", "doctor", "reload"} or (
        action not in {"doctor", "reload"} and not name
    ):
        ctx.notify("error", f"Unknown /mcp command: {ctx.args.strip()!r}.", body=_MCP_USAGE)
        return True

    async def _run() -> None:
        try:
            if action == "connect" and name is not None:
                await manager.connect_server(name)
            elif action == "disconnect" and name is not None:
                await manager.disconnect_server(name)
            elif action == "refresh" and name is not None:
                await manager.refresh_server(name)
            elif action == "reload":
                await manager.reload_all()
            else:
                result = await manager.doctor(name)
                ctx.console.print(json.dumps(result, sort_keys=True, indent=2), markup=False)
                return
            ctx.console.print(
                f"/mcp {action} complete{f' for {name}' if name else ''}.", markup=False
            )
        except Exception as exc:  # noqa: BLE001
            ctx.notify("error", f"/mcp {action} failed: {type(exc).__name__}: {exc}")

    manager.start_action(_run(), name=f"mcp-{action}")
    ctx.console.print(f"/mcp {action} scheduled{f' for {name}' if name else ''}.", markup=False)
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


def _workflows_busy_policy(args: str) -> BusyPolicy:
    """Keep recovery inspection immediate; defer registry mutation/typos."""
    return (
        BusyPolicy.IMMEDIATE_READ_ONLY if args.strip().lower() in {"", "runs"} else BusyPolicy.QUEUE
    )


def _history_busy_policy(args: str) -> BusyPolicy:
    """Keep transcript browsing read-only and queue durable repair operations."""
    return BusyPolicy.IMMEDIATE_READ_ONLY if not args.strip() else BusyPolicy.QUEUE


def _present_registry_result(ctx: CommandContext, title: str, message: str) -> None:
    """Show registry command output in the live overlay when available."""
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.registry_list import RegistryMessageOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(RegistryMessageOverlay(title, message, on_close))
        return
    ctx.console.print(message, markup=False)


def _model_rows(choices: "list[ModelChoice]") -> "list[PickerRow[ModelChoice]]":
    """``/model`` picker rows: grouped by provider, then the saved profiles."""
    from agenthicc.runners.model_switch import short_model
    from agenthicc.tui.theme import glyphs
    from agenthicc.tui.workspace.picker import PickerRow

    g = glyphs()
    rows: list[PickerRow[ModelChoice]] = []
    for choice in choices:
        window = f"{choice.context_window // 1000}k context" if choice.context_window else ""
        state = g.ok if choice.available else f"{g.fail} set {choice.missing_env}"
        detail = f" {g.sep} ".join(part for part in (window, state) if part)
        if choice.current:
            detail += " (current)"
        rows.append(
            PickerRow(
                value=choice,
                label=choice.profile or short_model(choice.model),
                detail=detail if choice.available else window,
                group=f"Profile {g.sep} {choice.provider}" if choice.profile else choice.provider,
                disabled="" if choice.available else f"{g.fail} set {choice.missing_env}",
            )
        )
    return rows


def _cmd_model(ctx: CommandContext) -> bool:
    """``/model`` opens the picker; ``/model PROVIDER [MODEL] [--save]`` switches (TUX-05)."""
    from agenthicc.config import SUPPORTED_PROVIDERS
    from agenthicc.runners.model_switch import model_choice, model_choices

    parts = ctx.args.split()
    save = "--save" in parts
    parts = [part for part in parts if part != "--save"]
    if not parts:
        choices = model_choices(ctx.config)
        if ctx.set_pending_menu is None or ctx.switch_model is None:
            for choice in choices:
                mark = "*" if choice.current else " "
                state = "ok" if choice.available else f"set {choice.missing_env}"
                name = f"{choice.profile} ({choice.label})" if choice.profile else choice.label
                ctx.console.print(f"{mark} {name}  {state}", markup=False)
            return True
        from agenthicc.tui.workspace.overlays.choice_picker import ChoicePickerOverlay

        switch = ctx.switch_model
        saver = ctx.save_model
        overlay: ChoicePickerOverlay[ModelChoice] = ChoicePickerOverlay(
            "Model",
            _model_rows(choices),
            subtitle="applies from the next turn, no restart",
            choose=switch,
            close=ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
            keep_open=True,
            extra_keys={"s": ("save as default", saver)} if saver is not None else None,
        )
        current = next((choice for choice in choices if choice.current), None)
        if current is not None:
            overlay.select_value(current)
        ctx.set_pending_menu(overlay)
        return True

    provider = parts[0].lower()
    profile = ""
    if provider not in SUPPORTED_PROVIDERS:
        if provider in ctx.config.providers:
            profile, provider = provider, ctx.config.providers[provider].provider
        else:
            choices_text = ", ".join([*SUPPORTED_PROVIDERS, *sorted(ctx.config.providers)])
            ctx.notify(
                "error",
                f"Unknown provider or profile {parts[0]!r}.",
                body=f"Choose one of: {choices_text}.",
            )
            return True
    choice = model_choice(
        ctx.config,
        provider,
        "" if profile else (parts[1] if len(parts) > 1 else ""),
        profile=profile,
    )
    if not choice.available:
        ctx.notify("warning", f"{choice.missing_env} is not set; set it to use {choice.label}.")
        return True
    if ctx.switch_model is None:
        ctx.notify(
            "error",
            "/model needs the interactive TUI.",
            body="Set [execution] provider and model in the configuration instead.",
        )
        return True
    message = ctx.switch_model(choice)
    if save and ctx.save_model is not None:
        message = f"{message}. {ctx.save_model(choice)}"
    ctx.notify("success", message)
    return True


def _cmd_skills(ctx: CommandContext) -> bool:
    requested = ctx.args.strip().lower()
    if requested:
        if requested != "reload":
            _present_registry_result(ctx, "Skills", "Usage: /skills [reload]")
            return True
        if ctx.reload_skills is None:
            _present_registry_result(
                ctx,
                "Skills",
                "Skill reload is only available in an interactive session.",
            )
            return True

        before = set(ctx.skills)
        try:
            discovery: SkillDiscoveryResult = ctx.reload_skills()
        except Exception as exc:  # noqa: BLE001
            message = str(exc).strip() or type(exc).__name__
            _present_registry_result(ctx, "Skills", f"Skill reload failed: {message}")
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
        messages = [f"Reloaded {len(after)} skill(s){suffix}."]
        for diagnostic in discovery.diagnostics:
            if diagnostic.severity != "info":
                messages.append(f"Skill reload: {diagnostic}")
        _present_registry_result(ctx, "Skills", "\n".join(messages))
        return True

    visible_skills = {
        slug: skill for slug, skill in ctx.skills.items() if _skill_allowed_for_context(ctx, skill)
    }
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.registry_list import SkillListOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(
            SkillListOverlay(
                list(sorted(visible_skills.values(), key=lambda s: s.slug)),
                on_close,
                ctx.set_input_text,
            )
        )
        return True

    try:
        from rich.table import Table
        from rich import box as _rbox

        from agenthicc.tui.theme import palette

        table = Table(title="Available Skills", box=_rbox.SIMPLE)
        table.add_column("Command", style=f"bold {palette().accent}")
        table.add_column("Name")
        table.add_column("Description")
        if not visible_skills:
            table.add_row("—", "(no skills found)", "")
        else:
            for _slug, skill in sorted(visible_skills.items()):
                command_names = ", ".join(f"${name}" for name in skill.command_names)
                table.add_row(
                    command_names,
                    skill.name,
                    (getattr(skill, "description", "") or "")[:80] or "—",
                )
        ctx.console.print(table)
    except ImportError:
        for _slug, skill in sorted(visible_skills.items()):
            command_names = ", ".join(f"${name}" for name in skill.command_names)
            ctx.console.print(f"  {command_names}  {skill.name}")
    return True


def _preflight_rows(ctx: CommandContext) -> list[tuple[str, str]]:
    """The pre-flight check's findings as ``/status`` rows (TUX-03 §5)."""
    import os

    from agenthicc.runners.preflight import check_session

    mode = ctx.app_state.active_mode().name if ctx.app_state is not None else ""
    issues = check_session(ctx.config, os.environ, mode=mode)
    if not issues:
        return [("Pre-flight", "all checks passed")]
    return [("Pre-flight", issue.text) for issue in issues]


def _cmd_status(ctx: CommandContext) -> bool:
    """Print the session's identity, model, usage and files (TUX-02 §2.2).

    The session ID, directory and usage left the status line; this is where
    they are, and where an absent value (cost, tokens) is explained.
    """
    import time

    from rich.markup import escape

    if ctx.app_state is None:
        rows = [("Session", ctx.session_id or "not saved yet"), ("Model", ctx.model or "unknown")]
    else:
        from agenthicc.tui.statusline import status_report

        rows = status_report(ctx.app_state)
    # Transcript times are local ``HH:MM``; the zone is shown here (TUX-06 §2.5).
    rows = [*rows, ("Time zone", time.strftime("%Z %z")), *_preflight_rows(ctx)]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        ctx.console.print(f"  [dim]{label + ':':<{width}}[/dim] {escape(value)}")
    return True


def _cmd_keybindings(ctx: CommandContext) -> bool:
    """Show the effective key bindings and create the bindings file (AGX-13)."""
    service = ctx.keymap_service
    if service is None:
        ctx.notify("warning", "Key bindings are unavailable here.")
        return True
    from agenthicc.tui.keymap_table import render_bindings

    created = service.ensure_file()
    loaded = service.reload()
    render_bindings(ctx.console, loaded)
    if created:
        ctx.console.print("Created the file with every action listed, commented out.", markup=False)
    ctx.console.print(
        "Edit the file and the changes apply at once; `agenthicc keybindings edit` "
        "opens it in your editor.",
        markup=False,
    )
    return True


def _cmd_perf(ctx: CommandContext) -> bool:
    """Show how many frames the live block drew and what they cost (AGX-11)."""
    stats = ctx.frame_stats() if ctx.frame_stats is not None else None
    if stats is None:
        ctx.notify("warning", "Rendering diagnostics are unavailable.")
        return True
    from agenthicc.tui.terminal.capabilities import capabilities

    summary = stats.summary()
    ctx.console.print(
        f"Frames drawn: {stats.frames} ({stats.full_redraws} full redraws)", markup=False
    )
    ctx.console.print(
        f"Render: p50 {summary['render_p50_ms']:.2f} ms · p95 {summary['render_p95_ms']:.2f} ms",
        markup=False,
    )
    ctx.console.print(
        f"Write: p50 {summary['write_p50_ms']:.2f} ms · p95 {summary['write_p95_ms']:.2f} ms",
        markup=False,
    )
    ctx.console.print(
        "Characters per frame: "
        f"p50 {summary['characters_p50']:.0f} · p95 {summary['characters_p95']:.0f}",
        markup=False,
    )
    ctx.console.print(
        "Synchronized output: " + ("on" if capabilities.synchronized_output else "off"),
        markup=False,
    )
    monitor = ctx.loop_lag() if ctx.loop_lag is not None else None
    if monitor is not None:
        lag = monitor.summary()
        ctx.console.print(
            f"Event-loop lag: p50 {lag['p50_ms']:.1f} ms · p95 {lag['p95_ms']:.1f} ms · "
            f"max {lag['max_ms']:.0f} ms",
            markup=False,
        )
        for stall in monitor.stalls():
            ctx.console.print(
                f"  stall {stall.duration_s * 1_000:.0f} ms at {stall.culprit}", markup=False
            )
    return True


def _cmd_startup(ctx: CommandContext) -> bool:
    """Show redacted startup phase timing and readiness diagnostics."""
    coordinator = ctx.startup
    if coordinator is None:
        ctx.notify("warning", "Startup diagnostics are unavailable.")
        return True
    milestones = coordinator.milestones()

    def seconds(value: float | None, pending: str) -> str:
        return f"{value:.3f}s" if value is not None else pending

    ctx.console.print(
        "Process start → first frame: "
        f"{seconds(milestones.process_to_first_frame_s, 'not recorded')}",
        markup=False,
    )
    ctx.console.print(
        "First frame → agent runtime ready: "
        f"{seconds(milestones.first_frame_to_runtime_ready_s, 'not ready')}",
        markup=False,
    )
    if milestones.modules_at_first_frame is not None:
        ctx.console.print(
            f"Modules loaded at first frame: {milestones.modules_at_first_frame}",
            markup=False,
        )
    reports = coordinator.snapshot()
    if not reports:
        ctx.console.print("No startup phases have been recorded.", markup=False)
        return True
    for report in reports:
        elapsed = report.elapsed_s
        elapsed_text = f"{elapsed:.3f}s" if elapsed is not None else "—"
        ctx.console.print(f"{report.name}: {report.state.value} ({elapsed_text})", markup=False)
    return True


def _cmd_ps(ctx: CommandContext) -> bool:
    """Open or print the owned background-terminal manager."""

    manager = ctx.terminal_manager
    if manager is None:
        ctx.notify("info", "Background terminal management is unavailable.")
        return True
    args = ctx.args.strip().split()
    if "--json" in args:
        import json

        ctx.console.print(
            json.dumps([record.to_dict() for record in manager.list_records()], sort_keys=True),
            markup=False,
        )
        return True
    selected = next((item for item in args if not item.startswith("-")), "")
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.terminals import TerminalListOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(TerminalListOverlay(manager, on_close, selected_id=selected))
        return True

    records = manager.list_records()
    if not records:
        ctx.console.print("[dim]No background terminals for this session.[/dim]")
        return True
    try:
        from rich import box as _rbox
        from rich.table import Table

        table = Table(title="Background Terminals", box=_rbox.SIMPLE)
        table.add_column("ID", style="bold")
        table.add_column("State")
        table.add_column("Command")
        table.add_column("Return")
        for record in records:
            table.add_row(
                record.terminal_id,
                record.state.value,
                record.label,
                "—" if record.returncode is None else str(record.returncode),
            )
        ctx.console.print(table)
    except ImportError:
        for record in records:
            ctx.console.print(f"{record.terminal_id} {record.state.value} {record.label}")
    return True


def _cmd_workers(ctx: CommandContext) -> bool:
    """Show this session's parallel workers: the agents hub's Running tab (AGX-05).

    ``--json`` prints the worker manifests instead, as it always has.
    """

    if ctx.open_agents_hub is not None and "--json" not in ctx.args.split():
        ctx.open_agents_hub("running")
        return True
    from agenthicc.worktrees import ManifestStore

    manifests = ManifestStore().list()
    visible = [
        manifest
        for manifest in manifests
        if not ctx.session_id or manifest.parent_session_id == ctx.session_id
    ]
    if "--json" in ctx.args.split():
        import json

        ctx.console.print(
            json.dumps([manifest.to_dict() for manifest in visible], sort_keys=True),
            markup=False,
        )
        return True
    try:
        from rich import box as _rbox
        from rich.table import Table

        table = Table(title="Parallel Workers", box=_rbox.SIMPLE)
        table.add_column("Orchestration", style="bold")
        table.add_column("Task")
        table.add_column("Status")
        table.add_column("Worktree")
        for manifest in visible:
            for task in manifest.tasks:
                table.add_row(
                    manifest.orchestration_id[:16],
                    task.task_id,
                    task.status.value,
                    task.worktree_id[:16] or "—",
                )
        if not visible:
            table.add_row("—", "—", "no worker orchestrations", "—")
        ctx.console.print(table)
    except ImportError:
        if not visible:
            ctx.console.print("No parallel worker orchestrations.")
        for manifest in visible:
            for task in manifest.tasks:
                ctx.console.print(
                    f"{manifest.orchestration_id[:16]} {task.task_id} {task.status.value}"
                )
    return True


def _cmd_stop(ctx: CommandContext) -> bool:
    """Stop all owned terminals, or one explicitly named handle."""

    manager = ctx.terminal_manager
    if manager is None:
        ctx.notify("warning", "Background terminal management is unavailable.")
        return True
    args = ctx.args.strip().split()
    force = "--force" in args
    targets = [item for item in args if not item.startswith("-")]
    target = targets[0] if targets else None
    if target == "all":
        confirmed = force or "--confirm" in args
        if not confirmed:
            ctx.notify(
                "warning",
                "This will stop every visible background terminal.",
                body="Repeat as /stop all --confirm (or --force) to confirm.",
            )
            return True
        count = manager.request_stop_all(force=force)
        ctx.notify("info", f"Stop requested for {count} background terminal(s).")
        return True
    if target is None:
        count = manager.request_stop_all(force=force)
        ctx.notify("info", f"Stop requested for {count} background terminal(s).")
        return True
    if manager.request_stop(target, force=force):
        ctx.notify("info", f"Stop requested for {target}.")
    else:
        ctx.notify("warning", f"No running owned terminal found for {target}.")
    return True


def _cmd_commands(ctx: CommandContext) -> bool:
    requested = ctx.args.strip()
    if requested:
        if requested != "reload":
            _present_registry_result(ctx, "Commands", "Usage: /commands [reload]")
            return True
        if ctx.reload_commands is None:
            _present_registry_result(
                ctx,
                "Commands",
                "Command reload is only available in an interactive session.",
            )
            return True
        try:
            _ok, message = ctx.reload_commands()
        except Exception as exc:  # noqa: BLE001
            message = f"Command reload failed; existing commands kept: {type(exc).__name__}: {exc}"
        _present_registry_result(ctx, "Commands", message)
        return True

    registry = ctx.command_registry
    if registry is None:
        _present_registry_result(ctx, "Commands", "No command registry available.")
        return True
    commands = [cmd for cmd in registry.all_commands() if not cmd.is_skill]
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.registry_list import CommandListOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(CommandListOverlay(commands, on_close, ctx.set_input_text))
        return True

    try:
        from rich.table import Table
        from rich import box as _rbox

        table = Table(title="Registered Commands", box=_rbox.SIMPLE)
        table.add_column("Command", style="bold")
        table.add_column("Group")
        table.add_column("Source", style="dim")
        table.add_column("Description")
        for cmd in commands:
            table.add_row(cmd.name, cmd.group, cmd.source_id, cmd.description)
        ctx.console.print(table)
    except ImportError:
        for cmd in commands:
            ctx.console.print(f"{cmd.name}  [{cmd.group}]  {cmd.description}")
    return True


def _cmd_tools(ctx: CommandContext) -> bool:
    """List the tools available to the current session."""
    requested = ctx.args.strip().lower()
    if requested:
        if requested != "reload":
            _present_registry_result(ctx, "Tools", "Usage: /tools [reload]")
            return True
        if ctx.reload_tools is None:
            _present_registry_result(
                ctx,
                "Tools",
                "Tool reload is only available in an interactive session.",
            )
            return True
        try:
            _ok, message = ctx.reload_tools()
        except Exception as exc:  # noqa: BLE001
            message = f"Tool reload failed; existing tools kept: {type(exc).__name__}: {exc}"
        _present_registry_result(ctx, "Tools", message)
        return True

    tools = ctx.tools
    tool_sources = ctx.tool_sources
    if tools is None:
        from agenthicc.plugins.registry import build_registry

        registry = build_registry()
        tools = registry.tools
        tool_sources = registry.sources
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.registry_list import ToolListOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(ToolListOverlay(list(tools), on_close, tool_sources))
        return True

    try:
        from rich import box as _rbox
        from rich.table import Table

        from agenthicc.tui.theme import palette

        table = Table(title="Available Tools", box=_rbox.SIMPLE)
        table.add_column("Tool", style=f"bold {palette().accent}")
        table.add_column("Source", style="dim")
        table.add_column("Description")
        if not tools:
            table.add_row("—", "—", "(no tools found)")
        else:
            for tool in tools:
                name_value: object = getattr(tool, "name", None)
                if not isinstance(name_value, str) or not name_value:
                    name_value = getattr(tool, "__name__", type(tool).__name__)
                description_value: object = getattr(tool, "description", None)
                if not isinstance(description_value, str) or not description_value:
                    description_value = getattr(tool, "__doc__", "")
                name = str(name_value)
                description_lines = str(description_value).splitlines()
                description = description_lines[0] if description_lines else ""
                source = (
                    "builtin" if tool_sources and tool_sources.get(name) == "builtin" else "plugin"
                )
                table.add_row(name, source, description or "—")
        ctx.console.print(table)
    except ImportError:
        for tool in tools:
            fallback_name_value: object = getattr(tool, "name", None)
            if not isinstance(fallback_name_value, str) or not fallback_name_value:
                fallback_name_value = getattr(tool, "__name__", type(tool).__name__)
            name = str(fallback_name_value)
            source = "builtin" if tool_sources and tool_sources.get(name) == "builtin" else "plugin"
            ctx.console.print(f"  {name}  [{source}]")
    return True


def _cmd_workflows(ctx: CommandContext) -> bool:
    """List workflows loaded into the current registry."""
    requested = ctx.args.strip().lower()
    if requested:
        if requested == "runs":
            if ctx.list_workflow_runs is None:
                _present_registry_result(
                    ctx,
                    "Workflow Runs",
                    "Workflow recovery is only available in an interactive session.",
                )
                return True
            try:
                records = ctx.list_workflow_runs()
            except Exception as exc:  # noqa: BLE001
                _present_registry_result(
                    ctx,
                    "Workflow Runs",
                    f"Workflow recovery inspection failed: {type(exc).__name__}: {exc}",
                )
                return True
            if ctx.set_pending_menu is not None:
                from agenthicc.tui.workspace.overlays.registry_list import (
                    WorkflowRunsOverlay,
                )

                on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
                on_resume = ctx.resume_workflow
                if on_resume is None:
                    _present_registry_result(
                        ctx,
                        "Workflow Runs",
                        "Workflow recovery is not available in this session.",
                    )
                    return True
                ctx.set_pending_menu(WorkflowRunsOverlay(records, on_close, on_resume))
                return True

            try:
                from rich import box as _rbox
                from rich.table import Table

                from agenthicc.tui.theme import palette

                table = Table(title="Paused Workflow Runs", box=_rbox.SIMPLE)
                table.add_column("Workflow", style=f"bold {palette().accent}")
                table.add_column("Phase")
                table.add_column("Status", style=palette().warning)
                table.add_column("Run ID", no_wrap=True)
                for record in records:
                    table.add_row(
                        record.workflow_name or "—",
                        record.current_phase or "saved state",
                        record.status,
                        record.run_id,
                    )
                if not records:
                    table.add_row("—", "—", "—", "(no paused workflow runs)")
                ctx.console.print(table)
            except ImportError:
                if not records:
                    ctx.console.print("No paused workflow runs.")
                for record in records:
                    ctx.console.print(
                        f"  {record.workflow_name or '—'}  "
                        f"{record.current_phase or 'saved state'}  "
                        f"[{record.status}]  {record.run_id}"
                    )
            return True
        if requested != "reload":
            _present_registry_result(ctx, "Workflows", "Usage: /workflows [runs|reload]")
            return True
        if ctx.reload_workflows is None:
            _present_registry_result(
                ctx,
                "Workflows",
                "Workflow reload is only available in an interactive session.",
            )
            return True
        try:
            _ok, message = ctx.reload_workflows()
        except Exception as exc:  # noqa: BLE001
            message = (
                f"Workflow reload failed; existing workflows kept: {type(exc).__name__}: {exc}"
            )
        _present_registry_result(ctx, "Workflows", message)
        return True

    registry = ctx.workflow_registry
    if registry is None:
        from agenthicc.workflows.registry import build_workflow_registry

        registry = build_workflow_registry()
    workflows = registry.all()
    if ctx.set_pending_menu is not None:
        from agenthicc.tui.workspace.overlays.registry_list import WorkflowListOverlay

        on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
        ctx.set_pending_menu(WorkflowListOverlay(workflows, registry, on_close, ctx.set_input_text))
        return True

    try:
        from rich import box as _rbox
        from rich.table import Table

        from agenthicc.tui.theme import palette

        table = Table(title="Registered Workflows", box=_rbox.SIMPLE)
        table.add_column("Workflow", style=f"bold {palette().accent}")
        table.add_column("Source", style="dim")
        table.add_column("Description")
        for workflow in workflows:
            entry = registry.get_entry(workflow.name)
            table.add_row(
                workflow.name,
                entry.source if entry is not None else "registered",
                workflow.description or "—",
            )
        if not workflows:
            table.add_row("—", "", "(no workflows found)")
        ctx.console.print(table)
    except ImportError:
        for workflow in workflows:
            ctx.console.print(f"  {workflow.name}  {workflow.description}")
    return True


def _menu_config(ctx: CommandContext) -> "Overlay":
    from agenthicc.tui.workspace.overlays.config_menu import ConfigMenuOverlay

    on_close = ctx.close_overlay if ctx.close_overlay is not None else (lambda: None)
    return ConfigMenuOverlay(ctx.config, on_close)


def _cmd_mode(ctx: CommandContext) -> bool:
    """``/mode`` opens the picker; ``/mode NAME`` switches (TUX-05 §2.5)."""
    from agenthicc.tui.theme import glyphs

    mode_manager = ctx.mode_manager
    if mode_manager is None:
        ctx.notify("warning", "The mode system is not available.")
        return True
    args = (ctx.args or "").strip()
    if not args:
        modes = mode_manager.registry.all()
        if ctx.set_pending_menu is None:
            for mode in modes:
                mark = "*" if mode.name == mode_manager.active_name else " "
                ctx.console.print(f"{mark} {mode.name}  {mode.description}", markup=False)
            return True
        from agenthicc.tui.workspace.overlays.choice_picker import ChoicePickerOverlay
        from agenthicc.tui.workspace.picker import PickerRow

        def choose(name: str) -> str:
            chosen = mode_manager.set_by_name(name)
            return f"{chosen.badge} {chosen.name} mode" if chosen else f"Unknown mode {name!r}"

        overlay: ChoicePickerOverlay[str] = ChoicePickerOverlay(
            "Mode",
            [
                PickerRow(
                    value=mode.name,
                    label=mode.name,
                    glyph=mode.badge,
                    detail=mode.description
                    + (" (current)" if mode.name == mode_manager.active_name else ""),
                )
                for mode in modes
            ],
            subtitle="what the agent may do without asking",
            choose=choose,
            close=ctx.close_overlay if ctx.close_overlay is not None else (lambda: None),
        )
        overlay.select_value(mode_manager.active_name)
        ctx.set_pending_menu(overlay)
        return True
    new_mode = mode_manager.set_by_name(args)
    if new_mode is None:
        choices = ", ".join(mode_manager.registry.selectable_names())
        ctx.notify("error", f"Unknown mode: {args!r}.", body=f"Choose one of: {choices}.")
        return True
    ctx.notify("success", f"Mode {glyphs().right} {new_mode.name}")
    return True


# ── PRD-45: skill handler factory ─────────────────────────────────────────────


def _skill_allowed_for_context(ctx: CommandContext, skill: "SkillDef") -> bool:
    """Apply frontmatter and configured per-agent skill permissions."""

    from agenthicc.skills.loader import SkillPermissionSet

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
    from agenthicc.skills.runner import process_skill_body

    def _handler(ctx: CommandContext) -> bool:
        if not _skill_allowed_for_context(ctx, skill):
            ctx.console.print(
                f"Skill ${slug} is not permitted for agent {ctx.active_agent!r}.",
                markup=False,
            )
            return True

        args = ctx.args.split() if ctx.args.strip() else []
        body = process_skill_body(skill, args=args, session_id=ctx.session_id)
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
        group="Session",
        description="Replay a previous session's conversation in the scroll buffer",
        argument_hint="[session-id]",
        handler=_cmd_replay,
    ),
    Command(
        name="/cancel",
        group="Session",
        description="Cancel the currently running intent",
        aliases=("/interrupt",),
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_cancel,
    ),
    Command(
        name="/loop",
        group="Workflows",
        description="Schedule a recurring prompt or registered slash command",
        argument_hint="[interval] <prompt> | status | pause | resume | stop",
        busy_policy=BusyPolicy.QUEUE,
        busy_policy_resolver=_loop_busy_policy,
        handler=_cmd_loop,
    ),
    Command(
        name="/loops",
        group="Workflows",
        description="Manage all scheduled loop jobs",
        argument_hint="",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_loops,
    ),
    Command(
        name="/schedules",
        group="Workflows",
        description="Manage repository-scoped scheduled workflows",
        argument_hint="",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_schedules,
    ),
    Command(
        name="/schedule",
        group="Workflows",
        description="Create or control a repository scheduled workflow",
        argument_hint="create ... | run-now ID | pause ID | resume ID NEXT-RUN | delete ID --confirm",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_schedule,
    ),
    Command(
        name="/agents",
        group="Agents",
        description="Open the agents hub: running work, what needs you, history, library",
        argument_hint="[running|needs-you|history|library]",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_agents,
    ),
    Command(
        name="/resume",
        group="Agents",
        description="Resume a saved session: the agents hub's History tab",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_resume,
    ),
    Command(
        name="/release-notes",
        group="Session",
        description="Show what changed in this version and since the last one you read",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        menu_factory=_release_notes_menu,
    ),
    Command(
        name="/dnd",
        group="Agents",
        description="Toggle do-not-disturb for background agent attention",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_dnd,
    ),
    Command(
        name="/inbox",
        group="Agents",
        description="Review and answer background approvals and questions (agents hub)",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_inbox,
    ),
    Command(
        name="/agent",
        group="Agents",
        description="Choose the primary agent the session talks to (also Tab)",
        argument_hint="[name]",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_agent,
    ),
    Command(
        name="/subagents",
        group="Agents",
        description="Show the turn's subagents and open a worker's transcript (also Ctrl+O)",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_subagents,
    ),
    Command(
        name="/clear",
        group="Session",
        description="Clear the transcript display",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_clear,
    ),
    Command(
        name="/commands",
        group="Session",
        description="List all registered commands with their source",
        argument_hint="[reload]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_reloadable_list_policy,
        handler=_cmd_commands,
    ),
    Command(
        name="/tools",
        group="Configuration",
        description="List all tools registered for this session",
        argument_hint="[reload]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_reloadable_list_policy,
        handler=_cmd_tools,
    ),
    Command(
        name="/workflows",
        group="Workflows",
        description="List all loaded workflows and their phases",
        argument_hint="[runs|reload]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_workflows_busy_policy,
        handler=_cmd_workflows,
    ),
    Command(
        name="/theme",
        group="Configuration",
        description="Choose the colour theme; applies at once, no restart",
        argument_hint="[name] [--save]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_theme,
    ),
    Command(
        name="/details",
        group="Configuration",
        description="Cycle tool detail: compact, normal, verbose",
        argument_hint="[compact|normal|verbose]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_details,
    ),
    Command(
        name="/config",
        group="Configuration",
        description="Open configuration editor",
        menu_factory=_menu_config,
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
    ),
    Command(
        name="/expand",
        group="Session",
        description="Expand tool output or @mention",
        argument_hint="[tool-id-or-@path]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_expand,
    ),
    Command(
        name="/help",
        group="Session",
        description="List available commands",
        argument_hint="[/command]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        menu_factory=_help_menu,
    ),
    Command(
        name="/history",
        group="Session",
        description="Browse transcript history or repair provider tool history",
        argument_hint="[repair]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_history_busy_policy,
        handler=_cmd_history,
    ),
    Command(
        name="/logs",
        group="Session",
        description="Show this session's diagnostics log",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_logs,
    ),
    Command(
        name="/init",
        group="Session",
        description="Inspect the project and preview AGENTS.md guidance",
        argument_hint="[write] [--force]",
        handler=_cmd_init,
    ),
    Command(
        name="/mcp",
        group="MCP",
        description="Show MCP server status",
        argument_hint="[status|reload|connect NAME|disconnect NAME|refresh NAME|doctor [NAME]]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_mcp_busy_policy,
        handler=_cmd_mcp,
    ),
    Command(
        name="/model",
        group="Configuration",
        aliases=("/models",),
        description="Choose the provider and model; applies from the next turn",
        argument_hint="[provider] [model]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_read_only_without_args,
        handler=_cmd_model,
    ),
    Command(
        name="/skills",
        group="Configuration",
        description="List or reload available skills",
        argument_hint="[reload]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        busy_policy_resolver=_reloadable_list_policy,
        handler=_cmd_skills,
    ),
    Command(
        name="/status",
        group="Session",
        description="Show the session, model, mode, usage, context and log paths",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_status,
    ),
    Command(
        name="/startup",
        group="Session",
        description="Show startup phase readiness and timing diagnostics",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_startup,
    ),
    Command(
        name="/keybindings",
        group="Configuration",
        description="Show key bindings and where to change them",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_keybindings,
    ),
    Command(
        name="/perf",
        group="Session",
        description="Show live-block frame counts and render and write cost",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_perf,
    ),
    Command(
        name="/ps",
        group="Session",
        description="Open the live manager for owned background terminals",
        aliases=("/processes",),
        argument_hint="[terminal-id] [--json]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_ps,
    ),
    Command(
        name="/background",
        group="Session",
        aliases=("/bg", "/detach"),
        description="Detach the active session into a background agent",
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
    ),
    Command(
        name="/workers",
        group="Agents",
        description="Show parallel workers in the agents hub",
        argument_hint="[--json]",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_workers,
    ),
    Command(
        name="/stop",
        group="Session",
        description="Stop all or one named background terminal",
        argument_hint="[terminal-id|all] [--force]",
        aliases=("/stop-terminal",),
        busy_policy=BusyPolicy.IMMEDIATE_CONTROL,
        handler=_cmd_stop,
    ),
    Command(
        name="/usage",
        group="Session",
        description="Show local token, cost, and active-run usage",
        busy_policy=BusyPolicy.IMMEDIATE_READ_ONLY,
        handler=_cmd_usage,
    ),
    Command(
        name="/mode",
        group="Configuration",
        description="Show or switch operational mode",
        argument_hint="[Safe|Plan|Yolo]",
        handler=_cmd_mode,
    ),
    Command(
        name="/workflow",
        group="Workflows",
        description="Switch workflow, or resume the latest saved run",
        argument_hint="<name> | resume [run-id] | reset [run-id]",
        # No handler: /workflow is intercepted in TUISession.route() before
        # dispatch_slash() so it can access session-local state.  The entry
        # exists here solely so the trigger picker displays and completes it.
        handler=None,
    ),
    Command(
        name="/compact",
        group="Session",
        description="Summarise conversation history to free context-window space",
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
