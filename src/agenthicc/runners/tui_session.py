"""TUI session orchestrator — kernel init, renderer setup, and intent loop."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

def _reset_terminal_on_exit() -> None:
    """Unconditionally reset the terminal to a usable state at TUI exit.

    This is a last-resort cleanup that runs after ``asyncio.run()`` returns,
    covering edge cases where ``cbreak_reader.raw_mode``'s ``_restore()``
    was not called (race conditions, process signals, unhandled exceptions).

    What we reset:
      * SGR attributes (colour, bold, dim, etc.) via ``\\x1b[m``
      * Bracketed paste mode OFF via ``\\x1b[?2004l``
      * Cursor visibility ON via ``\\x1b[?25h``
      * ECHO and ICANON re-enabled via termios (so typed text is visible)
    """
    try:
        import sys as _sys
        _sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
        _sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        import termios as _tm
        fd = 0  # stdin
        settings = _tm.tcgetattr(fd)
        # Re-enable ECHO, ICANON, ISIG so the terminal is fully interactive.
        settings[3] |= _tm.ECHO | _tm.ICANON | _tm.ISIG
        _tm.tcsetattr(fd, _tm.TCSAFLUSH, settings)
    except Exception:  # noqa: BLE001
        pass


from agenthicc.sessions import (
    _SESSIONS_DIR,
    _find_latest_session_for_cwd,
    _get_session_log_path,
    _register_session,
    _touch_session,
)
from agenthicc.runners.agent_builder import _build_agent_runner
from agenthicc.runners.agent_turn import _run_agent_turn


async def _run_tui_session(resume_id: str | None = None, cli_overrides: list[str] | None = None) -> None:
    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings
    from agenthicc.kernel.reducer import root_reducer
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.tui.transcript import TranscriptModel
    from agenthicc.tui.events import TUIEventAdapter
    from agenthicc.tui.tui import AgenthiccTUI as InlineRenderer
    from agenthicc.config import load_config, build_llm_config
    from agenthicc.conversation_store import ConversationStore  # noqa: PLC0415

    session_id = resume_id or uuid.uuid4().hex
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")

    settings = SystemSettings(
        event_log_path=log_path,
        snapshot_path=".agenthicc/snapshot.json",
    )
    state = AppState.create(settings=settings, policy=SecurityPolicy())

    # Restore from log when resuming
    if resume_id:
        log_file = _get_session_log_path(resume_id)
        if log_file and log_file.exists():
            state = await restore_from_log(str(log_file), state, root_reducer)
        _touch_session(resume_id)
    else:
        _register_session(session_id)

    processor = EventProcessor(initial_state=state, persist=True)
    model = TranscriptModel()
    adapter = TUIEventAdapter(model)
    adapter.subscribe_to(processor)

    # Load config and build LLM transport for agent runs
    cfg = load_config(cli_overrides=cli_overrides or [])
    try:
        llm_cfg = build_llm_config(cfg.execution)
    except ValueError as exc:
        from rich.console import Console  # noqa: PLC0415
        Console().print(f"[red]LLM config error: {exc}[/red]\n"
                        "[dim]Set ANTHROPIC_API_KEY or OPENAI_API_KEY env var, or use --set execution.provider=...[/dim]",
                        markup=True)
        llm_cfg = None

    renderer = InlineRenderer(
        model, adapter,
        base_path=os.getcwd(),
        history_file=".agenthicc/history",
    )
    renderer._processor = processor
    renderer._status.session_id = (
        f"{cfg.execution.provider}/{cfg.execution.effective_model()}"
    )
    renderer._status.resume_id = session_id
    renderer._loaded_config = cfg   # used by ConfigurationMenu

    renderer._active_agent = "default"
    renderer._exec_cfg = cfg.execution  # expose to _run_agent_turn for @mention config

    # ── skills discovery ──────────────────────────────────────────────
    from agenthicc.skills.loader import discover_skills as _discover_skills
    _skills = _discover_skills(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    renderer._skills = _skills

    # ── tool plugin discovery ─────────────────────────────────────────────
    from agenthicc.plugins.discovery import discover_project_tools, warn_conflicts  # noqa: PLC0415
    _project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    warn_conflicts(_project_plugins)
    renderer._project_plugin_tools = _project_plugins.all_tools
    if _project_plugins.all_tools:
        from rich.console import Console as _C  # noqa: PLC0415
        _C().print(f"[dim]Loaded {len(_project_plugins.all_tools)} plugin "
                   f"tool(s) from .agenthicc/tools/[/dim]")

    # ── MCP server initialisation ─────────────────────────────────────────
    _mcp_registry = None
    if cfg.tools.mcp_servers:
        try:
            from agenthicc.tools.mcp import McpToolRegistry  # noqa: PLC0415
            _mcp_registry = McpToolRegistry(event_processor=processor)
            for srv_cfg in cfg.tools.mcp_servers:
                _mcp_registry.register_server(srv_cfg)
            discovered = await _mcp_registry.discover_all()
            if discovered:
                from rich.console import Console as _Con  # noqa: PLC0415
                _Con().print(f"[dim]MCP: {len(discovered)} tool(s) from {len(cfg.tools.mcp_servers)} server(s)[/dim]")
            renderer._mcp_registry = _mcp_registry
        except Exception as exc:  # noqa: BLE001
            import logging as _log  # noqa: PLC0415
            _log.getLogger(__name__).error("MCP init failed: %s", exc)

    # ── @mention cache ────────────────────────────────────────────────────
    from agenthicc.mentions.cache import MentionCache  # noqa: PLC0415
    _mention_cache = MentionCache()
    renderer._mention_cache = _mention_cache

    # ── conversation store + memory ───────────────────────────────────────
    from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
    _session_memory = ShortTermMemory(max_tokens=32_000)
    conv_store = ConversationStore()
    _turn_index = [conv_store.next_turn_index(session_id)]

    # On resume: replay history into the transcript and restore LLM memory
    if resume_id:
        import shutil as _sh  # noqa: PLC0415
        from rich.console import Console as _Con  # noqa: PLC0415
        from rich.markdown import Markdown as _Md  # noqa: PLC0415
        from rich.rule import Rule  # noqa: PLC0415
        _con = _Con(highlight=False, markup=True)
        _cols = _sh.get_terminal_size((80, 24)).columns
        _con.print(Rule(f"[dim]resumed session {resume_id[:12]}[/dim]"))
        past_turns = conv_store.load_turns(resume_id)
        for turn in past_turns[-20:]:  # display last 20 Q&A pairs
            if "user" in turn:
                _con.print(f"[dim]❯ {turn['user']}[/dim]")
                _con.print(f"[dim]{'─' * _cols}[/dim]")
            if "assistant" in turn:
                ms = turn.get("model_short", "assistant")
                ts = turn.get("timestamp", time.time())
                hhmmss = time.strftime("%H:%M:%S", time.localtime(ts))
                _con.print(
                    f"[bold cyan]●[/] [bold]assistant ({ms})[/]  [dim]{hhmmss}[/dim]"
                )
                _con.print(_Md(turn["assistant"]), highlight=False)
        # Restore LLM context so the agent remembers past turns
        snapshot = conv_store.load_memory_snapshot(resume_id)
        if snapshot:
            _session_memory.restore(snapshot)

    # Build the lauren-ai runner that calls the LLM
    agent_runner = _build_agent_runner(llm_cfg, transcript=model)

    _MD_SENTINEL = InlineRenderer._MD_SENTINEL
    _pending_queue: list[str] = []   # FIFO queue for messages submitted while agent runs

    async def on_intent(text: str) -> None:
        from rich.markup import escape as _markup_escape  # noqa: PLC0415

        async def _run_one(user_text: str, turn_idx: int) -> None:
            """Save user turn, run agent, save assistant turn for one message."""
            conv_store.save_turn(session_id, turn_idx, "user", user_text, time.time())
            turns_before = len(model.turns)
            await _run_agent_turn(
                user_text, agent_runner, model, renderer, processor,
                session_memory=_session_memory,
                max_agent_turns=cfg.execution.max_agent_turns,
                pending_queue=_pending_queue,
            )
            if len(model.turns) > turns_before:
                last = model.turns[-1]
                content = "\n".join(
                    ln.replace("\x00md\x00", "") for ln in last.lines
                ).strip()
                ms = last.agent_name.replace("assistant (", "").rstrip(")")
                conv_store.save_turn(session_id, turn_idx, "assistant", content,
                                     time.time(), model_short=ms)

        await _run_one(text, _turn_index[0])
        _turn_index[0] += 1

        # Drain messages that arrived while the agent was running, one at a time.
        try:
            while _pending_queue:
                next_text = _pending_queue.pop(0)
                renderer.console.print(
                    f"[bold green]❯[/bold green] {_markup_escape(next_text)}",
                    markup=True, highlight=False,
                )
                renderer._flush_new_lines()
                await _run_one(next_text, _turn_index[0])
                _turn_index[0] += 1
        except asyncio.CancelledError:
            _pending_queue.clear()  # discard unprocessed messages on interrupt
            raise

        # Persist memory once after the whole burst is drained (not per-turn).
        conv_store.save_memory_snapshot(session_id, _session_memory.snapshot())

    # Start ad rotator for free-tier authenticated users
    ad_task: asyncio.Task | None = None
    try:
        from agenthicc.auth import AuthClient, NotLoggedInError  # noqa: PLC0415
        from agenthicc.ads import AdRotator  # noqa: PLC0415
        auth_client = AuthClient()
        bundle = auth_client.current_bundle()
        if bundle is not None and not bundle.is_pro:
            rotator = AdRotator(auth_client=auth_client, processor=processor)
            ad_task = asyncio.create_task(rotator.run())
    except Exception:
        pass  # ads never block startup

    proc_task = asyncio.create_task(processor.run())
    try:
        await renderer.run(on_intent)
    finally:
        proc_task.cancel()
        if ad_task is not None:
            ad_task.cancel()
        await asyncio.gather(proc_task, *(([ad_task] if ad_task else [])), return_exceptions=True)
        conv_store.close()
        if _mcp_registry is not None:
            await _mcp_registry.shutdown()


async def _run_tui_session_v2(
    resume_id: str | None = None,
    cli_overrides: list[str] | None = None,
) -> None:
    """New conversation-centric reactive runtime (PRD-58 to PRD-67).

    Uses the new Workspace (always-on Live block), ScrollBufferAppender,
    UnifiedInputSession, and ConversationStore while bridging into the
    existing agent runner infrastructure.
    """
    from agenthicc.kernel import AppState as KAppState, Event, EventProcessor, SecurityPolicy, SystemSettings
    from agenthicc.kernel.reducer import root_reducer
    from agenthicc.kernel.processor import restore_from_log
    from agenthicc.tui.transcript import TranscriptModel
    from agenthicc.tui.events import TUIEventAdapter
    from agenthicc.config import load_config, build_llm_config
    from agenthicc.conversation_store import ConversationStore as LegacyConvStore
    from rich.console import Console                                           # noqa: PLC0415

    # ── new reactive state ────────────────────────────────────────────────────
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession, InputMode
    from agenthicc.tui.runtime import (                                       # noqa: PLC0415
        EventBus, CommandBus, TaskManager, ModeManager,
        SendMessageCommand, InterruptAgentCommand, RunBuiltinCommand,
    )
    from agenthicc.tui.runtime.session_log import (                           # noqa: PLC0415
        create_session_id, register_session, touch_session,
        find_latest_session_for_cwd as _new_find_latest,
        SessionEventLog,
    )

    session_id = resume_id or create_session_id()

    # ── kernel setup (same as v1) ─────────────────────────────────────────────
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")
    settings = SystemSettings(
        event_log_path=log_path,
        snapshot_path=".agenthicc/snapshot.json",
    )
    k_state   = KAppState.create(settings=settings, policy=SecurityPolicy())
    if resume_id:
        log_file = _get_session_log_path(resume_id)
        if log_file and log_file.exists():
            k_state = await restore_from_log(str(log_file), k_state, root_reducer)
        _touch_session(resume_id)
    else:
        _register_session(session_id)

    processor = EventProcessor(initial_state=k_state, persist=True)
    model     = TranscriptModel()
    adapter   = TUIEventAdapter(model)
    adapter.subscribe_to(processor)

    # ── config / LLM ─────────────────────────────────────────────────────────
    cfg = load_config(cli_overrides=cli_overrides or [])
    try:
        llm_cfg = build_llm_config(cfg.execution)
    except ValueError as exc:
        Console().print(
            f"[red]LLM config error: {exc}[/red]\n"
            "[dim]Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or use --set execution.provider=...[/dim]",
            markup=True,
        )
        llm_cfg = None

    # ── new reactive app state ────────────────────────────────────────────────
    app_state = AppState.create()
    model_label = f"{cfg.execution.provider}/{cfg.execution.effective_model()}"
    app_state.conversation.model_name.set(model_label)
    app_state.conversation.session_id.set(session_id)

    # ── new session event log ─────────────────────────────────────────────────
    new_session_log = SessionEventLog(session_id)
    app_state.conversation.on_event(new_session_log.append)

    # ── workspace (always-on Live block) ──────────────────────────────────────
    # Use ONE console shared by both Workspace and old_renderer so there is
    # only one writer to stdout.  This prevents cursor-position desync.
    console   = Console(highlight=False, markup=True, force_terminal=True)
    workspace = Workspace(app_state, console)
    _shared_console = console

    # ── runtime services ──────────────────────────────────────────────────────
    event_bus    = EventBus()
    command_bus  = CommandBus()
    task_manager = TaskManager()
    mode_manager = ModeManager()
    mode_manager.set_by_name("Auto")

    # ── skills / plugins / MCP (same as v1) ───────────────────────────────────
    from agenthicc.skills.loader import discover_skills as _ds   # noqa: PLC0415
    _skills = _ds(project_dir=Path(".agenthicc"), user_dir=Path.home() / ".agenthicc")

    from agenthicc.plugins.discovery import discover_project_tools, warn_conflicts  # noqa: PLC0415
    _project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"), user_dir=Path.home() / ".agenthicc",
    )
    warn_conflicts(_project_plugins)

    _mcp_registry = None
    if cfg.tools.mcp_servers:
        try:
            from agenthicc.tools.mcp import McpToolRegistry  # noqa: PLC0415
            _mcp_registry = McpToolRegistry(event_processor=processor)
            for srv_cfg in cfg.tools.mcp_servers:
                _mcp_registry.register_server(srv_cfg)
            await _mcp_registry.discover_all()
        except Exception:  # noqa: BLE001
            pass

    from agenthicc.mentions.cache import MentionCache  # noqa: PLC0415
    _mention_cache = MentionCache()

    from lauren_ai._memory import ShortTermMemory     # noqa: PLC0415
    _session_memory = ShortTermMemory(max_tokens=32_000)
    legacy_conv     = LegacyConvStore()
    _turn_index     = [legacy_conv.next_turn_index(session_id)]

    # ── trigger registry ──────────────────────────────────────────────────────
    from agenthicc.tui.trigger import TriggerRegistry              # noqa: PLC0415
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger # noqa: PLC0415
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger  # noqa: PLC0415
    from agenthicc.commands import build_builtin_registry, CommandDispatcher  # noqa: PLC0415
    _cmd_registry = build_builtin_registry()
    _trigger_registry = TriggerRegistry()
    _trigger_registry.register(AtMentionTrigger())
    _trigger_registry.register(SlashCommandTrigger(_cmd_registry))
    _cmd_dispatcher = CommandDispatcher(_cmd_registry)

    # ── build legacy agent runner ─────────────────────────────────────────────
    agent_runner = _build_agent_runner(llm_cfg, transcript=model)

    # ── bridge: old bus events → new ConversationStore ────────────────────────
    # We reuse the existing AgenthiccTUI as a facade for the old bus wiring
    # but disable its direct console.print() calls (ScrollBufferAppender handles rendering).
    from agenthicc.tui.tui import AgenthiccTUI as _OldTUI  # noqa: PLC0415  (used for isinstance check in _run_agent_turn fallback)
    # _run_agent_turn now accepts conv_store= and publishes directly to ConversationStore.
    # No bridge needed — the old EventBus / AgenthiccTUI event handlers are bypassed entirely.
    # We still need a minimal "renderer" object so _run_agent_turn can access plugin tools,
    # skills, MCP registry, and mention cache via getattr.
    from types import SimpleNamespace as _NS  # noqa: PLC0415
    old_renderer = _NS(
        # Required by _run_agent_turn for agent configuration
        _exec_cfg             = cfg.execution,
        _skills               = _skills,
        _project_plugin_tools = _project_plugins.all_tools,
        _mcp_registry         = _mcp_registry,
        _mention_cache        = _mention_cache,
        _active_agent         = "default",
        # _status shim (used only in old-path; ignored when conv_store is set)
        _status               = _NS(
            session_id="", resume_id=session_id,
            completed_agents=0, input_tokens=0, output_tokens=0,
            session_cost_usd=0.0, active=False,
        ),
        # bus still needed because processor.emit goes through it for kernel events
        bus                   = _NS(publish=lambda e: None),
    )

    # ── resume: show previous conversation ───────────────────────────────────
    if resume_id:
        import shutil as _sh  # noqa: PLC0415
        from rich.rule import Rule  # noqa: PLC0415
        _cols = _sh.get_terminal_size((80, 24)).columns
        console.print(Rule(f"[dim]resumed session {resume_id[:12]}[/dim]"))
        past_turns = legacy_conv.load_turns(resume_id)
        for turn in past_turns[-10:]:
            if "user" in turn:
                console.print(f"[dim]❯ {turn['user']}[/dim]")
            if "assistant" in turn:
                ms  = turn.get("model_short", "assistant")
                ts  = turn.get("timestamp", time.time())
                hms = time.strftime("%H:%M:%S", time.localtime(ts))
                console.print(
                    f"[bold cyan]●[/bold cyan] [bold]{ms}[/bold]  [dim]{hms}[/dim]",
                    markup=True,
                )
                from rich.markdown import Markdown as _Md  # noqa: PLC0415
                console.print(_Md(turn["assistant"]), highlight=False)
        snapshot = legacy_conv.load_memory_snapshot(resume_id)
        if snapshot:
            _session_memory.restore(snapshot)

    # ── intent handler ────────────────────────────────────────────────────────
    input_session = UnifiedInputSession(
        app_state=app_state,
        command_bus=command_bus,
        trigger_registry=_trigger_registry,
        mode_manager=mode_manager,
        overlay_host=workspace.overlays,
        cwd=Path(os.getcwd()),
        cfg=cfg,
        history=[],
    )

    _pending_queue: list[str] = []
    _agent_task: asyncio.Task | None = None   # background task for the current agent turn

    async def _run_agent_for_text(text: str) -> None:
        """Run one agent turn (called from the background task, not inline)."""
        input_session.set_mode(InputMode.STREAMING)
        legacy_conv.save_turn(session_id, _turn_index[0], "user", text, time.time())
        turns_before = len(model.turns)
        try:
            await _run_agent_turn(
                text, agent_runner, model, old_renderer, processor,
                session_memory=_session_memory,
                max_agent_turns=cfg.execution.max_agent_turns,
                pending_queue=_pending_queue,
                conv_store=app_state.conversation,
            )
        finally:
            input_session.set_mode(InputMode.IDLE)

        if len(model.turns) > turns_before:
            last    = model.turns[-1]
            content = "\n".join(
                ln.replace("\x00md\x00", "") for ln in last.lines
            ).strip()
            ms = last.agent_name.replace("assistant (", "").rstrip(")")
            legacy_conv.save_turn(
                session_id, _turn_index[0], "assistant",
                content, time.time(), model_short=ms,
            )
        _turn_index[0] += 1

        # Drain queued messages
        while _pending_queue:
            next_text = _pending_queue.pop(0)
            await _run_agent_for_text(next_text)

        legacy_conv.save_memory_snapshot(session_id, _session_memory.snapshot())

    async def _agent_task_body(text: str) -> None:
        """Background task wrapper: run agent, clear task ref when done."""
        nonlocal _agent_task
        try:
            await _run_agent_for_text(text)
        except asyncio.CancelledError:
            # Agent was interrupted (ESC / Ctrl+C) — ensure clean state
            app_state.conversation.end_turn()
            input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            app_state.conversation.fail_turn(str(exc))
            input_session.set_mode(InputMode.IDLE)
        finally:
            _agent_task = None

    # Wire SendMessageCommand — spawns agent as a background task so the
    # UnifiedInputSession continues reading keystrokes (ESC / Ctrl+C work).
    async def _handle_send(cmd: SendMessageCommand) -> None:
        nonlocal _agent_task
        if _agent_task and not _agent_task.done():
            return  # already running; queue handled inside _run_agent_for_text
        _agent_task = asyncio.create_task(
            _agent_task_body(cmd.text), name="agent-turn"
        )

    def _handle_interrupt(cmd: InterruptAgentCommand) -> None:
        nonlocal _agent_task
        if _agent_task and not _agent_task.done():
            _agent_task.cancel()

    def _handle_builtin(cmd: RunBuiltinCommand) -> None:
        from agenthicc.commands import CommandContext  # noqa: PLC0415
        ctx = CommandContext(
            text=f"/{cmd.name}",
            args=cmd.args,
            model=old_renderer._model,
            console=console,
            renderer=old_renderer,
            config=cfg,
            session_id=session_id,
        )
        if cmd.name == "config":
            from agenthicc.tui.workspace.overlays.config_menu import ConfigMenuOverlay  # noqa: PLC0415
            workspace.overlays.show(
                ConfigMenuOverlay(cfg=cfg, on_close=workspace.overlays.hide)
            )
        else:
            _cmd_dispatcher.dispatch(f"/{cmd.name}", ctx)

    command_bus.register(SendMessageCommand,    _handle_send)
    command_bus.register(InterruptAgentCommand, _handle_interrupt)
    command_bus.register(RunBuiltinCommand,     _handle_builtin)

    # ── start workspace + kernel ──────────────────────────────────────────────
    workspace.start()
    proc_task = asyncio.create_task(processor.run())
    ad_task: asyncio.Task | None = None
    try:
        from agenthicc.auth import AuthClient  # noqa: PLC0415
        from agenthicc.ads import AdRotator   # noqa: PLC0415
        auth_client = AuthClient()
        bundle = auth_client.current_bundle()
        if bundle is not None and not bundle.is_pro:
            rotator = AdRotator(auth_client=auth_client, processor=processor)
            ad_task = asyncio.create_task(rotator.run())
    except Exception:
        pass

    # ── tick loop (advances animation frames) ─────────────────────────────────
    async def _tick() -> None:
        while True:
            await asyncio.sleep(0.05)
            app_state.conversation.tick()

    tick_task = asyncio.create_task(_tick())

    try:
        # ── main input loop ───────────────────────────────────────────────────
        # Session info (session ID, turns, cost, tokens) is shown directly in
        # the Live block status bar — no need to print it to the scroll buffer.
        await input_session.run()
        # input_session.run() only returns when the user exits (Ctrl+C twice)

    finally:
        tick_task.cancel()
        proc_task.cancel()
        if ad_task:
            ad_task.cancel()
        await asyncio.gather(
            tick_task, proc_task,
            *(([ad_task] if ad_task else [])),
            return_exceptions=True,
        )
        workspace.stop()
        new_session_log.close()
        legacy_conv.close()
        if _mcp_registry:
            await _mcp_registry.shutdown()


# Alias so tests can patch _run_tui_session and _run_tui picks up the patch.
# By default the alias points to the new reactive runtime (v2).
# Tests that monkeypatch this name still work correctly.
_run_tui_session = _run_tui_session_v2


def _run_tui(args: argparse.Namespace) -> None:
    try:
        from rich.console import Console  # noqa: F401
    except ImportError:
        print(
            "error: TUI requires rich:\n"
            "  pip install agenthicc\n"
            "Or run headless: agenthicc --headless",
            file=sys.stderr,
        )
        sys.exit(1)

    cli_overrides = getattr(args, "set_overrides", [])

    resume_id: str | None = None
    if args.resume:
        resume_id = args.resume
    elif args.continue_session:
        resume_id = _find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(_run_tui_session(resume_id=resume_id, cli_overrides=cli_overrides))
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Final-exit terminal reset — runs regardless of how the TUI exited.
        # This is the last line of defence against any raw_mode cleanup that
        # didn't finish (race conditions, exceptions, signal delivery order).
        # We explicitly re-enable ECHO + ICANON and reset all terminal attributes
        # so the user's original shell session is completely usable.
        _reset_terminal_on_exit()
