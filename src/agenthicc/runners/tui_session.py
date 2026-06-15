"""TUI session — starts the reactive runtime (PRD-58 to PRD-67)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _reset_terminal_on_exit() -> None:
    try:
        sys.stdout.write("\x1b[m\x1b[?2004l\x1b[?25h")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        import termios as _tm
        settings = _tm.tcgetattr(0)
        settings[3] |= _tm.ECHO | _tm.ICANON | _tm.ISIG
        _tm.tcsetattr(0, _tm.TCSAFLUSH, settings)
    except Exception:  # noqa: BLE001
        pass


from agenthicc.tui.runtime.session_log import (   # noqa: E402
    create_session_id, register_session, touch_session,
    find_latest_session_for_cwd, get_session_log_path, SessionEventLog,
)
from agenthicc.runners.agent_builder import _build_agent_runner  # noqa: E402
from agenthicc.runners.agent_turn import _run_agent_turn         # noqa: E402


_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"

# Module-level aliases so tests that monkeypatch these names on the module work.
_find_latest_session_for_cwd = find_latest_session_for_cwd
# _run_tui_session is used as the entry point; tests may monkeypatch it.
# (It IS _run_tui_session at module level — the alias just makes it explicit.)


async def _run_tui_session(
    resume_id: str | None = None,
    cli_overrides: list[str] | None = None,
) -> None:
    """Reactive TUI session — single entry point, no legacy branches."""
    from rich.console import Console                              # noqa: PLC0415
    from agenthicc.kernel import (                               # noqa: PLC0415
        AppState as KAppState, EventProcessor,
        SecurityPolicy, SystemSettings,
    )
    from agenthicc.kernel.reducer import root_reducer            # noqa: PLC0415
    from agenthicc.kernel.processor import restore_from_log     # noqa: PLC0415
    from agenthicc.config import load_config, build_llm_config  # noqa: PLC0415
    from agenthicc.tui.conversation_store import AppState       # noqa: PLC0415
    from agenthicc.tui.workspace import Workspace               # noqa: PLC0415
    from agenthicc.tui.input.unified_session import (           # noqa: PLC0415
        UnifiedInputSession, InputMode,
    )
    from agenthicc.tui.runtime import (                         # noqa: PLC0415
        CommandBus, ModeManager,
        SendMessageCommand, InterruptAgentCommand,
    )

    # ── session ID ────────────────────────────────────────────────────────────
    session_id = resume_id or create_session_id()
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # ── kernel (event sourcing + tool execution) ──────────────────────────────
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")
    k_state  = KAppState.create(
        settings=SystemSettings(event_log_path=log_path,
                                snapshot_path=".agenthicc/snapshot.json"),
        policy=SecurityPolicy(),
    )
    if resume_id:
        lf = get_session_log_path(resume_id)
        if lf and lf.exists():
            k_state = await restore_from_log(str(lf), k_state, root_reducer)
        touch_session(resume_id)
    else:
        register_session(session_id, os.getcwd(), "")

    processor = EventProcessor(initial_state=k_state, persist=True)

    # ── config / LLM ─────────────────────────────────────────────────────────
    cfg = load_config(cli_overrides=cli_overrides or [])
    try:
        llm_cfg = build_llm_config(cfg.execution)
    except ValueError as exc:
        Console().print(
            f"[red]LLM config error: {exc}[/red]\n"
            "[dim]Set ANTHROPIC_API_KEY or OPENAI_API_KEY, or --set execution.provider=...[/dim]",
            markup=True,
        )
        llm_cfg = None

    model_label = f"{cfg.execution.provider}/{cfg.execution.effective_model()}"

    # ── reactive state ────────────────────────────────────────────────────────
    app_state = AppState.create()
    app_state.conversation.model_name.set(model_label)
    app_state.conversation.session_id.set(session_id)

    session_log = SessionEventLog(session_id)
    app_state.conversation.on_event(session_log.append)

    # ── console + workspace ───────────────────────────────────────────────────
    console   = Console(highlight=False, markup=True, force_terminal=True)
    workspace = Workspace(app_state, console)

    # ── runtime services ──────────────────────────────────────────────────────
    command_bus  = CommandBus()
    mode_manager = ModeManager(app_state=app_state)   # PRD-75: writes app_state.active_mode
    mode_manager.set_by_name("Auto")

    # PRD-78: approval service — coordinates tool approval between agent and TUI.
    from agenthicc.tools.approval import ApprovalService  # noqa: PLC0415
    approval_svc = ApprovalService(app_state)

    # ── skills / plugins ─────────────────────────────────────────────────────
    from agenthicc.skills.loader import discover_skills as _ds      # noqa: PLC0415
    _skills = _ds(project_dir=Path(".agenthicc"),
                  user_dir=Path.home() / ".agenthicc")

    from agenthicc.plugins.discovery import (                       # noqa: PLC0415
        discover_project_tools, warn_conflicts, _scan_directory,
    )
    _project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"), user_dir=Path.home() / ".agenthicc",
    )
    warn_conflicts(_project_plugins)
    if _project_plugins.all_tools:
        console.print(
            f"[dim]Loaded {len(_project_plugins.all_tools)} project tool(s) from .agenthicc/tools/[/dim]"
        )

    # ── command plugins ───────────────────────────────────────────────────────
    _cmd_plugin_results = (
        _scan_directory(Path.home() / ".agenthicc" / "commands")
        + _scan_directory(Path(".agenthicc") / "commands")
    )
    _project_commands = [cmd for r in _cmd_plugin_results for cmd in r.commands]

    # ── MCP ───────────────────────────────────────────────────────────────────
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

    from lauren_ai._memory import ShortTermMemory      # noqa: PLC0415
    _session_memory = ShortTermMemory(max_tokens=32_000)

    # ── command registry + trigger registry ──────────────────────────────────
    from agenthicc.tui.trigger import TriggerManager                     # noqa: PLC0415
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger # noqa: PLC0415
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger  # noqa: PLC0415
    from agenthicc.commands import build_builtin_registry, CommandDispatcher  # noqa: PLC0415
    from agenthicc.commands.command import Command as _Cmd         # noqa: PLC0415

    _cmd_registry = build_builtin_registry()
    for _spec in _project_commands:
        try:
            # If the plugin already exported a full Command (with handler), use it
            # directly.  Only wrap bare CommandSpec completion-hint objects.
            if isinstance(_spec, _Cmd):
                _cmd_registry.register(_spec)
            else:
                _cmd_registry.register(_Cmd(
                    name=_spec.name,
                    description=_spec.description,
                    aliases=tuple(getattr(_spec, "aliases", ())),
                    argument_hint=getattr(_spec, "argument_hint", ""),
                    group=getattr(_spec, "group", "Project"),
                    source_id="plugin",
                ))
        except Exception:  # noqa: BLE001
            pass
    if _project_commands:
        console.print(
            f"[dim]Loaded {len(_project_commands)} project command(s) from .agenthicc/commands/[/dim]"
        )

    # Register each discovered skill as a slash command so /slug is invocable.
    from agenthicc.commands.builtins import _make_skill_handler  # noqa: PLC0415
    for _slug, _skill in _skills.items():
        try:
            _cmd_registry.register(_Cmd(
                name=f"/{_slug}",
                description=_skill.description or _skill.name,
                argument_hint="[args…]",
                group="Skills",
                handler=_make_skill_handler(_slug, _skill),
                source_id=f"skill:{_slug}",
            ))
        except Exception:  # noqa: BLE001
            pass

    _trigger_registry = TriggerManager()
    _trigger_registry.register(AtMentionTrigger())
    _trigger_registry.register(SlashCommandTrigger(_cmd_registry))
    _cmd_dispatcher = CommandDispatcher(_cmd_registry)

    # ── agent runner ──────────────────────────────────────────────────────────
    agent_runner = _build_agent_runner(llm_cfg, transcript=None)

    # ── resume: show previous context ────────────────────────────────────────
    if resume_id:
        from rich.rule import Rule  # noqa: PLC0415
        console.print(Rule(f"[dim]resumed session {session_id[:12]}[/dim]"))
        # Restore LLM short-term memory
        from agenthicc.conversation_store import ConversationStore as _LCS  # noqa: PLC0415
        _lcs = _LCS()
        snap = _lcs.load_memory_snapshot(resume_id)
        if snap:
            _session_memory.restore(snap)
        _lcs.close()

    # ── pending skill body (set by skill command handlers, consumed by _run_turn) ─
    _pending_skill_body: list[str] = []

    def _set_pending_skill(body: str) -> None:
        _pending_skill_body.clear()
        _pending_skill_body.append(body)

    # ── command dispatch helper ───────────────────────────────────────────────
    def _dispatch_slash(text: str) -> bool:
        """Dispatch a slash command. Returns True if handled."""
        from agenthicc.commands import CommandContext  # noqa: PLC0415

        ctx = CommandContext(
            text=text,
            args=" ".join(text.split()[1:]),
            model=model_label,
            console=console,
            config=cfg,
            session_id=session_id,
            skills=_skills,
            command_registry=_cmd_registry,
            mode_manager=mode_manager,
            set_pending_skill=_set_pending_skill,
            set_pending_menu=workspace.overlays.show,
            close_overlay=workspace.overlays.hide,
        )
        return bool(_cmd_dispatcher.dispatch(text, ctx))

    # ── agent task plumbing ───────────────────────────────────────────────────
    _agent_task: asyncio.Task | None = None
    _turn_count = [0]

    async def _run_turn(text: str) -> None:
        input_session.set_mode(InputMode.STREAMING)
        approval_svc.reset_turn_memory()   # PRD-78: clear per-turn blanket approvals
        # Prepend any queued skill body (from /skillname commands)
        if _pending_skill_body:
            text = _pending_skill_body.pop() + "\n\n" + text
        try:
            await _run_agent_turn(
                text, agent_runner, None,   # transcript=None (not used)
                None,                        # renderer=None (not used)
                processor,
                session_memory=_session_memory,
                max_agent_turns=cfg.execution.max_agent_turns,
                conv_store=app_state.conversation,
                app_state=app_state,
                exec_cfg=cfg.execution,
                skills=_skills,
                mention_cache=_mention_cache,
                project_plugin_tools=_project_plugins.all_tools,
                mcp_registry=_mcp_registry,
                active_agent="default",
                completed_turns=_turn_count[0],
                approval_svc=approval_svc,  # PRD-78
            )
        finally:
            input_session.set_mode(InputMode.IDLE)
            _turn_count[0] += 1
            # Persist memory after each turn
            try:
                from agenthicc.conversation_store import ConversationStore as _LCS2  # noqa: PLC0415
                _lcs2 = _LCS2()
                _lcs2.save_memory_snapshot(session_id, _session_memory.snapshot())
                _lcs2.close()
            except Exception:  # noqa: BLE001
                pass

    # ── single message queue (state machine) ─────────────────────────────────
    # All messages — whether submitted immediately or queued during streaming —
    # enter via SendMessageCommand and are held here until the agent is free.
    _msg_queue: list[str] = []

    def _route(msg: str) -> bool:
        """Route one message through the full pipeline.

        Slash commands are dispatched immediately (synchronously).
        Returns True if the message was a slash command and is fully handled.
        Returns False if it is regular text that should become an agent turn.
        """
        if not msg.startswith("/"):
            return False
        if _dispatch_slash(msg):
            return True
        cmd_name = msg.split()[0]
        if _cmd_registry.get(cmd_name) is not None:
            console.print(
                f"  [dim]Command [bold]{cmd_name}[/bold] has no handler. "
                f"Add a handler in [bold].agenthicc/commands/[/bold][/dim]"
            )
        return True  # never forward slash commands to the agent

    def _advance() -> None:
        """Start the next agent turn from _msg_queue (synchronous, no await).

        Drains leading slash commands immediately, then starts an agent task
        for the first regular message.  Clears the ⌛ Queued notification once
        the queue is empty.  Safe to call from a finally block: no awaits, so
        asyncio cannot interleave another coroutine between _agent_task = None
        and the new task creation — the race condition is structurally impossible.
        """
        nonlocal _agent_task
        while _msg_queue:
            msg = _msg_queue.pop(0).strip()
            if not msg:
                continue
            if _route(msg):
                continue
            # First regular message: show in transcript and start agent.
            app_state.conversation.notification.set(None)
            app_state.conversation.append_event("user_message", {"text": msg})
            _agent_task = asyncio.create_task(_agent_task_body(msg), name="agent-turn")
            return
        # Queue exhausted — clear any residual notification.
        app_state.conversation.notification.set(None)

    async def _agent_task_body(text: str) -> None:
        nonlocal _agent_task
        try:
            await _run_turn(text)
        except asyncio.CancelledError:
            app_state.conversation.end_turn()
            input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            app_state.conversation.fail_turn(str(exc))
            input_session.set_mode(InputMode.IDLE)
        finally:
            _agent_task = None
            _advance()

    async def _handle_send(cmd: SendMessageCommand) -> None:
        nonlocal _agent_task
        text = cmd.text.strip()
        if not text:
            return

        # Agent is busy — queue for later and show confirmation.
        if _agent_task and not _agent_task.done():
            _msg_queue.append(text)
            label = text[:40] + ("…" if len(text) > 40 else "")
            app_state.conversation.notification.set(f"⌛ Queued: {label}")
            return

        # Agent is free — route immediately (slash dispatch or agent turn).
        if _route(text):
            # Skill commands deposit a body in _pending_skill_body and return True.
            # Start the agent turn immediately so the skill executes on one Enter press.
            if _pending_skill_body:
                body = _pending_skill_body.pop()
                app_state.conversation.append_event("user_message", {"text": text})
                _agent_task = asyncio.create_task(_agent_task_body(body), name="agent-turn")
            return
        app_state.conversation.append_event("user_message", {"text": text})
        _agent_task = asyncio.create_task(_agent_task_body(text), name="agent-turn")

    def _handle_interrupt(cmd: InterruptAgentCommand) -> None:
        nonlocal _agent_task
        if _agent_task and not _agent_task.done():
            _agent_task.cancel()

    command_bus.register(SendMessageCommand,    _handle_send)
    command_bus.register(InterruptAgentCommand, _handle_interrupt)

    # ── input session ─────────────────────────────────────────────────────────
    input_session = UnifiedInputSession(
        app_state=app_state,
        command_bus=command_bus,
        trigger_registry=_trigger_registry,
        mode_manager=mode_manager,
        overlay_host=workspace.overlays,
        cwd=Path(os.getcwd()),
        cfg=cfg,
    )

    # ── PRD-78: wire pending_approval signal → ApprovalOverlay ───────────────
    def _on_approval_change() -> None:
        req = app_state.pending_approval()
        from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay  # noqa: PLC0415
        if req is not None:
            overlay = ApprovalOverlay(req, approval_svc, workspace.overlays.hide)
            workspace.overlays.show(overlay)
        else:
            if isinstance(workspace.overlays.widget, ApprovalOverlay):
                workspace.overlays.hide()

    app_state.pending_approval.subscribe(_on_approval_change)

    # ── start ─────────────────────────────────────────────────────────────────
    workspace.start()
    proc_task = asyncio.create_task(processor.run())
    ad_task: asyncio.Task | None = None
    try:
        from agenthicc.auth import AuthClient  # noqa: PLC0415
        from agenthicc.ads import AdRotator   # noqa: PLC0415
        auth  = AuthClient()
        bndl  = auth.current_bundle()
        if bndl is not None and not bndl.is_pro:
            ad_task = asyncio.create_task(
                AdRotator(auth_client=auth, processor=processor).run()
            )
    except Exception:  # noqa: BLE001
        pass

    async def _tick() -> None:
        while True:
            await asyncio.sleep(0.05)
            app_state.conversation.tick()

    tick_task = asyncio.create_task(_tick())

    try:
        await input_session.run()
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
        session_log.close()
        if _mcp_registry:
            await _mcp_registry.shutdown()


def _run_tui(args: argparse.Namespace) -> None:
    try:
        from rich.console import Console  # noqa: F401
    except ImportError:
        print("error: TUI requires rich — pip install agenthicc", file=sys.stderr)
        sys.exit(1)

    cli_overrides = getattr(args, "set_overrides", [])
    resume_id: str | None = None
    if getattr(args, "resume", None):
        resume_id = args.resume
    elif getattr(args, "continue_session", False):
        resume_id = find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(_run_tui_session(resume_id=resume_id, cli_overrides=cli_overrides))
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        _reset_terminal_on_exit()
