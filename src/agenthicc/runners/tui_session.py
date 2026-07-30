"""TUI session — starts the reactive runtime (PRD-58 to PRD-67, PRD-93)."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._config import LLMConfig
    from agenthicc.cli.context import CLIContext, CLIFlags
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import SendMessageCommand, InterruptAgentCommand
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.commands.command import Command
    from agenthicc.commands.command import UsageSnapshot
    from agenthicc.commands.busy_policy import BusyDecision
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.skills.loader import SkillDef, SkillDiscoveryResult
    from agenthicc.workflows.plugin import WorkflowPlugin
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.tools.base import ToolLike


def _make_session_tools(
    approval_svc: ApprovalService | None,
    memory_router: MemoryRouter | None = None,
    semantic_index: SemanticIndex | None = None,
) -> list[ToolLike]:
    """Tools injected into every interactive agent turn (Yolo mode + plan phase)."""
    from agenthicc.workflows.code_plan.phase_tools import make_questions_tool  # noqa: PLC0415
    from agenthicc.workflows.memory_tools import make_memory_tools  # noqa: PLC0415

    return make_questions_tool(approval_svc) + make_memory_tools(memory_router, semantic_index)


def _build_agent_runner(
    llm_cfg: LLMConfig | None, *, cassette_dir: Path | None = None
) -> AgentRunnerBase | None:
    """Build a lauren-ai AgentRunnerBase wired to a SignalBus."""
    if llm_cfg is None:
        return None
    from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
    from lauren_ai._module import _build_transport  # noqa: PLC0415
    from lauren_ai._signals import SignalBus  # noqa: PLC0415

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
    msg = str(exc).strip()
    return f"{name}: {msg}" if msg else name


def _build_skill_command(slug: str, skill: "SkillDef") -> "Command":
    """Build the dollar-prefixed command owned by one discovered skill."""
    from agenthicc.commands.builtins import _make_skill_handler  # noqa: PLC0415
    from agenthicc.commands.command import Command  # noqa: PLC0415

    return Command(
        name=f"${slug}",
        description=skill.description or skill.name,
        argument_hint="[args…]",
        group="Skills",
        handler=_make_skill_handler(slug, skill),
        aliases=tuple(f"${alias}" for alias in skill.aliases),
        source_id=f"skill:{slug}",
    )


def _register_skill_commands(
    registry: "UnifiedCommandRegistry",
    skills: "dict[str, SkillDef]",
) -> None:
    """Register the current skill commands in the unified command registry."""
    for slug, skill in skills.items():
        try:
            command = _build_skill_command(slug, skill)
            if any(registry.get(name) is not None for name in (command.name, *command.aliases)):
                continue
            registry.register(command)
        except Exception:  # noqa: BLE001
            # A malformed extension must not prevent the TUI from starting.
            pass


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


from agenthicc.tui.runtime.session_log import (  # noqa: E402
    create_session_id,
    register_session,
    update_session_mode,
    load_session_mode,
    touch_session,
    find_latest_session_for_cwd,
    SessionEventLog,
    load_user_message_history,
)
from agenthicc.runners.agent_turn import _run_agent_turn  # noqa: E402
from agenthicc.runners.session_context import SessionContext  # noqa: E402


_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"

# Module-level alias so tests that monkeypatch this name on the module work.
_find_latest_session_for_cwd = find_latest_session_for_cwd


# ── session construction ──────────────────────────────────────────────────────


async def _build_session_context(
    resume_id: str | None,
    cli_overrides: list[str] | None,
    record_cassette_dir: Path | None = None,
    config_path: str | None = None,
    headless: bool = False,
) -> SessionContext:
    """Construct all session-scoped singletons and return a SessionContext."""
    from rich.console import Console  # noqa: PLC0415
    from agenthicc.kernel import (  # noqa: PLC0415
        AppState as KAppState,
        EventProcessor,
        SecurityPolicy,
        SystemSettings,
    )
    from agenthicc.kernel.reducer import root_reducer  # noqa: PLC0415
    from agenthicc.kernel.processor import restore_from_log  # noqa: PLC0415
    from agenthicc.config import load_config, build_llm_config  # noqa: PLC0415
    from agenthicc.tui.conversation_store import AppState  # noqa: PLC0415
    from agenthicc.tui.runtime import (  # noqa: PLC0415
        CommandBus,
        ModeManager,
    )

    # ── session ID ────────────────────────────────────────────────────────────
    session_id = resume_id or create_session_id()
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # PRD-150: every client observes the same session projection.  The TUI
    # remains responsible for Rich/reactive presentation, while the service
    # owns the client-neutral snapshot and event cursor.
    from agenthicc.session_service import SessionService  # noqa: PLC0415

    session_service = SessionService()
    await session_service.ensure_session(
        session_id,
        project_root=Path.cwd(),
        capabilities=frozenset({"read", "control", "workspace"}),
    )

    # ── cassette dir: <base>/<session_id>/ ───────────────────────────────────
    cassette_dir: Path | None = (
        record_cassette_dir / session_id if record_cassette_dir is not None else None
    )
    if cassette_dir is not None:
        cassette_dir.mkdir(parents=True, exist_ok=True)

    # ── kernel ────────────────────────────────────────────────────────────────
    log_path = str(_SESSIONS_DIR / f"{session_id}.jsonl")
    k_state = KAppState.create(
        settings=SystemSettings(event_log_path=log_path, snapshot_path=".agenthicc/snapshot.json"),
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
    if resume_id:
        await session_service.import_kernel_log(session_id, log_path)
    kernel_event_queue = processor.subscribe_events()

    async def _project_kernel_events() -> None:
        while True:
            kernel_event = await kernel_event_queue.get()
            await session_service.publish_kernel_event(session_id, kernel_event)

    kernel_projection_task = asyncio.create_task(
        _project_kernel_events(), name=f"session-kernel-projection-{session_id}"
    )

    # ── config / LLM ─────────────────────────────────────────────────────────
    cfg = load_config(cli_overrides=cli_overrides or [], config_path=config_path)

    # PRD-149: terminal subprocesses are owned by a session-scoped manager.
    # Keep their registry alongside the existing background-session store but
    # in a separate namespace so agent sessions and terminal handles cannot be
    # confused with one another.
    from agenthicc.background.settings import load_background_settings  # noqa: PLC0415
    from agenthicc.background.terminals import TerminalManager  # noqa: PLC0415

    background_settings = load_background_settings(
        config_path=config_path,
        overrides=tuple(cli_overrides or ()),
        cwd=Path.cwd(),
    )
    terminal_root = (
        Path(background_settings.store_path).expanduser() / "terminals"
        if background_settings.store_path
        else None
    )
    terminal_manager = TerminalManager(
        session_id=session_id,
        cwd=Path.cwd(),
        store_root=terminal_root,
        enabled=(
            background_settings.enabled
            and background_settings.terminals_enabled
            and os.environ.get("AGENTHICC_DISABLE_BACKGROUND", "") != "1"
        ),
        max_terminals=background_settings.max_terminals,
        max_terminals_per_project=background_settings.max_terminals_per_project,
        max_output_bytes=background_settings.terminal_max_output_bytes,
        wall_timeout_s=background_settings.terminal_wall_timeout_s,
        cancel_grace_s=background_settings.terminal_cancel_grace_s,
        retention_days=background_settings.terminal_retention_days,
    )

    # PRD-108: configure shared HTTP client timeout from config before any tool runs.
    from agenthicc.tools.http import configure as _configure_http  # noqa: PLC0415

    _configure_http(cfg.tools.http_timeout_s)

    console = Console(
        highlight=False,
        markup=True,
        force_terminal=not headless,
        quiet=headless,
    )
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

    def _project_conversation_event(event: object) -> None:
        """Project TUI events without making the reactive store authoritative."""

        try:
            kind = getattr(event, "kind", "")
            payload = getattr(event, "payload", {})
            if not isinstance(kind, str) or not isinstance(payload, dict):
                return
            turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
            task = asyncio.create_task(
                session_service.publish(
                    session_id,
                    source="tui",
                    kind=kind,
                    payload=payload,
                    turn_id=turn_id,
                )
            )

            def _consume_projection_error(done: asyncio.Task[object]) -> None:
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(_consume_projection_error)
        except RuntimeError:
            # Construction-time events can occur before an event loop owns the
            # adapter. The kernel/session journal remains authoritative then.
            return

    app_state.conversation.on_event(_project_conversation_event)

    # ── runtime services ──────────────────────────────────────────────────────
    command_bus = CommandBus()

    from agenthicc.tools.approval import ApprovalService  # noqa: PLC0415

    approval_svc: ApprovalService = ApprovalService(app_state)
    if cassette_dir is not None:
        from agenthicc.testing.recording_approval import RecordingApprovalService  # noqa: PLC0415

        approval_svc = cast(
            "ApprovalService",
            RecordingApprovalService(approval_svc, cassette_dir / "approvals.jsonl"),
        )

    # ── workflow + agents registries ──────────────────────────────────────────
    from agenthicc.workflows.registry import build_workflow_registry  # noqa: PLC0415
    from agenthicc.agents.registry import build_agents_registry  # noqa: PLC0415

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
    # Safe is the default posture for every new session. Resume migrates legacy
    # names (Auto/Guard/etc.) through the canonical registry before the callback
    # is installed, so the persisted value is never overwritten prematurely.
    mode_manager.set_by_name("Safe")
    persisted_mode: str | None = None
    if resume_id:
        persisted_mode = load_session_mode(resume_id)
        if persisted_mode is not None and mode_manager.set_by_name(persisted_mode) is None:
            raise ValueError(
                f"Cannot resume session {resume_id!r}: unknown persisted mode "
                f"{persisted_mode!r}. Choose one of: "
                f"{', '.join(mode_manager.registry.selectable_names())}."
            )

    def _persist_mode(mode: object) -> None:
        name = getattr(mode, "name", "")
        if isinstance(name, str) and not mode_manager.registry.is_internal(name):
            update_session_mode(session_id, name)

    mode_manager.set_change_callback(_persist_mode)
    if resume_id and persisted_mode is not None:
        # Persist the canonical spelling after a successful alias migration;
        # future resumes never need to carry the legacy identity forward.
        update_session_mode(session_id, mode_manager.active_name)

    # ── skills / plugins ─────────────────────────────────────────────────────
    from agenthicc.skills.bootstrap import bootstrap_default_skills  # noqa: PLC0415
    from agenthicc.skills.loader import (  # noqa: PLC0415
        discover_skills_with_diagnostics,
    )

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

    skill_discovery = discover_skills_with_diagnostics(
        project_dir=Path(".agenthicc"),
        user_dir=_skill_global_dir,
    )
    skills = skill_discovery.skills
    for diagnostic in skill_discovery.diagnostics:
        if diagnostic.severity != "info":
            console.print(
                f"[yellow]Skill discovery: {diagnostic}[/yellow]",
                markup=True,
            )

    from agenthicc.plugins.discovery import (  # noqa: PLC0415
        discover_project_tools,
        warn_conflicts,
    )
    from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415

    project_plugins = discover_project_tools(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    warn_conflicts(project_plugins)
    if project_plugins.all_tools:
        console.print(
            f"[dim]Loaded {len(project_plugins.all_tools)} project tool(s) from .agenthicc/tools/[/dim]"
        )

    # ── command plugins ───────────────────────────────────────────────────────
    # Use the command-specific loader so COMMAND and COMMANDS exports share
    # one validated contract and can later be reloaded atomically.
    command_plugins = discover_command_plugins(
        project_dir=Path(".agenthicc"),
        user_dir=Path.home() / ".agenthicc",
    )
    project_commands = command_plugins.all_commands
    command_plugin_names = {command.name for command in project_commands}

    # ── MCP ───────────────────────────────────────────────────────────────────
    mcp_registry = None
    if cfg.tools.mcp_servers:
        try:
            from agenthicc.tools.mcp import McpToolRegistry  # noqa: PLC0415

            mcp_registry = McpToolRegistry(event_processor=processor)
            for srv_cfg in cfg.tools.mcp_servers:
                mcp_registry.register_server(srv_cfg)
            await mcp_registry.discover_all()
        except Exception:  # noqa: BLE001
            pass

    from agenthicc.mentions.cache import MentionCache  # noqa: PLC0415

    mention_cache = MentionCache()

    # PRD-129 Phase 2: durable conversation journal.  session_memory is a
    # JournaledShortTermMemory — every transition is fsync'd to a per-session
    # append-only journal, and on resume (session_id == resume_id) the journal
    # is folded straight back into memory.  This supersedes the old SQLite
    # memory-snapshot durability (which only checkpointed at turn boundaries).
    from agenthicc.runners.session_conversation import SessionConversation  # noqa: PLC0415

    session_conversation = SessionConversation.open(
        session_id,
        max_tokens=cfg.execution.effective_usable_budget(),
    )
    session_memory = session_conversation.memory

    # One browser manager and one opaque browser context per stable session
    # conversation.  The selected backend is side-effect free at construction;
    # its optional package/browser runtime is loaded only when a tool is used.
    if cfg.tools.browser_backend == "playwright":
        from agenthicc.tools.playwright import (  # noqa: PLC0415
            create_playwright_session,
            make_playwright_tools,
        )

        browser_manager = create_playwright_session(
            cfg.tools.playwright,
            conversation_id=session_conversation.conversation_id,
            workspace_root=Path.cwd(),
        )
        browser_tools = make_playwright_tools(browser_manager)
    elif cfg.tools.browser_backend == "cloakbrowser":
        from agenthicc.tools.cloakbrowser import (  # noqa: PLC0415
            create_browser_session,
            make_cloakbrowser_tools,
        )

        browser_manager = create_browser_session(
            cfg.tools.cloakbrowser,
            conversation_id=session_conversation.conversation_id,
            workspace_root=Path.cwd(),
        )
        browser_tools = make_cloakbrowser_tools(browser_manager)
    else:
        browser_manager = None
        browser_tools = []

    from agenthicc.runners.usage_ledger import UsageLedger  # noqa: PLC0415

    usage_ledger = UsageLedger.open(
        session_id,
        journal=session_conversation.journal,
        legacy_token_events=SessionEventLog.load(session_id),
        conversation_id=session_conversation.conversation_id,
    )
    usage_ledger.bind_conversation_store(app_state.conversation)

    # PRD-132 L1: install the durable, freshness-validated workspace file cache so
    # read_file resolves unchanged files from a per-project store instead of disk.
    if cfg.execution.file_cache:
        from agenthicc.tools.fs.file_cache import (  # noqa: PLC0415
            WorkspaceFileCache,
            configure_file_cache,
        )

        configure_file_cache(WorkspaceFileCache(Path(".agenthicc") / "cache" / "file-cache.db"))

    # ── three-tier memory (PRD-101) ───────────────────────────────────────────
    from agenthicc.memory.layers import (  # noqa: PLC0415
        ProjectMemoryLayer,
        GlobalMemoryLayer,
        SessionMemoryLayer,
    )
    from agenthicc.memory.router import MemoryRouter  # noqa: PLC0415
    from agenthicc.memory.vector import SemanticIndex  # noqa: PLC0415

    _project_memory = ProjectMemoryLayer(Path(".agenthicc") / "memory" / "project.db")
    _global_memory = GlobalMemoryLayer()
    _session_layer = SessionMemoryLayer()
    _memory_router = MemoryRouter(
        session_layer=_session_layer,
        project_layer=_project_memory,
        global_layer=_global_memory,
    )
    _semantic_index = SemanticIndex()

    # ── command registry + trigger registry ──────────────────────────────────
    from agenthicc.tui.trigger import TriggerManager  # noqa: PLC0415
    from agenthicc.tui.triggers.at_mention import AtMentionTrigger  # noqa: PLC0415
    from agenthicc.tui.triggers.slash_command import (  # noqa: PLC0415
        SkillTrigger,
        SlashCommandTrigger,
    )
    from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
    from agenthicc.commands.command import Command as _Cmd  # noqa: PLC0415

    cmd_registry = build_builtin_registry()
    for _spec in project_commands:
        try:
            if isinstance(_spec, _Cmd):
                cmd_registry.register(_spec)
            else:
                cmd_registry.register(
                    _Cmd(
                        name=_spec.name,
                        description=_spec.description,
                        aliases=tuple(getattr(_spec, "aliases", ())),
                        argument_hint=getattr(_spec, "argument_hint", ""),
                        group=getattr(_spec, "group", "Project"),
                        source_id="plugin",
                    )
                )
        except Exception:  # noqa: BLE001
            pass
    if project_commands:
        console.print(
            f"[dim]Loaded {len(project_commands)} project command(s) from .agenthicc/commands/[/dim]"
        )

    _register_skill_commands(cmd_registry, skills)

    trigger_registry = TriggerManager()
    trigger_registry.register(AtMentionTrigger())
    trigger_registry.register(SlashCommandTrigger(cmd_registry, workflow_registry))
    trigger_registry.register(SkillTrigger(cmd_registry))

    # ── agent runner ──────────────────────────────────────────────────────────
    agent_runner = _build_agent_runner(
        llm_cfg,
        cassette_dir=cassette_dir,
    )

    # ── resume: restore previous context ─────────────────────────────────────
    # PRD-129 Phase 2: prior context is restored by folding the durable journal
    # at construction time (session_id == resume_id), so no explicit load is
    # needed here — only the visual marker.
    #
    # PRD-129 Phase 3: if the prior session died mid-turn (a turn_started with no
    # turn_completed), build a ResumePlan so the session can re-drive that turn
    # from where it left off — replaying already-completed tools.
    pending_resume = None
    if resume_id:
        from rich.rule import Rule  # noqa: PLC0415

        console.print(Rule(f"[dim]resumed session {session_id[:12]}[/dim]"))
        from agenthicc.runners.run_coordinator import RunCoordinator  # noqa: PLC0415

        _incomplete = RunCoordinator.detect_incomplete_turn(session_conversation.journal)
        if _incomplete is not None:
            pending_resume = RunCoordinator.build_resume_plan(
                session_conversation.journal, _incomplete
            )

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
        agent_runner=cast("AgentRunnerBase", agent_runner),
        session_memory=session_memory,
        session_conversation=session_conversation,
        mention_cache=mention_cache,
        skills=skills,
        project_plugins=project_plugins,
        mcp_registry=mcp_registry,
        terminal_manager=terminal_manager,
        cfg=cfg,
        session_id=session_id,
        model_label=model_label,
        console=console,
        memory_router=_memory_router,
        semantic_index=_semantic_index,
        pending_resume=pending_resume,
        command_plugin_names=command_plugin_names,
        session_service=session_service,
        kernel_projection_task=kernel_projection_task,
        usage_ledger=usage_ledger,
        browser_manager=browser_manager,
        browser_tools=browser_tools,
        resumed=bool(resume_id),
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
        self._ctx = ctx
        self._workspace = workspace
        self._input_session = input_session

        # Mutable session state
        self._pending_skill_body: list[str] = []
        self._msg_queue: list[str] = []
        self._agent_task: asyncio.Task[object] | None = None
        self._last_submitted_text: str = ""
        self._turn_count: int = 0
        self._pending_replay_id: str | None = None
        self._workflow_override: str | None = None  # PRD-114: /workflow command
        self._workflow_handle: WorkflowRunHandle | None = None
        terminal_manager = getattr(ctx, "terminal_manager", None)
        if terminal_manager is not None:
            self._terminal_unsub = terminal_manager.changed.subscribe(self._sync_terminal_status)
        else:
            self._terminal_unsub = lambda: None

        from agenthicc.commands import CommandDispatcher  # noqa: PLC0415
        from agenthicc.workflows.config import WorkflowConfig  # noqa: PLC0415

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
            plugin_tools=ctx.project_plugins,
            mcp_registry=ctx.mcp_registry,
            mention_cache=ctx.mention_cache,
            agents_registry=ctx.agents_registry,
            memory_router=ctx.memory_router,
            semantic_index=ctx.semantic_index,
            session_memory=ctx.session_memory,
            conversation_id=ctx.session_id,
            usage_ledger=getattr(ctx, "usage_ledger", None),
            browser_manager=getattr(ctx, "browser_manager", None),
            browser_tools=list(getattr(ctx, "browser_tools", [])),
            next_queued_message=self._next_queued_message,
        )
        self._restore_paused_workflow()
        self._sync_terminal_status()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_pending_skill(self, body: str) -> None:
        self._pending_skill_body.clear()
        self._pending_skill_body.append(body)

    def _set_pending_replay(self, session_id: str) -> None:
        self._pending_replay_id = session_id

    def _finalize_returned_workflow(self) -> None:
        """Close a custom workflow handle whose runner returned normally.

        Built-in runners explicitly mark their handle terminal. Downstream
        runners are allowed to focus on their own state machine, so the session
        owner supplies this final lifecycle boundary as a safety net. A normal
        return cannot leave a run looking active; otherwise the next request
        could accidentally reuse its run id and overwrite its checkpoint.
        """
        handle = self._workflow_handle
        if handle is None or handle.lifecycle in {"complete", "failed", "discarded"}:
            return
        workflow_run = self._ctx.app_state.workflow_run()
        status = getattr(workflow_run, "status", None)
        terminal: Literal["complete", "failed"] = "failed" if status == "failed" else "complete"
        handle.mark_terminal(
            terminal,
            error="workflow returned unsuccessfully" if status == "failed" else "",
        )
        if handle.checkpoint_supported:
            try:
                handle.save_checkpoint(reason=terminal)
            except Exception:  # noqa: BLE001 — lifecycle cleanup must not mask the result
                handle.checkpoint_supported = False

    def _restore_paused_workflow(self) -> None:
        """Rehydrate the newest paused workflow checkpoint for this session."""
        conversation = getattr(self._ctx, "session_conversation", None)
        if conversation is None:
            return
        from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore  # noqa: PLC0415
        from agenthicc.runners.workflow_handle import WorkflowRunHandle  # noqa: PLC0415

        store = WorkflowCheckpointStore(self._ctx.session_id)
        candidates = []
        for run_id in store.list_run_ids():
            try:
                checkpoint = store.load(run_id)
            except Exception as exc:  # noqa: BLE001
                self._ctx.app_state.conversation.notify_transient(
                    f"⚠ Ignoring invalid workflow checkpoint {run_id}: {exc}"
                )
                continue
            if checkpoint is not None and checkpoint.status in {"paused", "pausing"}:
                candidates.append(checkpoint)
        if not candidates:
            return
        checkpoint = max(candidates, key=lambda item: item.created_at)
        definition = self._ctx.workflow_registry.get(checkpoint.workflow_name)
        if definition is None:
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ Cannot resume '{checkpoint.workflow_name}': workflow is not loaded"
            )
            return
        try:
            self._workflow_handle = WorkflowRunHandle.from_checkpoint(
                checkpoint,
                workflow=definition,
                conversation=conversation,
                checkpoint_store=store,
                browser_manager=getattr(self._ctx, "browser_manager", None),
            )
            self._ctx.app_state.conversation.notification.set(
                f"Workflow '{checkpoint.workflow_name}' is paused. Use /workflow resume or send a message to continue."
            )
        except Exception as exc:  # noqa: BLE001
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ Cannot restore workflow '{checkpoint.workflow_name}': {exc}"
            )

    def _publish_session_event(
        self,
        kind: str,
        payload: dict[str, object] | None = None,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Send a TUI lifecycle event to the client-neutral projection."""

        service = getattr(self._ctx, "session_service", None)
        if service is None:
            return
        try:
            task = asyncio.create_task(
                service.publish(
                    self._ctx.session_id,
                    source="tui",
                    kind=kind,
                    payload=payload or {},
                    turn_id=turn_id,
                )
            )
        except RuntimeError:
            return

        def _consume_projection_error(done: asyncio.Task[object]) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_consume_projection_error)

    def _wire_approval_overlay(self) -> None:
        workspace = self._workspace
        approval_svc = self._ctx.approval_svc
        app_state = self._ctx.app_state

        def _on_approval_change() -> None:
            req = app_state.pending_approval()
            from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay  # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay  # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.questions import QuestionsOverlay  # noqa: PLC0415

            # Registry maps ApprovalRequest.kind → overlay class.
            # Add new overlay kinds by extending this dict — no if/elif needed.
            _overlay_registry = {
                "plan_review": PlanApprovalOverlay,
                "questions": QuestionsOverlay,
            }
            _overlay_default = ApprovalOverlay

            if req is not None:
                kind = getattr(req, "kind", "tool")
                factory = _overlay_registry.get(kind, _overlay_default)
                workspace.overlays.show(factory(req, approval_svc, workspace.overlays.hide))
            else:
                if isinstance(
                    workspace.overlays.widget,
                    tuple(_overlay_registry.values()) + (_overlay_default,),
                ):
                    workspace.overlays.hide()

        app_state.pending_approval.subscribe(_on_approval_change)

    def _sync_terminal_status(self) -> None:
        """Project the current terminal wait into the reactive status bar."""

        terminal_manager = getattr(self._ctx, "terminal_manager", None)
        if terminal_manager is None:
            return
        snapshot = terminal_manager.wait_snapshot()
        conversation = self._ctx.app_state.conversation
        if snapshot is None:
            conversation.clear_terminal_wait()
            redraw = getattr(self._workspace, "_redraw", None)
            if callable(redraw):
                redraw()
            return
        elapsed_value = snapshot.get("elapsed_s", 0.0)
        count_value = snapshot.get("running_count", 0)
        conversation.set_terminal_wait(
            terminal_id=str(snapshot["terminal_id"]),
            label=str(snapshot["label"]),
            elapsed_s=float(elapsed_value) if isinstance(elapsed_value, (int, float)) else 0.0,
            running_count=int(count_value) if isinstance(count_value, int) else 0,
        )
        redraw = getattr(self._workspace, "_redraw", None)
        if callable(redraw):
            redraw()

    # ── public routing ────────────────────────────────────────────────────────

    def dispatch_slash(self, text: str) -> bool:
        """Dispatch a registered command or skill trigger."""
        from agenthicc.commands import CommandContext  # noqa: PLC0415
        from agenthicc.plugins.registry import build_registry  # noqa: PLC0415

        ctx = self._ctx
        project_tools: list[ToolLike] = list(ctx.project_plugins.all_tools)
        project_tools.extend(getattr(ctx, "browser_tools", []))
        if ctx.mcp_registry is not None:
            project_tools.extend(ctx.mcp_registry.all_tools())
        tool_registry = build_registry(project_plugin_tools=project_tools)
        context = CommandContext(
            text=text,
            args=" ".join(text.split()[1:]),
            model=ctx.model_label,
            console=ctx.console,
            config=ctx.cfg,
            session_id=ctx.session_id,
            skills=ctx.skills,
            active_agent="default",
            command_registry=ctx.cmd_registry,
            tools=tool_registry.tools,
            tool_sources=tool_registry.sources,
            workflow_registry=ctx.workflow_registry,
            mode_manager=ctx.mode_manager,
            terminal_manager=getattr(ctx, "terminal_manager", None),
            set_pending_skill=self._set_pending_skill,
            set_pending_menu=self._workspace.overlays.show,
            close_overlay=self._workspace.overlays.hide,
            set_pending_replay=self._set_pending_replay,
            reload_skills=self._reload_skills,
            reload_commands=self._reload_commands,
            reload_tools=self._reload_tools,
            reload_workflows=self._reload_workflows,
            set_input_text=self._set_input_text,
            usage_snapshot=self._usage_snapshot,
            cancel_active=self._cancel_active_task,
        )
        return bool(self._cmd_dispatcher.dispatch(text, context))

    def _set_input_text(self, text: str) -> None:
        """Put a registry selection in the composer without submitting it."""
        self._input_session.set_text(text)

    def _usage_snapshot(self) -> "UsageSnapshot":
        """Read one local usage snapshot without touching the active runner."""
        from agenthicc.commands.command import UsageSnapshot  # noqa: PLC0415

        conversation = self._ctx.app_state.conversation
        task = self._agent_task
        ledger = getattr(self._ctx, "usage_ledger", None)
        if ledger is not None:
            ledger_snapshot = ledger.snapshot()
            return UsageSnapshot(
                input_tokens=ledger_snapshot.input_tokens,
                output_tokens=ledger_snapshot.output_tokens,
                cost_usd=ledger_snapshot.cost_usd,
                active_run=bool(task is not None and not task.done()),
                queue_depth=len(self._msg_queue),
                usage_status=ledger_snapshot.usage_status,
                cost_status=ledger_snapshot.cost_status,
                calls=ledger_snapshot.calls,
                known_calls=ledger_snapshot.known_calls,
                unavailable_calls=ledger_snapshot.unavailable_calls,
                provisional_calls=ledger_snapshot.provisional_calls,
                durability_status=ledger_snapshot.durability_status,
            )
        return UsageSnapshot(
            input_tokens=int(conversation.tokens_in()),
            output_tokens=int(conversation.tokens_out()),
            cost_usd=float(conversation.cost_usd()),
            active_run=bool(task is not None and not task.done()),
            queue_depth=len(self._msg_queue),
        )

    def _cancel_active_task(self) -> bool:
        """Request cancellation through the same owner used by Ctrl+C."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        task = self._agent_task
        if task is None or task.done():
            # A cancellation request can race the task's finalizer.  Do not
            # leave the input pipeline in STREAMING while the task reference
            # is already finished; otherwise Ctrl+C is treated as another
            # interrupt instead of the idle exit sequence.
            self._input_session.set_mode(InputMode.IDLE)
            return False
        # Esc/Ctrl+C first stops the owned terminal currently awaited by this
        # turn, then cancels the agent task.  The process group therefore does
        # not outlive a cancelled foreground wait.
        terminal_manager = getattr(self._ctx, "terminal_manager", None)
        try:
            if terminal_manager is not None:
                terminal_manager.request_stop_current()
            return task.cancel()
        finally:
            # Switch immediately.  The task's finally block also sets IDLE,
            # but waiting for that callback creates a race with a quick
            # ESC → Ctrl+C exit attempt on Windows.
            self._input_session.set_mode(InputMode.IDLE)

    def _busy_decision(self, text: str) -> "BusyDecision":
        from agenthicc.commands.busy_policy import classify_busy_command  # noqa: PLC0415

        return classify_busy_command(text, self._ctx.cmd_registry)

    def _notify_queued(self, text: str) -> None:
        label = text[:40] + ("…" if len(text) > 40 else "")
        position = len(self._msg_queue)
        self._ctx.app_state.conversation.notification.set(f"⌛ Queued #{position}: {label}")

    def _next_queued_message(self) -> str | None:
        """Claim the next plain-text message at a completed tool boundary.

        The active agent loop owns the shared memory, so a queued message can
        be appended safely only after its tool results have been committed and
        before the loop's next model request. Slash commands and skills stay at
        the head of the FIFO until the turn is idle, where ``advance()`` can
        route them locally instead of sending their command spelling to the
        model.
        """
        if not self._msg_queue:
            return None
        text = self._msg_queue[0].strip()
        if not text or text.startswith(("/", "$")):
            return None
        self._msg_queue.pop(0)
        self._ctx.app_state.conversation.append_event("user_message", {"text": text})
        if self._msg_queue:
            self._notify_queued(self._msg_queue[0])
        else:
            self._ctx.app_state.conversation.notification.set(None)
        return text

    def _notify_busy_rejected(self, text: str, reason: str) -> None:
        command = text.split(None, 1)[0] if text.split(None, 1) else text
        self._ctx.app_state.conversation.notification.set(
            f"⛔ Rejected while busy: {command} — {reason or 'try again after the run'}"
        )

    def _run_busy_immediate(self, text: str, decision: "BusyDecision") -> None:
        """Run one approved local command through the normal route/dispatcher."""
        try:
            handled = self.route(text)
        except Exception as exc:  # noqa: BLE001
            self._ctx.app_state.conversation.notification.set(
                f"⚠ Command failed locally: {type(exc).__name__}: {exc}"
            )
            return
        if not handled:
            # A registry/plugin race cannot fall through to the LPM.
            self._ctx.app_state.conversation.notification.set(
                f"⚠ Command unavailable while busy: {decision.command_name}"
            )
            return
        self._ctx.app_state.conversation.notify_transient(f"▶ Ran now: {decision.command_name}")

    def _reload_skills(self) -> "SkillDiscoveryResult":
        """Rescan skill directories and refresh skill-owned dollar commands."""
        from agenthicc.skills.loader import discover_skills_with_diagnostics  # noqa: PLC0415

        cfg = self._ctx.cfg
        global_dir = (
            Path(cfg.skills.default_skill_directory).expanduser()
            if cfg.skills.default_skill_directory
            else Path.home() / ".agenthicc"
        )
        discovery = discover_skills_with_diagnostics(
            project_dir=Path(".agenthicc"),
            user_dir=global_dir,
        )

        # Build all replacement commands before mutating the live session. If
        # discovery or command construction fails, the current session remains
        # usable and the caller can report the failure.
        replacement_commands = [
            (skill, _build_skill_command(slug, skill)) for slug, skill in discovery.skills.items()
        ]
        registry = self._ctx.cmd_registry
        skill_sources = {
            command.source_id
            for command in registry.all_commands()
            if command.source_id.startswith("skill:")
        }
        for source_id in skill_sources:
            registry.unregister_source(source_id)

        # Preserve the dictionary object because workflow configuration and
        # command contexts keep references to this session-owned mapping.
        self._ctx.skills.clear()
        self._ctx.skills.update(discovery.skills)
        conflicts: list[tuple[Path, str]] = []
        for skill, command in replacement_commands:
            if any(registry.get(name) is not None for name in (command.name, *command.aliases)):
                conflicts.append(
                    (
                        skill.path,
                        f"{command.name}: command name or alias conflicts with an existing command",
                    )
                )
                continue
            registry.register(command)

        if conflicts:
            from agenthicc.skills.loader import SkillDiagnostic, SkillDiscoveryResult  # noqa: PLC0415

            discovery = SkillDiscoveryResult(
                skills=discovery.skills,
                diagnostics=discovery.diagnostics
                + tuple(
                    SkillDiagnostic(
                        path=path,
                        code="command-conflict",
                        message=message,
                        severity="warning",
                    )
                    for path, message in conflicts
                ),
            )
        return discovery

    def _reload_tools(self) -> tuple[bool, str]:
        """Rescan project/user tool plugins and publish them atomically."""
        from agenthicc.plugins.discovery import discover_project_tools, warn_conflicts

        try:
            discovered = discover_project_tools(
                project_dir=Path(".agenthicc"),
                user_dir=Path.home() / ".agenthicc",
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"Tool reload failed; existing tools kept: {type(exc).__name__}: {exc}"

        if discovered.failed:
            failures: list[str] = []
            for result in discovered.failed:
                reason = result.error or ("missing dependencies: " + ", ".join(result.missing_deps))
                failures.append(f"{result.path}: {reason}")
            return False, "Tool reload failed; existing tools kept:\n" + "\n".join(failures)

        old_count = len(self._ctx.project_plugins.all_tools)
        warn_conflicts(discovered)
        self._ctx.project_plugins = discovered

        # WorkflowConfig is session-owned and frozen; replace its plugin set so
        # subsequent workflow turns see the same freshly loaded tools.
        import dataclasses

        self._wf_config_base = dataclasses.replace(
            self._wf_config_base,
            plugin_tools=discovered,
        )
        new_count = len(discovered.all_tools)
        return True, f"Tools reloaded — {new_count} tool(s) available (was {old_count})."

    def _reload_workflows(self) -> tuple[bool, str]:
        """Rebuild workflows and replace the live registry in place."""
        from agenthicc.workflows.registry import build_workflow_registry

        try:
            discovered = build_workflow_registry(
                project_dir=Path(".agenthicc"),
                user_dir=Path.home() / ".agenthicc",
            )
        except Exception as exc:  # noqa: BLE001
            return False, (
                f"Workflow reload failed; existing workflows kept: {type(exc).__name__}: {exc}"
            )

        registry = self._ctx.workflow_registry
        old_names = set(registry.names())
        new_names = set(discovered.names())
        registry.replace_with(discovered)

        if self._workflow_override is not None and self._workflow_override not in new_names:
            self._workflow_override = None
            self._ctx.app_state.conversation.workflow_override.set(None)

        changes: list[str] = []
        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added:
            changes.append(f"added: {', '.join(added)}")
        if removed:
            changes.append(f"removed: {', '.join(removed)}")
        suffix = "; ".join(changes) if changes else "no workflow changes"
        return True, f"Workflows reloaded — {len(new_names)} workflow(s); {suffix}."

    def _reload_commands(self) -> tuple[bool, str]:
        """Rescan slash-command plugins and publish a valid set atomically."""
        from agenthicc.commands import build_builtin_registry  # noqa: PLC0415
        from agenthicc.commands.plugin_loader import discover_command_plugins  # noqa: PLC0415

        try:
            discovered = discover_command_plugins(
                project_dir=Path(".agenthicc"),
                user_dir=Path.home() / ".agenthicc",
            )
        except Exception as exc:  # noqa: BLE001
            return (
                False,
                f"Command reload failed; existing commands kept: {type(exc).__name__}: {exc}",
            )

        if discovered.failed:
            failures: list[str] = []
            for result in discovered.failed:
                if result.error:
                    reason = result.error
                else:
                    reason = "missing dependencies: " + ", ".join(result.missing_deps)
                failures.append(f"{result.path}: {reason}")
            return (
                False,
                "Command reload failed; existing commands kept:\n" + "\n".join(failures),
            )

        registry = self._ctx.cmd_registry
        old_plugin_names = set(getattr(self._ctx, "command_plugin_names", set()))
        old_plugins = {
            command.name: command
            for command in registry.all_commands()
            if command.name in old_plugin_names
        }
        new_plugins = {command.name: command for command in discovered.all_commands}

        # Preserve commands registered by other extension surfaces (for
        # example MCP) while rebuilding the stable built-in/skill/plugin order.
        preserved = [
            command
            for command in registry.all_commands()
            if command.name not in old_plugin_names
            and command.source_id != "builtin"
            and not command.source_id.startswith("skill:")
        ]

        candidate = build_builtin_registry()
        for command in preserved:
            candidate.register(command)
        candidate.register_many(discovered.all_commands)
        _register_skill_commands(candidate, self._ctx.skills)

        # Only publish after discovery, validation, and candidate construction
        # have all succeeded. Consumers keep the same registry object.
        registry.replace_with(candidate)
        new_plugin_names = set(new_plugins)
        command_plugin_names = getattr(self._ctx, "command_plugin_names", None)
        if command_plugin_names is None:
            self._ctx.command_plugin_names = set(new_plugin_names)
        else:
            command_plugin_names.clear()
            command_plugin_names.update(new_plugin_names)

        added = sorted(new_plugin_names - old_plugin_names)
        removed = sorted(old_plugin_names - new_plugin_names)
        updated = sorted(set(old_plugins) & new_plugin_names)
        summary: list[str] = []
        if added:
            summary.append(f"added: {', '.join(added)}")
        if updated:
            summary.append(f"updated: {', '.join(updated)}")
        if removed:
            summary.append(f"removed: {', '.join(removed)}")
        if not summary:
            summary.append("no command changes")
        return True, "Commands reloaded — " + "; ".join(summary)

    def _handle_workflow_command(self, args: str) -> bool:
        """Handle /workflow <name> | reset | resume [run-id]."""
        name = args.strip()
        conv = self._ctx.app_state.conversation
        if name == "resume" or name.startswith("resume "):
            run_id = name.partition(" ")[2].strip() or None
            return self._handle_workflow_resume(run_id)
        if not name or name == "reset":
            handle = self._workflow_handle
            if handle is not None and handle.lifecycle in {"paused", "pausing"}:
                handle.mark_terminal("discarded", error="reset by user")
                if handle.checkpoint_supported:
                    try:
                        handle.save_checkpoint(reason="reset")
                    except Exception as exc:  # noqa: BLE001
                        conv.notify_transient(f"⚠ Could not persist workflow reset: {exc}")
                        return True
                    from agenthicc.kernel import Event  # noqa: PLC0415

                    asyncio.create_task(
                        self._ctx.processor.emit(
                            Event.create(
                                "WorkflowRunDiscarded",
                                {
                                    "run_id": handle.run_id,
                                    "workflow_name": handle.workflow_name,
                                },
                            )
                        )
                    )
                self._workflow_handle = None
                conv.notify_transient("↩ Paused workflow discarded; workflow reset to mode default")
                self._workflow_override = None
                conv.workflow_override.set(None)
                return True
            self._workflow_override = None
            conv.workflow_override.set(None)
            conv.notify_transient("↩ Workflow reset to mode default")
            return True
        defn = self._ctx.workflow_registry.get(name)
        if defn is None:
            available = ", ".join(self._ctx.workflow_registry.names()) or "none"
            conv.notify_transient(f"⚠ Unknown workflow: {name!r}  (available: {available})")
            return True
        if (
            self._workflow_handle is not None
            and self._workflow_handle.lifecycle in {"paused", "pausing"}
            and self._workflow_handle.workflow_name != name
        ):
            conv.notify_transient(
                f"⚠ '{self._workflow_handle.workflow_name}' is paused. Resume or reset it before switching workflows."
            )
            return True
        self._workflow_override = name
        conv.workflow_override.set(name)
        conv.notify_transient(f"⚡ Workflow → {name}")
        return True

    def _handle_workflow_resume(self, run_id: str | None) -> bool:
        """Resume the paused workflow attached to this session."""
        conv = self._ctx.app_state.conversation
        if self._agent_task is not None and not self._agent_task.done():
            conv.notify_transient("⚠ Cannot resume a workflow while another run is active")
            return True
        handle = self._workflow_handle
        if run_id is not None and (handle is None or handle.run_id != run_id):
            conv.notify_transient(f"⚠ No paused workflow with run id {run_id!r}")
            return True
        if handle is None or handle.lifecycle not in {"paused", "pausing"}:
            conv.notify_transient(
                "⚠ No paused workflow is available to resume; create_workflow writes directly to the workspace."
            )
            return True
        definition = self._ctx.workflow_registry.get(handle.workflow_name)
        if definition is None or handle.context is None:
            conv.notify_transient(f"⚠ Workflow '{handle.workflow_name}' is not available to resume")
            return True
        self._agent_task = asyncio.create_task(
            self._resume_workflow_task(definition, handle.context),
            name=f"resume-workflow-{handle.run_id}",
        )
        conv.notify_transient(f"↻ Resuming workflow '{handle.workflow_name}'…")
        return True

    async def _handle_compact_command(self) -> None:
        """Handle /compact — compact the current session memory (PRD-119)."""
        from agenthicc.memory.compactor import compact_memory  # noqa: PLC0415

        ctx = self._ctx
        conv = ctx.app_state.conversation
        mem = ctx.session_memory

        if mem is None or not mem._messages:
            conv.notify_transient("⎋ Nothing to compact")
            return

        transport = getattr(ctx.agent_runner, "_transport", None)
        if transport is None:
            conv.notify_transient("⚠ No transport available for compaction")
            return

        model = ctx.cfg.execution.effective_model()
        # Bound each summariser call to the model window so compacting a history
        # larger than the window map-reduces instead of overflowing (PRD-135 B).
        await compact_memory(
            mem,
            transport,
            model=model,
            conv_store=conv,
            max_input_tokens=ctx.cfg.execution.effective_context_window(),
            usage_ledger=getattr(ctx, "usage_ledger", None),
            session_id=ctx.session_id,
            run_id=f"compaction:{ctx.session_id}:{uuid.uuid4().hex[:12]}",
        )
        conv.notify_transient("⎋ Compacted")

    def route(self, msg: str) -> bool:
        """Return True if command routing consumes *msg* locally."""
        if not msg.startswith(("/", "$")):
            return False
        # PRD-114: /workflow is handled locally — not via the command registry.
        # PRD-119: /compact likewise — needs access to session memory.
        parts = msg.split(None, 1)
        if parts[0] == "/workflow":
            return self._handle_workflow_command(parts[1] if len(parts) > 1 else "")
        if parts[0] == "/compact":
            asyncio.create_task(self._handle_compact_command(), name="compact")
            return True
        command = self._ctx.cmd_registry.get(parts[0])
        if command is None and parts[0].startswith("$"):
            # Unknown dollar-prefixed text is ordinary user input, not a
            # failed command.  This also keeps literal `$...` prompts usable.
            return False
        if command is not None and command.is_skill != parts[0].startswith("$"):
            # Skills have their own `$` namespace. This guard also keeps
            # stale or manually injected slash-named skill records from
            # reintroducing the removed legacy syntax.
            return False
        if self.dispatch_slash(msg):
            # Check if a replay was requested by the command handler.
            if self._pending_replay_id:
                replay_id = self._pending_replay_id
                self._pending_replay_id = None
                self._agent_task = asyncio.create_task(self._run_replay(replay_id), name="replay")
            return True
        cmd_name = msg.split()[0]
        if self._ctx.cmd_registry.get(cmd_name) is not None:
            self._ctx.console.print(
                f"  [dim]Command [bold]{cmd_name}[/bold] has no handler. "
                f"Add a handler in [bold].agenthicc/commands/[/bold][/dim]"
            )
        return True  # never forward slash commands to the agent

    def _start_workflow_continuation(self, text: str) -> bool:
        """Start a paused workflow with one ordinary user continuation."""
        handle = self._workflow_handle
        if handle is None or handle.lifecycle not in {"paused", "pausing"}:
            return False
        if not handle.checkpoint_supported:
            self._ctx.app_state.conversation.notify_transient(
                "⚠ This workflow does not support safe checkpoints; use /workflow reset or retry after fixing its context codec."
            )
            self._msg_queue.insert(0, text)
            return True
        definition = self._ctx.workflow_registry.get(handle.workflow_name)
        if definition is None or handle.context is None:
            self._ctx.app_state.conversation.notify_transient(
                f"⚠ Workflow '{handle.workflow_name}' is not available to resume"
            )
            return True
        turn_id = f"turn_{uuid.uuid4().hex}"
        self._publish_session_event(
            "turn_queued", {"text": text, "client_id": "tui"}, turn_id=turn_id
        )
        self._agent_task = asyncio.create_task(
            self._resume_workflow_task(
                definition,
                handle.context,
                continuation=text,
                turn_id=turn_id,
            ),
            name=f"resume-workflow-{handle.run_id}",
        )
        return True

    def advance(self) -> None:
        """Drain _msg_queue: dispatch slash commands, start next agent task."""
        notice_kept = False
        while self._msg_queue:
            msg = self._msg_queue.pop(0).strip()
            if not msg:
                continue
            # The registry can be reloaded while text is waiting. Reclassify
            # before release so a newly rejected command is not executed from
            # stale metadata, and report removed slash commands explicitly.
            decision = self._busy_decision(msg)
            if decision.policy.value == "reject":
                self._notify_busy_rejected(msg, decision.reason)
                notice_kept = True
                continue
            token = msg.split(None, 1)[0]
            if token.startswith("/") and self._ctx.cmd_registry.get(token) is None:
                self._ctx.app_state.conversation.notify_transient(
                    f"⚠ Queued command no longer exists: {token}"
                )
                notice_kept = True
                continue
            if self.route(msg):
                if self._agent_task is not None and not self._agent_task.done():
                    return
                continue
            self._ctx.app_state.conversation.notification.set(None)
            self._ctx.app_state.conversation.append_event("user_message", {"text": msg})
            if self._start_workflow_continuation(msg):
                return
            turn_id = f"turn_{uuid.uuid4().hex}"
            self._publish_session_event(
                "turn_queued", {"text": msg, "client_id": "tui"}, turn_id=turn_id
            )
            self._agent_task = asyncio.create_task(
                self.agent_task_body(msg, turn_id=turn_id), name="agent-turn"
            )
            return
        if not notice_kept:
            self._ctx.app_state.conversation.notification.set(None)

    # ── agent turn plumbing ───────────────────────────────────────────────────

    async def run_turn(self, text: str, resume: object | None = None) -> None:
        """Dispatch one user message: workflow or direct agent turn.

        *resume* (a PRD-129 ``ResumePlan``) re-drives an interrupted direct turn
        with its original turn id and a ledger seeded with the tools that already
        ran, so completed side effects are replayed rather than repeated.
        """
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        ctx = self._ctx

        self._input_session.set_mode(InputMode.STREAMING)
        ctx.approval_svc.reset_turn_memory()

        if self._pending_skill_body:
            text = self._pending_skill_body.pop() + "\n\n" + text

        # PRD-114: /workflow override takes priority over mode default.
        _active_wf_name = self._workflow_override or ctx.app_state.active_mode().default_workflow
        _plugin_cls = ctx.workflow_registry.get(_active_wf_name) if _active_wf_name else None

        _timeout = ctx.cfg.execution.turn_timeout_s
        # PRD-126 gap 11: a turn-timeout deadline so retries are not scheduled
        # when there is no meaningful budget left before asyncio.wait_for fires.
        import time as _time  # noqa: PLC0415

        _deadline = (_time.monotonic() + _timeout) if (_timeout and _timeout > 0) else None

        async def _run_inner() -> None:
            if _plugin_cls is not None:
                import dataclasses as _dc  # noqa: PLC0415

                # PRD-116: build per-workflow params from merged TOML/CLI/env config.
                _wf_params = _plugin_cls.build_params(ctx.cfg.workflows.get(_plugin_cls.name, {}))
                _phase_specs = getattr(_plugin_cls, "phases", ())
                if (
                    self._workflow_handle is None
                    or self._workflow_handle.workflow_name != _plugin_cls.name
                ):
                    from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore  # noqa: PLC0415
                    from agenthicc.runners.workflow_handle import WorkflowRunHandle  # noqa: PLC0415

                    session_conversation = getattr(ctx, "session_conversation", None)
                    if session_conversation is not None:
                        self._workflow_handle = WorkflowRunHandle.create(
                            run_id=uuid.uuid4().hex,
                            workflow=_plugin_cls,
                            conversation=session_conversation,
                            intent=text,
                            checkpoint_store=WorkflowCheckpointStore(ctx.session_id),
                            browser_manager=getattr(ctx, "browser_manager", None),
                        )
                _wf_config = _dc.replace(
                    self._wf_config_base,
                    completed_turns=self._turn_count,
                    params=_wf_params,
                    workflow_handle=self._workflow_handle,
                    terminal_wait_policies={
                        phase.name: phase.terminal_wait_policy
                        for phase in _phase_specs
                        if hasattr(phase, "name") and hasattr(phase, "terminal_wait_policy")
                    },
                )
                # Plugin owns runner construction — no name-based branching.
                _wf_runner = _plugin_cls.build_runner(_wf_config, ctx.mode_manager)
                await _wf_runner.run(text)
                self._finalize_returned_workflow()
                # PRD-155: workflow-bound runs return to Safe after success.
                _wf_result = ctx.app_state.workflow_run()
                if (
                    _wf_result is not None
                    and getattr(_wf_result, "status", None) == "complete"
                    and ctx.app_state.active_mode().default_workflow is not None
                ):
                    ctx.mode_manager.set_by_name("Safe")
                    ctx.app_state.conversation.notification.set(
                        "✓ Workflow complete — switched to Safe mode"
                    )
                if self._workflow_handle is not None and self._workflow_handle.lifecycle in {
                    "complete",
                    "failed",
                    "discarded",
                }:
                    self._workflow_handle = None
            else:
                # PRD-126: direct (non-workflow) turns are retried at the
                # _run_agent_turn boundary inside AgentTurnRunner itself, so no
                # retry wrapper is needed here.
                await _run_agent_turn(
                    text,
                    ctx.agent_runner,
                    ctx.processor,
                    session_memory=ctx.session_memory,
                    conversation_id=ctx.session_id,
                    max_agent_turns=ctx.cfg.execution.max_agent_turns,
                    conv_store=ctx.app_state.conversation,
                    app_state=ctx.app_state,
                    exec_cfg=ctx.cfg.execution,
                    skills=ctx.skills,
                    skill_permissions=ctx.cfg.agents.skill_permissions_for("default"),
                    mention_cache=ctx.mention_cache,
                    project_plugin_tools=(
                        cast("list[ToolLike]", ctx.project_plugins.all_tools)
                        + _make_session_tools(
                            ctx.approval_svc,
                            memory_router=ctx.memory_router,
                            semantic_index=ctx.semantic_index,
                        )
                        + list(getattr(ctx, "browser_tools", []))
                    ),
                    mcp_registry=ctx.mcp_registry,
                    active_agent="default",
                    completed_turns=self._turn_count,
                    approval_svc=ctx.approval_svc,
                    memory_router=ctx.memory_router,
                    semantic_index=ctx.semantic_index,
                    retry_deadline_monotonic=_deadline,
                    resume_turn_id=getattr(resume, "turn_id", None),
                    resume_ledger=getattr(resume, "ledger", None),
                    next_queued_message=self._next_queued_message,
                    usage_ledger=getattr(ctx, "usage_ledger", None),
                    browser_manager=getattr(ctx, "browser_manager", None),
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
            # PRD-129 Phase 2: no per-turn snapshot save — the JournaledShortTermMemory
            # already fsync'd every transition durably as it happened.

    async def agent_task_body(
        self,
        text: str,
        resume: object | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Wrap run_turn with error handling; advance queue on completion."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        conv = self._ctx.app_state.conversation
        turn_failed = False
        turn_cancelled = False
        workflow_paused = False
        conversation = getattr(self._ctx, "session_conversation", None)
        owner_id = turn_id or f"activity_{uuid.uuid4().hex}"
        acquired = False
        conv.begin_activity()
        if turn_id is not None:
            self._publish_session_event("turn_started", turn_id=turn_id)
        try:
            if conversation is not None:
                await conversation.acquire(owner_id)
                acquired = True
            await self.run_turn(text, resume=resume)
        except asyncio.CancelledError:
            turn_cancelled = True
            handle = self._workflow_handle
            if handle is not None and handle.is_pause_requested():
                workflow_paused = True
                await self._finalize_workflow_pause(handle)
            elif handle is not None and handle.lifecycle in {"running", "resuming", "pausing"}:
                handle.mark_terminal("failed", error="cancelled")
                try:
                    handle.save_checkpoint(reason="cancelled")
                except Exception:  # noqa: BLE001
                    pass
            # close_turn() is idempotent — inner layers may have already called it.
            conv.close_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            turn_failed = True
            # Only emit an error event if the turn is still open; if _stream()
            # already closed it (via its own finally), this is a no-op.
            conv.close_turn(error=_fmt_exc(exc) if conv.is_turn_active else None)
            self._input_session.set_mode(InputMode.IDLE)
        finally:
            conv.end_activity()
            if acquired and conversation is not None:
                conversation.release(owner_id)
            if turn_id is not None:
                turn_event = (
                    "turn_paused"
                    if workflow_paused
                    else "turn_cancelled"
                    if turn_cancelled
                    else "turn_failed"
                    if turn_failed
                    else "turn_completed"
                )
                self._publish_session_event(
                    turn_event,
                    turn_id=turn_id,
                )
            self._agent_task = None
            self.advance()

    async def _finalize_workflow_pause(self, handle: "WorkflowRunHandle") -> None:
        """Persist a paused workflow only after the runner has unwound."""
        from agenthicc.kernel import Event  # noqa: PLC0415

        conv = self._ctx.app_state.conversation
        try:
            # Save while PAUSING first so an unsupported custom context fails
            # closed without falsely advertising a resumable PAUSED state.
            handle.save_checkpoint(reason="escape")
            handle.mark_paused(reason="escape")
            checkpoint = handle.save_checkpoint(reason="escape")
            await self._ctx.processor.emit(
                Event.create(
                    "WorkflowRunPaused",
                    {
                        "run_id": handle.run_id,
                        "workflow_name": handle.workflow_name,
                        "phase_name": handle.current_phase,
                        "conversation_cursor": checkpoint.conversation_cursor,
                    },
                )
            )
            await self._ctx.processor.emit(
                Event.create(
                    "WorkflowCheckpointSaved",
                    {
                        "run_id": handle.run_id,
                        "workflow_name": handle.workflow_name,
                        "revision": checkpoint.revision,
                        "reason": "escape",
                    },
                )
            )
            conv.notify_transient(
                f"⏸ Workflow '{handle.workflow_name}' paused at {handle.current_phase or 'current phase'}. "
                "Send a message or use /workflow resume to continue."
            )
        except Exception as exc:  # noqa: BLE001
            conv.notify_transient(f"⚠ Workflow pause could not be checkpointed: {exc}")

    async def handle_send(self, cmd: "SendMessageCommand") -> None:
        """Route user message: slash → command dispatcher, text → agent."""
        text = cmd.text.strip()
        if not text:
            return

        # Keep the latest accepted natural-language intent available to the
        # background control plane even if it was submitted while another run
        # was still active and therefore entered the FIFO queue.
        if not text.startswith(("/", "$")):
            self._last_submitted_text = text

        if self._agent_task and not self._agent_task.done():
            decision = self._busy_decision(text)
            if decision.policy.value in {"immediate-read-only", "immediate-control"}:
                self._run_busy_immediate(text, decision)
                return
            if decision.policy.value == "reject":
                self._notify_busy_rejected(text, decision.reason)
                return
            self._msg_queue.append(text)
            self._notify_queued(text)
            return

        if isinstance(self, TUISession) and not text.startswith(("/", "$")):
            if self._start_workflow_continuation(text):
                self._ctx.app_state.conversation.append_event("user_message", {"text": text})
                return

        if self.route(text):
            if self._pending_skill_body:
                body = self._pending_skill_body.pop()
                self._ctx.app_state.conversation.append_event("user_message", {"text": text})
                turn_id = f"turn_{uuid.uuid4().hex}"
                publish_event = getattr(self, "_publish_session_event", None)
                if callable(publish_event):
                    publish_event(
                        "turn_queued", {"text": body, "client_id": "tui"}, turn_id=turn_id
                    )
                    task = self.agent_task_body(body, turn_id=turn_id)
                else:
                    task = self.agent_task_body(body)
                self._agent_task = asyncio.create_task(task, name="agent-turn")
            return
        self._ctx.app_state.conversation.append_event("user_message", {"text": text})
        turn_id = f"turn_{uuid.uuid4().hex}"
        publish_event = getattr(self, "_publish_session_event", None)
        if callable(publish_event):
            publish_event("turn_queued", {"text": text, "client_id": "tui"}, turn_id=turn_id)
            task = self.agent_task_body(text, turn_id=turn_id)
        else:
            task = self.agent_task_body(text)
        self._agent_task = asyncio.create_task(task, name="agent-turn")

    def handle_interrupt(self, cmd: "InterruptAgentCommand") -> None:
        """Cancel, or cooperatively pause, the current agent task."""
        handle = self._workflow_handle
        if (
            getattr(cmd, "disposition", "cancel") == "pause"
            and handle is not None
            and handle.lifecycle in {"running", "resuming"}
        ):
            handle.request_pause()
            self._ctx.app_state.conversation.notify_transient(
                f"⏸ Pausing workflow '{handle.workflow_name}'…"
            )
        self._cancel_active_task()

    # ── workflow resume (PRD-94) ──────────────────────────────────────────────

    async def _run_replay(self, session_id: str) -> None:
        """Replay a historical session's conversation through the render pipeline."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415
        from agenthicc.tui.runtime.replay import ConversationReplayer  # noqa: PLC0415

        ctx = self._ctx

        # Enter Replay mode — blocks all tool capabilities during replay.
        prior_mode = ctx.app_state.active_mode()
        ctx.mode_manager.set_internal_by_name("Replay")
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
            ctx.mode_manager.restore(prior_mode)
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

    def _has_incomplete_workflow(self) -> bool:
        from agenthicc.kernel.state import NodeStatus  # noqa: PLC0415

        k_state = self._ctx.processor.get_state()
        return any(
            bool(wf.name) and wf.status not in (NodeStatus.complete, NodeStatus.failed)
            for wf in k_state.workflows.values()
        )

    def _maybe_resume_interrupted_turn(self) -> None:
        """PRD-129 Phase 3: re-drive a turn the prior session left incomplete.

        Fires only for a *direct* turn (no in-progress workflow — those are left
        to the workflow's own resume).  Rolls memory back to the turn's pre-turn
        point, then re-submits the user message with a ledger seeded from the
        tools that already ran, so completed side effects are replayed, not
        repeated.
        """
        ctx = self._ctx
        plan = ctx.pending_resume
        if plan is None or self._has_incomplete_workflow():
            return
        mem = ctx.session_memory
        rollback = getattr(mem, "rollback_to", None)
        if callable(rollback):
            rollback(int(getattr(plan, "base_count", 0)))
        ctx.app_state.conversation.notification.set(
            "↻ Resuming an interrupted turn — completed tools are replayed, not repeated…"
        )
        self._agent_task = asyncio.create_task(
            self.agent_task_body(str(getattr(plan, "user_message", "")), resume=plan),
            name="resume-turn",
        )

    async def _resume_workflow_task(
        self,
        wf_defn: type[WorkflowPlugin],
        context: object,
        *,
        continuation: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Resume a WorkflowRunner with the session conversation still attached."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415

        ctx = self._ctx
        handle = self._workflow_handle
        conversation = getattr(ctx, "session_conversation", None)
        owner_id = turn_id or (
            handle.run_id if handle is not None else f"resume_{uuid.uuid4().hex}"
        )
        acquired = False
        self._input_session.set_mode(InputMode.STREAMING)
        if ctx.approval_svc is not None:
            ctx.approval_svc.reset_turn_memory()
        ctx.app_state.conversation.begin_activity()
        if turn_id is not None:
            self._publish_session_event("turn_started", turn_id=turn_id)
        try:
            import dataclasses as _dc  # noqa: PLC0415

            if conversation is not None:
                await conversation.acquire(owner_id)
                acquired = True
            if handle is not None:
                handle.mark_resuming()
                from agenthicc.kernel import Event  # noqa: PLC0415

                await self._ctx.processor.emit(
                    Event.create(
                        "WorkflowRunResumed",
                        {
                            "run_id": handle.run_id,
                            "workflow_name": handle.workflow_name,
                        },
                    )
                )
            if ctx.session_memory is not None:
                continuation_text = (
                    "[WORKFLOW RESUME]\n"
                    f"Workflow: {wf_defn.name}\n"
                    f"Current phase: {getattr(handle, 'current_phase', None) or 'current'}\n"
                    "The prior agent turn was interrupted safely; preserve completed work "
                    "and continue from the saved phase.\n"
                    f"Original intent: {getattr(handle, 'original_intent', '')}\n"
                    + (f"User continuation: {continuation}" if continuation else "Continue.")
                )
                if handle is not None and continuation:
                    handle.append_continuation(continuation)
                ctx.session_memory.add_user(continuation_text)

            _wf_params = wf_defn.build_params(ctx.cfg.workflows.get(wf_defn.name, {}))
            _phase_specs = getattr(wf_defn, "phases", ())
            _wf_config = _dc.replace(
                self._wf_config_base,
                params=_wf_params,
                workflow_handle=handle,
                session_memory=ctx.session_memory,
                conversation_id=ctx.session_id,
                terminal_wait_policies={
                    phase.name: phase.terminal_wait_policy
                    for phase in _phase_specs
                    if hasattr(phase, "name") and hasattr(phase, "terminal_wait_policy")
                },
            )
            runner = wf_defn.build_runner(_wf_config, ctx.mode_manager)
            await runner.resume(context)
            self._finalize_returned_workflow()
            # PRD-155: completed workflow-bound runs return to Safe.
            _wf_result = ctx.app_state.workflow_run()
            if (
                _wf_result is not None
                and getattr(_wf_result, "status", None) == "complete"
                and ctx.app_state.active_mode().default_workflow is not None
            ):
                ctx.mode_manager.set_by_name("Safe")
                ctx.app_state.conversation.notification.set(
                    "✓ Workflow resumed and complete — switched to Safe mode"
                )
        except asyncio.CancelledError:
            if handle is not None and handle.is_pause_requested():
                await self._finalize_workflow_pause(handle)
            elif handle is not None and handle.lifecycle in {"resuming", "running", "pausing"}:
                handle.mark_terminal("failed", error="cancelled")
                try:
                    handle.save_checkpoint(reason="cancelled")
                except Exception:  # noqa: BLE001
                    pass
            ctx.app_state.conversation.close_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            conv = ctx.app_state.conversation
            conv.close_turn(error=_fmt_exc(exc) if conv.is_turn_active else None)
            if handle is not None and handle.lifecycle not in {"complete", "failed", "discarded"}:
                handle.mark_terminal("failed", error=_fmt_exc(exc))
                try:
                    handle.save_checkpoint(reason="failed")
                except Exception:  # noqa: BLE001
                    pass
            self._input_session.set_mode(InputMode.IDLE)
        finally:
            ctx.app_state.conversation.end_activity()
            if acquired and conversation is not None:
                conversation.release(owner_id)
            if turn_id is not None:
                self._publish_session_event(
                    "turn_paused"
                    if handle is not None and handle.lifecycle == "paused"
                    else "turn_failed"
                    if handle is not None and handle.lifecycle == "failed"
                    else "turn_completed",
                    turn_id=turn_id,
                )
            self._agent_task = None
            if handle is not None and handle.lifecycle in {"complete", "failed", "discarded"}:
                self._workflow_handle = None
            self.advance()

    # ── main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start tasks, run input session, tear down."""
        from agenthicc.tui.runtime import (  # noqa: PLC0415
            SendMessageCommand,
            InterruptAgentCommand,
        )

        ctx = self._ctx

        ctx.command_bus.register(SendMessageCommand, self.handle_send)
        ctx.command_bus.register(InterruptAgentCommand, self.handle_interrupt)
        self._wire_approval_overlay()

        self._workspace.start()
        if getattr(ctx, "resumed", False):
            from agenthicc.tui.runtime.session_log import SessionEventLog  # noqa: PLC0415

            await self._workspace.replay_transcript(
                SessionEventLog.load(ctx.session_id, rendered=False)
            )
        proc_task = asyncio.create_task(ctx.processor.run())
        # If a previous session had an in-progress workflow, show a notification
        # but do NOT auto-start it — the user decides what to do next.
        self._notify_incomplete_workflow()
        # PRD-129 Phase 3: auto-resume a direct turn the prior session left
        # interrupted (no-op on a clean start or when a workflow was in progress).
        self._maybe_resume_interrupted_turn()
        ad_task: asyncio.Task[object] | None = None
        try:
            from agenthicc.auth import AuthClient  # noqa: PLC0415
            from agenthicc.ads import AdRotator  # noqa: PLC0415

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
                # Approval and question requests suspend the LLM turn while
                # the overlay owns the terminal. Freeze animation during that
                # wait so SIGWINCH redraws do not race a changing status bar.
                ctx.app_state.conversation.tick(paused=ctx.app_state.pending_approval() is not None)

        tick_task = asyncio.create_task(_tick())
        try:
            await self._input_session.run()
        finally:
            # Input can exit while an ESC-triggered cancellation is still
            # unwinding. Cancel and await the owned task before closing the
            # session; otherwise Windows may leave its executor/terminal wait
            # alive during asyncio shutdown.
            self._msg_queue.clear()
            agent_task = self._agent_task
            if agent_task is not None and not agent_task.done():
                agent_task.cancel()
            tick_task.cancel()
            proc_task.cancel()
            if ad_task:
                ad_task.cancel()
            await asyncio.gather(
                *([agent_task] if agent_task else []),
                tick_task,
                proc_task,
                *([ad_task] if ad_task else []),
                return_exceptions=True,
            )
            self._workspace.stop()


# ── thin factory (≤60 lines) ──────────────────────────────────────────────────


async def _run_tui_session(
    resume_id: str | None = None,
    cli_overrides: list[str] | None = None,
    record_cassette: str | None = None,
    cli_flags: CLIFlags | None = None,
    config_path: str | None = None,
) -> None:
    """Reactive TUI session — single entry point, no legacy branches."""
    from agenthicc.tui.workspace import Workspace  # noqa: PLC0415
    from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415

    cassette_base: Path | None = Path(record_cassette) if record_cassette else None

    ctx = await _build_session_context(
        resume_id, cli_overrides, cassette_base, config_path=config_path
    )
    # PRD-79: stamp CLIFlags onto AppState immediately after creation; frozen for session lifetime.
    if cli_flags is not None:
        ctx.app_state.cli_flags = cli_flags
    workspace = Workspace(
        ctx.app_state,
        ctx.console,
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
        history=load_user_message_history(ctx.session_id) if ctx.resumed else None,
    )
    session = TUISession(ctx, workspace, input_session)
    from agenthicc.background.terminals import (  # noqa: PLC0415
        reset_current_terminal_manager,
        set_current_terminal_manager,
    )

    terminal_context_token = set_current_terminal_manager(ctx.terminal_manager)
    try:
        from agenthicc.tui.welcome import fetch_changelog, print_welcome  # noqa: PLC0415

        changelog = await fetch_changelog()

        print_welcome(
            ctx.console,
            model=ctx.model_label,
            cwd=os.getcwd(),
            changelog=changelog,
        )
        await session.run()
    finally:
        reset_current_terminal_manager(terminal_context_token)
        session._terminal_unsub()
        ctx.session_log.close()
        # PRD-129 Phase 2: close the durable conversation journal handle.
        _close = getattr(ctx.session_memory, "close", None)
        if callable(_close):
            _close()
        # PRD-132 L1: close + clear the workspace file cache.
        from agenthicc.tools.fs.file_cache import (  # noqa: PLC0415
            configure_file_cache,
            get_file_cache,
        )

        _fc = get_file_cache()
        if _fc is not None:
            _fc.close()
            configure_file_cache(None)
        if ctx.mcp_registry:
            await ctx.mcp_registry.shutdown()
        browser_manager = getattr(ctx, "browser_manager", None)
        if browser_manager is not None:
            await browser_manager.close_session()
        await ctx.terminal_manager.close()
        if ctx.kernel_projection_task is not None:
            ctx.kernel_projection_task.cancel()
            await asyncio.gather(ctx.kernel_projection_task, return_exceptions=True)
        session_service = getattr(ctx, "session_service", None)
        if session_service is not None:
            await session_service.close()
        if cassette_base is not None:
            _write_cassette_meta(cassette_base / ctx.session_id, ctx.session_id)


def _write_cassette_meta(cassette_dir: Path, session_id: str) -> None:
    """Write meta.json alongside the cassette files."""
    import json as _json
    from datetime import datetime, timezone

    meta = {
        "session_id": session_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "intent": "",  # filled in manually or from history
    }
    try:
        (cassette_dir / "meta.json").write_text(_json.dumps(meta, indent=2), encoding="utf-8")
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
    import atexit  # noqa: PLC0415
    import signal as _signal  # noqa: PLC0415

    atexit.register(_reset_terminal_on_exit)

    def _sig_exit(signum: int, frame: object) -> None:
        _reset_terminal_on_exit()
        sys.exit(0)

    try:
        _signal.signal(_signal.SIGTERM, _sig_exit)
        _signal.signal(_signal.SIGHUP, _sig_exit)
    except (AttributeError, OSError):
        pass  # Windows / non-TTY environments

    resume_id: str | None = ctx.resume_id
    if resume_id is None and ctx.continue_session:
        resume_id = _find_latest_session_for_cwd()
        if resume_id is None:
            print("No previous session found for this directory. Starting fresh.")

    try:
        asyncio.run(
            _run_tui_session(
                resume_id=resume_id,
                cli_overrides=list(ctx.set_overrides),
                record_cassette=ctx.record_cassette,
                cli_flags=ctx.flags,
                config_path=ctx.config_path,
            )
        )
    except Exception as exc:
        print(f"TUI error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        # Windows can still deliver a console Ctrl+C as an OS-level interrupt
        # when a host terminal ignores the raw-input mode request. Treat it as
        # a normal TUI exit after asyncio has run its cleanup path; never leave
        # a traceback or a half-reset terminal behind.
        pass
    finally:
        _reset_terminal_on_exit()
