"""TUI session — starts the reactive runtime (PRD-58 to PRD-67, PRD-93)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._config import LLMConfig
    from lauren_ai._signals import AgentRunComplete
    from agenthicc.cli.context import CLIContext, CLIFlags
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import SendMessageCommand, InterruptAgentCommand
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.workflows.plugin import WorkflowDefinition, WorkflowContext

def _make_session_tools(
    approval_svc: ApprovalService | None,
    memory_router: MemoryRouter | None = None,
    semantic_index: SemanticIndex | None = None,
) -> list:
    """Tools injected into every interactive agent turn (Auto mode + plan phase)."""
    from agenthicc.workflows.phase_tools import make_questions_tool  # noqa: PLC0415
    from agenthicc.workflows.memory_tools import make_memory_tools   # noqa: PLC0415
    return (
        make_questions_tool(approval_svc)
        + make_memory_tools(memory_router, semantic_index)
    )


def _build_agent_runner(llm_cfg: LLMConfig | None, *, cassette_dir: Path | None = None) -> AgentRunnerBase | None:
    """Build a lauren-ai AgentRunnerBase wired to a SignalBus."""
    if llm_cfg is None:
        return None
    from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
    from lauren_ai._module import _build_transport          # noqa: PLC0415
    from lauren_ai._signals import SignalBus                # noqa: PLC0415

    transport = _build_transport(llm_cfg)
    if cassette_dir is not None:
        from agenthicc.testing.recording_transport import RecordingTransport  # noqa: PLC0415
        cassette_dir.mkdir(parents=True, exist_ok=True)
        transport = RecordingTransport(transport, cassette_dir / "cassette.jsonl")
    return AgentRunnerBase(transport=transport, signals=SignalBus())


def _fmt_exc(exc: BaseException) -> str:
    """Format an exception as 'ExceptionType: message' for scroll-buffer display.

    Never returns a bare ``str(exc)`` — the exception class name is always
    included so users can identify the failure type (e.g. ``ReadTimeout``).
    """
    name = type(exc).__name__
    msg  = str(exc).strip()
    return f"{name}: {msg}" if msg else name


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
    find_latest_session_for_cwd, SessionEventLog,
)
from agenthicc.runners.agent_turn import _run_agent_turn         # noqa: E402
from agenthicc.runners.session_context import SessionContext      # noqa: E402


_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"

# Module-level alias so tests that monkeypatch this name on the module work.
_find_latest_session_for_cwd = find_latest_session_for_cwd


# ── session construction ──────────────────────────────────────────────────────

async def _build_session_context(
    resume_id: str | None,
    cli_overrides: list[str] | None,
    record_cassette_dir: Path | None = None,
) -> SessionContext:
    """Construct all session-scoped singletons and return a SessionContext."""
    from rich.console import Console                              # noqa: PLC0415
    from agenthicc.kernel import (                               # noqa: PLC0415
        AppState as KAppState, EventProcessor,
        SecurityPolicy, SystemSettings,
    )
    from agenthicc.kernel.reducer import root_reducer            # noqa: PLC0415
    from agenthicc.kernel.processor import restore_from_log     # noqa: PLC0415
    from agenthicc.config import load_config, build_llm_config  # noqa: PLC0415
    from agenthicc.tui.conversation_store import AppState       # noqa: PLC0415
    from agenthicc.tui.runtime import (                         # noqa: PLC0415
        CommandBus, ModeManager,
    )

    # ── session ID ────────────────────────────────────────────────────────────
    session_id = resume_id or create_session_id()
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # ── cassette dir: <base>/<session_id>/ ───────────────────────────────────
    cassette_dir: Path | None = (
        record_cassette_dir / session_id if record_cassette_dir is not None else None
    )
    if cassette_dir is not None:
        cassette_dir.mkdir(parents=True, exist_ok=True)

    # ── kernel ────────────────────────────────────────────────────────────────
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")
    k_state  = KAppState.create(
        settings=SystemSettings(event_log_path=log_path,
                                snapshot_path=".agenthicc/snapshot.json"),
        policy=SecurityPolicy(),
    )
    if resume_id:
        # log_path already points to the kernel event log (sessions/<id>.jsonl).
        # get_session_log_path() returns the TUI conversation log
        # (sessions/<id>/conversation.jsonl) — a completely different file that
        # restore_from_log cannot parse, producing "skipping corrupt event log line".
        kernel_log = Path(log_path)
        if kernel_log.exists():
            k_state = await restore_from_log(log_path, k_state, root_reducer)
        touch_session(resume_id)
    else:
        register_session(session_id, os.getcwd(), "")

    processor = EventProcessor(initial_state=k_state, persist=True)

    # ── config / LLM ─────────────────────────────────────────────────────────
    cfg = load_config(cli_overrides=cli_overrides or [])

    # PRD-108: configure shared HTTP client timeout from config before any tool runs.
    from agenthicc.tools.http import configure as _configure_http  # noqa: PLC0415
    _configure_http(cfg.tools.http_timeout_s)

    console = Console(highlight=False, markup=True, force_terminal=True)
    try:
        llm_cfg = build_llm_config(cfg.execution)
    except ValueError as exc:
        console.print(
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

    # ── runtime services ──────────────────────────────────────────────────────
    command_bus = CommandBus()

    from agenthicc.tools.approval import ApprovalService         # noqa: PLC0415
    approval_svc: ApprovalService = ApprovalService(app_state)
    if cassette_dir is not None:
        from agenthicc.testing.recording_approval import RecordingApprovalService  # noqa: PLC0415
        approval_svc = RecordingApprovalService(
            approval_svc, cassette_dir / "approvals.jsonl"
        )

    # ── workflow + agents registries ──────────────────────────────────────────
    from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415
    from agenthicc.agents.registry import build_agents_registry      # noqa: PLC0415
    workflow_registry = build_workflow_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    agents_registry = build_agents_registry(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )

    # ── mode manager ──────────────────────────────────────────────────────────
    mode_manager = ModeManager(
        app_state=app_state,
        default_map=workflow_registry.mode_default_map(),
        available_map=workflow_registry.mode_available_map(),
    )
    mode_manager.set_by_name("Auto")

    # ── skills / plugins ─────────────────────────────────────────────────────
    from agenthicc.skills.bootstrap import bootstrap_default_skills  # noqa: PLC0415
    from agenthicc.skills.loader import discover_skills as _ds       # noqa: PLC0415

    _skill_global_dir = (
        Path(cfg.skills.default_skill_directory).expanduser()
        if cfg.skills.default_skill_directory
        else Path.home() / ".agenthicc"
    )
    _n_installed = bootstrap_default_skills(
        global_dir=_skill_global_dir,
        enabled=cfg.skills.install_default_skills,
    )
    if _n_installed:
        console.print(
            f"[dim]Installed {_n_installed} default skill(s).[/dim]",
            markup=True,
        )

    skills = _ds(project_dir=Path(".agenthicc"),
                 user_dir=_skill_global_dir)

    from agenthicc.plugins.discovery import (                        # noqa: PLC0415
        discover_project_tools, warn_conflicts, _scan_directory,
    )
    project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"), user_dir=Path.home() / ".agenthicc",
    )
    warn_conflicts(project_plugins)
    if project_plugins.all_tools:
        console.print(
            f"[dim]Loaded {len(project_plugins.all_tools)} project tool(s) from .agenthicc/tools/[/dim]"
        )

    # ── command plugins ───────────────────────────────────────────────────────
    _cmd_plugin_results = (
        _scan_directory(Path.home() / ".agenthicc" / "commands")
        + _scan_directory(Path(".agenthicc") / "commands")
    )
    project_commands = [cmd for r in _cmd_plugin_results for cmd in r.commands]

    # ── MCP ───────────────────────────────────────────────────────────────────
    mcp_registry = None
    if cfg.tools.mcp_servers:
        try:
            from agenthicc.tools.mcp import McpToolRegistry     # noqa: PLC0415
            mcp_registry = McpToolRegistry(event_processor=processor)
            for srv_cfg in cfg.tools.mcp_servers:
                mcp_registry.register_server(srv_cfg)
            await mcp_registry.discover_all()
        except Exception:  # noqa: BLE001
            pass

    from agenthicc.mentions.cache import MentionCache            # noqa: PLC0415
    mention_cache = MentionCache()

    from lauren_ai._memory import ShortTermMemory                # noqa: PLC0415
    session_memory = ShortTermMemory(max_tokens=32_000)

    # ── three-tier memory (PRD-101) ───────────────────────────────────────────
    from agenthicc.memory.layers import (                        # noqa: PLC0415
        ProjectMemoryLayer, GlobalMemoryLayer, SessionMemoryLayer,
    )
    from agenthicc.memory.router import MemoryRouter             # noqa: PLC0415
    from agenthicc.memory.vector import SemanticIndex            # noqa: PLC0415
    _project_memory = ProjectMemoryLayer(Path(".agenthicc") / "memory" / "project.db")
    _global_memory  = GlobalMemoryLayer()
    _session_layer  = SessionMemoryLayer()
    _memory_router  = MemoryRouter(
        session_layer=_session_layer,
        project_layer=_project_memory,
        global_layer=_global_memory,
    )
    _semantic_index = SemanticIndex()

    # ── command registry + trigger registry ──────────────────────────────────
    from agenthicc.tui.trigger import TriggerManager                      # noqa: PLC0415
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger        # noqa: PLC0415
    from agenthicc.tui.triggers.slash_command import SlashCommandTrigger  # noqa: PLC0415
    from agenthicc.commands import build_builtin_registry                  # noqa: PLC0415
    from agenthicc.commands.command import Command as _Cmd                 # noqa: PLC0415

    cmd_registry = build_builtin_registry()
    for _spec in project_commands:
        try:
            if isinstance(_spec, _Cmd):
                cmd_registry.register(_spec)
            else:
                cmd_registry.register(_Cmd(
                    name=_spec.name,
                    description=_spec.description,
                    aliases=tuple(getattr(_spec, "aliases", ())),
                    argument_hint=getattr(_spec, "argument_hint", ""),
                    group=getattr(_spec, "group", "Project"),
                    source_id="plugin",
                ))
        except Exception:  # noqa: BLE001
            pass
    if project_commands:
        console.print(
            f"[dim]Loaded {len(project_commands)} project command(s) from .agenthicc/commands/[/dim]"
        )

    from agenthicc.commands.builtins import _make_skill_handler  # noqa: PLC0415
    for _slug, _skill in skills.items():
        try:
            cmd_registry.register(_Cmd(
                name=f"/{_slug}",
                description=_skill.description or _skill.name,
                argument_hint="[args…]",
                group="Skills",
                handler=_make_skill_handler(_slug, _skill),
                source_id=f"skill:{_slug}",
            ))
        except Exception:  # noqa: BLE001
            pass

    trigger_registry = TriggerManager()
    trigger_registry.register(AtMentionTrigger())
    trigger_registry.register(SlashCommandTrigger(cmd_registry))

    # ── agent runner ──────────────────────────────────────────────────────────
    agent_runner = _build_agent_runner(
        llm_cfg,
        cassette_dir=cassette_dir,
    )

    # ── PRD-83: AgentRunComplete reconciliation handler ───────────────────────
    # Per-run baseline captured just before each run; the handler uses it so
    # that set_tokens() produces the correct SESSION total (baseline + run).
    _runner_signals = getattr(agent_runner, "_signals", None)
    if _runner_signals is not None:
        from lauren_ai._signals import AgentRunComplete as _ARC  # noqa: PLC0415

        # Baseline tokens accumulated before the current run began.
        # Updated at run-start (AgentRunStarted) when that signal is available;
        # for now we update it optimistically at the end of each completed run
        # so the next run's baseline is correct.
        _baseline: list[tuple[int, int, float]] = [(0, 0, 0.0)]

        @_runner_signals.on(_ARC)
        async def _on_agent_run_complete(sig: AgentRunComplete) -> None:
            usage = getattr(sig, "total_usage", None)
            cost  = float(getattr(sig, "total_cost_usd", 0.0) or 0.0)
            if usage is not None:
                inp = int(getattr(usage, "input_tokens", 0) or 0)
                out = int(getattr(usage, "output_tokens", 0) or 0)
                base_inp, base_out, base_cost = _baseline[0]
                # Authoritative session total = pre-run baseline + this run's total.
                app_state.conversation.set_tokens(
                    base_inp + inp,
                    base_out + out,
                    base_cost + cost,
                )
                # Advance baseline for the next run.
                _baseline[0] = (base_inp + inp, base_out + out, base_cost + cost)

    # ── resume: restore previous context ─────────────────────────────────────
    if resume_id:
        from rich.rule import Rule  # noqa: PLC0415
        console.print(Rule(f"[dim]resumed session {session_id[:12]}[/dim]"))
        from agenthicc.conversation_store import ConversationStore as _LCS  # noqa: PLC0415
        _lcs = _LCS()
        snap = _lcs.load_memory_snapshot(resume_id)
        if snap:
            session_memory.restore(snap)
        _lcs.close()

    return SessionContext(
        processor=processor,
        app_state=app_state,
        session_log=session_log,
        approval_svc=approval_svc,
        mode_manager=mode_manager,
        command_bus=command_bus,
        workflow_registry=workflow_registry,
        agents_registry=agents_registry,
        cmd_registry=cmd_registry,
        trigger_registry=trigger_registry,
        agent_runner=agent_runner,
        session_memory=session_memory,
        mention_cache=mention_cache,
        skills=skills,
        project_plugins=project_plugins,
        mcp_registry=mcp_registry,
        cfg=cfg,
        session_id=session_id,
        model_label=model_label,
        console=console,
        memory_router=_memory_router,
        semantic_index=_semantic_index,
    )


# ── TUISession ────────────────────────────────────────────────────────────────

class TUISession:
    """All TUI session behaviour — methods correspond to the former nested closures."""

    def __init__(
        self,
        ctx: "SessionContext",
        workspace: "Workspace",
        input_session: "UnifiedInputSession",
    ) -> None:
        self._ctx           = ctx
        self._workspace     = workspace
        self._input_session = input_session

        # Mutable session state
        self._pending_skill_body: list[str]           = []
        self._msg_queue:          list[str]           = []
        self._agent_task:         asyncio.Task | None = None
        self._turn_count:         int                 = 0
        self._pending_replay_id:  str | None          = None

        from agenthicc.commands import CommandDispatcher          # noqa: PLC0415
        from agenthicc.workflows.config import WorkflowConfig      # noqa: PLC0415
        self._cmd_dispatcher = CommandDispatcher(ctx.cmd_registry)
        # Built once per session; completed_turns is updated per run via replace().
        self._wf_config_base = WorkflowConfig(
            conv_store=ctx.app_state.conversation,
            app_state=ctx.app_state,
            processor=ctx.processor,
            agent_runner=ctx.agent_runner,
            approval_svc=ctx.approval_svc,
            cfg=ctx.cfg,
            skills=ctx.skills,
            plugin_tools=ctx.project_plugins.all_tools,
            mcp_registry=ctx.mcp_registry,
            mention_cache=ctx.mention_cache,
            agents_registry=ctx.agents_registry,
            memory_router=ctx.memory_router,
            semantic_index=ctx.semantic_index,
        )

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_pending_skill(self, body: str) -> None:
        self._pending_skill_body.clear()
        self._pending_skill_body.append(body)

    def _set_pending_replay(self, session_id: str) -> None:
        self._pending_replay_id = session_id

    def _wire_approval_overlay(self) -> None:
        workspace    = self._workspace
        approval_svc = self._ctx.approval_svc
        app_state    = self._ctx.app_state

        def _on_approval_change() -> None:
            req = app_state.pending_approval()
            from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay           # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay  # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay         # noqa: PLC0415

            # Registry maps ApprovalRequest.kind → overlay class.
            # Add new overlay kinds by extending this dict — no if/elif needed.
            _overlay_registry = {
                "plan_review": PlanApprovalOverlay,
                "questions":   QuestionsOverlay,
            }
            _overlay_default = ApprovalOverlay

            if req is not None:
                kind    = getattr(req, "kind", "tool")
                factory = _overlay_registry.get(kind, _overlay_default)
                workspace.overlays.show(factory(req, approval_svc, workspace.overlays.hide))
            else:
                if isinstance(workspace.overlays.widget, tuple(_overlay_registry.values()) + (_overlay_default,)):
                    workspace.overlays.hide()

        app_state.pending_approval.subscribe(_on_approval_change)

    # ── public routing ────────────────────────────────────────────────────────

    def dispatch_slash(self, text: str) -> bool:
        """Dispatch a slash command. Returns True if handled."""
        from agenthicc.commands import CommandContext  # noqa: PLC0415
        ctx = self._ctx
        context = CommandContext(
            text=text,
            args=" ".join(text.split()[1:]),
            model=ctx.model_label,
            console=ctx.console,
            config=ctx.cfg,
            session_id=ctx.session_id,
            skills=ctx.skills,
            command_registry=ctx.cmd_registry,
            mode_manager=ctx.mode_manager,
            set_pending_skill=self._set_pending_skill,
            set_pending_menu=self._workspace.overlays.show,
            close_overlay=self._workspace.overlays.hide,
            set_pending_replay=self._set_pending_replay,
        )
        return bool(self._cmd_dispatcher.dispatch(text, context))

    def route(self, msg: str) -> bool:
        """Return True if msg is a slash command and was dispatched."""
        if not msg.startswith("/"):
            return False
        if self.dispatch_slash(msg):
            # Check if a replay was requested by the command handler.
            if self._pending_replay_id:
                replay_id = self._pending_replay_id
                self._pending_replay_id = None
                self._agent_task = asyncio.create_task(
                    self._run_replay(replay_id), name="replay"
                )
            return True
        cmd_name = msg.split()[0]
        if self._ctx.cmd_registry.get(cmd_name) is not None:
            self._ctx.console.print(
                f"  [dim]Command [bold]{cmd_name}[/bold] has no handler. "
                f"Add a handler in [bold].agenthicc/commands/[/bold][/dim]"
            )
        return True  # never forward slash commands to the agent

    def advance(self) -> None:
        """Drain _msg_queue: dispatch slash commands, start next agent task."""
        while self._msg_queue:
            msg = self._msg_queue.pop(0).strip()
            if not msg:
                continue
            if self.route(msg):
                continue
            self._ctx.app_state.conversation.notification.set(None)
            self._ctx.app_state.conversation.append_event("user_message", {"text": msg})
            self._agent_task = asyncio.create_task(
                self.agent_task_body(msg), name="agent-turn"
            )
            return
        self._ctx.app_state.conversation.notification.set(None)

    # ── agent turn plumbing ───────────────────────────────────────────────────

    async def run_turn(self, text: str) -> None:
        """Dispatch one user message: workflow or direct agent turn."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415
        ctx = self._ctx

        self._input_session.set_mode(InputMode.STREAMING)
        ctx.approval_svc.reset_turn_memory()

        if self._pending_skill_body:
            text = self._pending_skill_body.pop() + "\n\n" + text

        _active_wf_name = ctx.app_state.active_mode().default_workflow
        _wf_defn = ctx.workflow_registry.get(_active_wf_name) if _active_wf_name else None

        _timeout = ctx.cfg.execution.turn_timeout_s

        async def _run_inner() -> None:
            if _wf_defn is not None:
                import dataclasses as _dc  # noqa: PLC0415
                # PRD-111: build per-workflow params from merged TOML/CLI/env config.
                _wf_params = _wf_defn.build_params(
                    ctx.cfg.workflows.get(_wf_defn.name, {})
                )
                _wf_config = _dc.replace(
                    self._wf_config_base,
                    completed_turns=self._turn_count,
                    params=_wf_params,
                )
                # Each WorkflowDefinition carries its own runner_factory (PRD-110).
                # No name-based branching here — the plugin owns the runner choice.
                _wf_runner = _wf_defn.build_runner(_wf_config, ctx.mode_manager)
                await _wf_runner.run(text)
                # PRD-89: exit workflow-bound mode after successful completion
                _wf_result = ctx.app_state.workflow_run()
                if (
                    _wf_result is not None
                    and getattr(_wf_result, "status", None) == "complete"
                    and ctx.app_state.active_mode().default_workflow is not None
                ):
                    ctx.mode_manager.set_by_name("Auto")
                    ctx.app_state.conversation.notification.set(
                        "✓ Workflow complete — switched to Auto mode"
                    )
            else:
                await _run_agent_turn(
                    text, ctx.agent_runner, ctx.processor,
                    session_memory=ctx.session_memory,
                    max_agent_turns=ctx.cfg.execution.max_agent_turns,
                    conv_store=ctx.app_state.conversation,
                    app_state=ctx.app_state,
                    exec_cfg=ctx.cfg.execution,
                    skills=ctx.skills,
                    mention_cache=ctx.mention_cache,
                    project_plugin_tools=(
                        ctx.project_plugins.all_tools
                        + _make_session_tools(
                            ctx.approval_svc,
                            memory_router=ctx.memory_router,
                            semantic_index=ctx.semantic_index,
                        )
                    ),
                    mcp_registry=ctx.mcp_registry,
                    active_agent="default",
                    completed_turns=self._turn_count,
                    approval_svc=ctx.approval_svc,
                    memory_router=ctx.memory_router,
                    semantic_index=ctx.semantic_index,
                )

        try:
            if _timeout and _timeout > 0:
                await asyncio.wait_for(_run_inner(), timeout=_timeout)
            else:
                await _run_inner()
        except asyncio.TimeoutError:
            ctx.app_state.conversation.close_turn(
                error=(
                    f"TimeoutError: Turn timed out after {_timeout:.0f}s — "
                    "the agent or a tool may be stuck on a slow network call."
                )
            )
        finally:
            self._input_session.set_mode(InputMode.IDLE)
            self._turn_count += 1
            try:
                from agenthicc.conversation_store import ConversationStore as _LCS  # noqa: PLC0415
                _lcs = _LCS()
                _lcs.save_memory_snapshot(ctx.session_id, ctx.session_memory.snapshot())
                _lcs.close()
            except Exception:  # noqa: BLE001
                pass

    async def agent_task_body(self, text: str) -> None:
        """Wrap run_turn with error handling; advance queue on completion."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415
        conv = self._ctx.app_state.conversation
        try:
            await self.run_turn(text)
        except asyncio.CancelledError:
            # close_turn() is idempotent — inner layers may have already called it.
            conv.close_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            # Only emit an error event if the turn is still open; if _stream()
            # already closed it (via its own finally), this is a no-op.
            conv.close_turn(
                error=_fmt_exc(exc) if conv.is_turn_active else None
            )
            self._input_session.set_mode(InputMode.IDLE)
        finally:
            self._agent_task = None
            self.advance()

    async def handle_send(self, cmd: "SendMessageCommand") -> None:
        """Route user message: slash → command dispatcher, text → agent."""
        text = cmd.text.strip()
        if not text:
            return

        if self._agent_task and not self._agent_task.done():
            self._msg_queue.append(text)
            label = text[:40] + ("…" if len(text) > 40 else "")
            self._ctx.app_state.conversation.notification.set(f"⌛ Queued: {label}")
            return

        if self.route(text):
            if self._pending_skill_body:
                body = self._pending_skill_body.pop()
                self._ctx.app_state.conversation.append_event("user_message", {"text": text})
                self._agent_task = asyncio.create_task(
                    self.agent_task_body(body), name="agent-turn"
                )
            return
        self._ctx.app_state.conversation.append_event("user_message", {"text": text})
        self._agent_task = asyncio.create_task(self.agent_task_body(text), name="agent-turn")

    def handle_interrupt(self, cmd: "InterruptAgentCommand") -> None:
        """Cancel the current agent task if one is running."""
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()

    # ── workflow resume (PRD-94) ──────────────────────────────────────────────

    async def _run_replay(self, session_id: str) -> None:
        """Replay a historical session's conversation through the render pipeline."""
        from agenthicc.tui.input.unified_session import InputMode     # noqa: PLC0415
        from agenthicc.tui.runtime.replay import ConversationReplayer  # noqa: PLC0415
        ctx = self._ctx

        # Enter Replay mode — blocks all tool capabilities during replay.
        prior_mode = ctx.app_state.active_mode()
        ctx.mode_manager.set_by_name("Replay")
        self._input_session.set_mode(InputMode.STREAMING)

        try:
            replayer = ConversationReplayer(
                session_id=session_id,
                conv_store=ctx.app_state.conversation,
                mode_manager=ctx.mode_manager,
            )
            await replayer.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            ctx.app_state.conversation.notification.set("⏮ Replay cancelled.")
            raise
        except Exception as exc:
            ctx.app_state.conversation.notification.set(f"⏮ Replay error: {exc}")
        finally:
            ctx.app_state.active_mode.set(prior_mode)
            self._input_session.set_mode(InputMode.IDLE)
            self._agent_task = None
            self.advance()

    def _notify_incomplete_workflow(self) -> None:
        """If the kernel state has an unfinished workflow, notify the user.

        Does NOT auto-start the workflow.  On --resume the user should decide
        whether to continue — sending a message in Plan mode will start a fresh
        workflow run with their new intent.
        """
        from agenthicc.kernel.state import NodeStatus  # noqa: PLC0415
        k_state = self._ctx.processor.get_state()
        for wf in k_state.workflows.values():
            if wf.status in (NodeStatus.complete, NodeStatus.failed):
                continue
            if not wf.name:
                continue
            self._ctx.app_state.conversation.notification.set(
                f"Session had an in-progress '{wf.name}' workflow. "
                "Send a message to start a new run."
            )
            return

    async def _resume_workflow_task(self, wf_defn: WorkflowDefinition, context: WorkflowContext) -> None:
        """Resume a WorkflowRunner with error handling matching agent_task_body."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415
        ctx = self._ctx
        self._input_session.set_mode(InputMode.STREAMING)
        ctx.approval_svc.reset_turn_memory()
        try:
            import dataclasses as _dc  # noqa: PLC0415
            _wf_params = wf_defn.build_params(ctx.cfg.workflows.get(wf_defn.name, {}))
            _wf_config  = _dc.replace(self._wf_config_base, params=_wf_params)
            runner = wf_defn.build_runner(_wf_config, ctx.mode_manager)
            await runner.resume(context)
            # PRD-89: exit workflow-bound mode after completion
            _wf_result = ctx.app_state.workflow_run()
            if (
                _wf_result is not None
                and getattr(_wf_result, "status", None) == "complete"
                and ctx.app_state.active_mode().default_workflow is not None
            ):
                ctx.mode_manager.set_by_name("Auto")
                ctx.app_state.conversation.notification.set(
                    "✓ Workflow resumed and complete — switched to Auto mode"
                )
        except asyncio.CancelledError:
            ctx.app_state.conversation.close_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            conv = ctx.app_state.conversation
            conv.close_turn(
                error=_fmt_exc(exc) if conv.is_turn_active else None
            )
            self._input_session.set_mode(InputMode.IDLE)
        finally:
            self._agent_task = None
            self.advance()

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start tasks, run input session, tear down."""
        from agenthicc.tui.runtime import (  # noqa: PLC0415
            SendMessageCommand, InterruptAgentCommand,
        )
        ctx = self._ctx

        ctx.command_bus.register(SendMessageCommand,    self.handle_send)
        ctx.command_bus.register(InterruptAgentCommand, self.handle_interrupt)
        self._wire_approval_overlay()

        self._workspace.start()
        proc_task = asyncio.create_task(ctx.processor.run())
        # If a previous session had an in-progress workflow, show a notification
        # but do NOT auto-start it — the user decides what to do next.
        self._notify_incomplete_workflow()
        ad_task: asyncio.Task | None = None
        try:
            from agenthicc.auth import AuthClient  # noqa: PLC0415
            from agenthicc.ads import AdRotator   # noqa: PLC0415
            auth = AuthClient()
            bndl = auth.current_bundle()
            if bndl is not None and not bndl.is_pro:
                ad_task = asyncio.create_task(
                    AdRotator(auth_client=auth, processor=ctx.processor).run()
                )
        except Exception:  # noqa: BLE001
            pass

        async def _tick() -> None:
            while True:
                await asyncio.sleep(0.05)
                ctx.app_state.conversation.tick()

        tick_task = asyncio.create_task(_tick())
        try:
            await self._input_session.run()
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
            self._workspace.stop()


# ── thin factory (≤60 lines) ──────────────────────────────────────────────────

async def _run_tui_session(
    resume_id: str | None = None,
    cli_overrides: list[str] | None = None,
    record_cassette: str | None = None,
    cli_flags: CLIFlags | None = None,
) -> None:
    """Reactive TUI session — single entry point, no legacy branches."""
    from agenthicc.tui.workspace import Workspace                        # noqa: PLC0415
    from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415

    cassette_base: Path | None = Path(record_cassette) if record_cassette else None

    ctx = await _build_session_context(resume_id, cli_overrides, cassette_base)
    # PRD-79: stamp CLIFlags onto AppState immediately after creation; frozen for session lifetime.
    if cli_flags is not None:
        ctx.app_state.cli_flags = cli_flags
    workspace = Workspace(
        ctx.app_state, ctx.console,
        max_live_tool_calls=ctx.cfg.tools.max_live_tool_calls,
    )
    input_session = UnifiedInputSession(
        app_state=ctx.app_state,
        command_bus=ctx.command_bus,
        trigger_registry=ctx.trigger_registry,
        mode_manager=ctx.mode_manager,
        overlay_host=workspace.overlays,
        cwd=Path(os.getcwd()),
        cfg=ctx.cfg,
    )
    session = TUISession(ctx, workspace, input_session)
    try:
        from agenthicc.tui.welcome import print_welcome  # noqa: PLC0415
        print_welcome(
            ctx.console,
            model=ctx.model_label,
            cwd=os.getcwd(),
        )
        await session.run()
    finally:
        ctx.session_log.close()
        if ctx.mcp_registry:
            await ctx.mcp_registry.shutdown()
        if cassette_base is not None:
            _write_cassette_meta(cassette_base / ctx.session_id, ctx.session_id)


def _write_cassette_meta(cassette_dir: Path, session_id: str) -> None:
    """Write meta.json alongside the cassette files."""
    import json as _json
    from datetime import datetime, timezone
    meta = {
        "session_id": session_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "intent": "",   # filled in manually or from history
    }
    try:
        (cassette_dir / "meta.json").write_text(
            _json.dumps(meta, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


# ── sync entry point (unchanged) ─────────────────────────────────────────────

def _run_tui(ctx: CLIContext) -> None:
    try:
        from rich.console import Console  # noqa: F401
    except ImportError:
        print("error: TUI requires rich — pip install agenthicc", file=sys.stderr)
        sys.exit(1)

    # Crash-safe terminal restore (PRD-107, Layer 5).
    # Cover all exit paths: normal exit (finally below), atexit, SIGTERM, SIGHUP.
    import atexit    # noqa: PLC0415
    import signal as _signal  # noqa: PLC0415
    atexit.register(_reset_terminal_on_exit)

    def _sig_exit(signum: int, frame: object) -> None:
        _reset_terminal_on_exit()
        sys.exit(0)

    try:
        _signal.signal(_signal.SIGTERM, _sig_exit)
        _signal.signal(_signal.SIGHUP,  _sig_exit)
    except (AttributeError, OSError):
        pass   # Windows / non-TTY environments

    resume_id: str | None = ctx.resume_id
    if resume_id is None and ctx.continue_session:
        resume_id = find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(_run_tui_session(
            resume_id=resume_id,
            cli_overrides=list(ctx.set_overrides),
            record_cassette=ctx.record_cassette,
            cli_flags=ctx.flags,
        ))
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        _reset_terminal_on_exit()
