"""TUI session — starts the reactive runtime (PRD-58 to PRD-67, PRD-93)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agenthicc.runners.session_context import SessionContext
    from agenthicc.tui.workspace import Workspace
    from agenthicc.tui.input.unified_session import UnifiedInputSession
    from agenthicc.tui.runtime import SendMessageCommand, InterruptAgentCommand

def _build_agent_runner(llm_cfg: Any) -> Any:
    """Build a lauren-ai AgentRunnerBase wired to a SignalBus."""
    if llm_cfg is None:
        return None
    from lauren_ai._agents._runner import AgentRunnerBase  # noqa: PLC0415
    from lauren_ai._module import _build_transport          # noqa: PLC0415
    from lauren_ai._signals import SignalBus                # noqa: PLC0415

    transport = _build_transport(llm_cfg)
    return AgentRunnerBase(transport=transport, signals=SignalBus())


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
from agenthicc.runners.agent_turn import _run_agent_turn         # noqa: E402
from agenthicc.runners.session_context import SessionContext      # noqa: E402


_SESSIONS_DIR = Path.home() / ".agenthicc" / "sessions"

# Module-level alias so tests that monkeypatch this name on the module work.
_find_latest_session_for_cwd = find_latest_session_for_cwd


def _reconstruct_workflow_context(wf: Any) -> Any:
    """Rebuild a WorkflowContext from a kernel Workflow state entry (PRD-94)."""
    from agenthicc.workflow.plugin import WorkflowContext, PhaseOutput  # noqa: PLC0415
    from agenthicc.kernel.state import NodeStatus                       # noqa: PLC0415

    context = WorkflowContext(
        intent=wf.intent_text,
        run_id=wf.workflow_id,
        workflow_name=wf.name,
    )
    for node_id, node in wf.nodes.items():
        if node.status == NodeStatus.complete and isinstance(node.result, dict):
            r = node.result
            context.add_output(PhaseOutput(
                phase_name=node_id,
                role=r.get("role", ""),
                full_text=r.get("full_text", ""),
                structured=r.get("structured"),
                approved=r.get("approved"),
            ))
    return context


# ── session construction ──────────────────────────────────────────────────────

async def _build_session_context(
    resume_id: str | None,
    cli_overrides: list[str] | None,
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

    # ── kernel ────────────────────────────────────────────────────────────────
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
    approval_svc = ApprovalService(app_state)

    # ── workflow + agents registries ──────────────────────────────────────────
    from agenthicc.workflow.registry import build_workflow_registry  # noqa: PLC0415
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
    from agenthicc.skills.loader import discover_skills as _ds       # noqa: PLC0415
    skills = _ds(project_dir=Path(".agenthicc"),
                 user_dir=Path.home() / ".agenthicc")

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
    agent_runner = _build_agent_runner(llm_cfg)

    # ── PRD-83: AgentRunComplete reconciliation handler ───────────────────────
    _runner_signals = getattr(agent_runner, "_signals", None)
    if _runner_signals is not None:
        from lauren_ai._signals import AgentRunComplete as _ARC  # noqa: PLC0415

        @_runner_signals.on(_ARC)
        async def _on_agent_run_complete(sig: Any) -> None:
            usage = getattr(sig, "total_usage", None)
            cost  = float(getattr(sig, "total_cost_usd", 0.0) or 0.0)
            if usage is not None:
                inp = int(getattr(usage, "input_tokens", 0) or 0)
                out = int(getattr(usage, "output_tokens", 0) or 0)
                app_state.conversation.set_tokens(inp, out, cost)

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

        from agenthicc.commands import CommandDispatcher          # noqa: PLC0415
        from agenthicc.workflow.config import WorkflowConfig      # noqa: PLC0415
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
        )

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set_pending_skill(self, body: str) -> None:
        self._pending_skill_body.clear()
        self._pending_skill_body.append(body)

    def _wire_approval_overlay(self) -> None:
        workspace    = self._workspace
        approval_svc = self._ctx.approval_svc
        app_state    = self._ctx.app_state

        def _on_approval_change() -> None:
            req = app_state.pending_approval()
            from agenthicc.tui.workspace.overlays.approval import ApprovalOverlay           # noqa: PLC0415
            from agenthicc.tui.workspace.overlays.plan_approval import PlanApprovalOverlay  # noqa: PLC0415
            if req is not None:
                if getattr(req, "kind", "tool") == "plan_review":
                    overlay = PlanApprovalOverlay(req, approval_svc, workspace.overlays.hide)
                else:
                    overlay = ApprovalOverlay(req, approval_svc, workspace.overlays.hide)
                workspace.overlays.show(overlay)
            else:
                if isinstance(workspace.overlays.widget, (ApprovalOverlay, PlanApprovalOverlay)):
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
        )
        return bool(self._cmd_dispatcher.dispatch(text, context))

    def route(self, msg: str) -> bool:
        """Return True if msg is a slash command and was dispatched."""
        if not msg.startswith("/"):
            return False
        if self.dispatch_slash(msg):
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

        try:
            if _wf_defn is not None:
                import dataclasses as _dc                              # noqa: PLC0415
                from agenthicc.workflow.runner import WorkflowRunner   # noqa: PLC0415
                _wf_config = _dc.replace(
                    self._wf_config_base, completed_turns=self._turn_count,
                )
                _wf_runner = WorkflowRunner(_wf_defn, _wf_config, ctx.mode_manager)
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
                    project_plugin_tools=ctx.project_plugins.all_tools,
                    mcp_registry=ctx.mcp_registry,
                    active_agent="default",
                    completed_turns=self._turn_count,
                    approval_svc=ctx.approval_svc,
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
        try:
            await self.run_turn(text)
        except asyncio.CancelledError:
            self._ctx.app_state.conversation.end_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            self._ctx.app_state.conversation.fail_turn(str(exc))
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

    def _schedule_workflow_resume(self) -> None:
        """If the kernel state has an unfinished workflow, schedule its resume."""
        from agenthicc.kernel.state import NodeStatus  # noqa: PLC0415
        k_state = self._ctx.processor.get_state()
        for wf in k_state.workflows.values():
            if wf.status in (NodeStatus.complete, NodeStatus.failed):
                continue
            if not wf.name:
                continue
            wf_defn = self._ctx.workflow_registry.get(wf.name)
            if wf_defn is None:
                continue
            context = _reconstruct_workflow_context(wf)
            self._ctx.app_state.conversation.notification.set(
                f"⟳ Resuming workflow '{wf.name}'…"
            )
            self._agent_task = asyncio.create_task(
                self._resume_workflow_task(wf_defn, context), name="agent-turn"
            )
            return  # resume one workflow at a time

    async def _resume_workflow_task(self, wf_defn: Any, context: Any) -> None:
        """Resume a WorkflowRunner with error handling matching agent_task_body."""
        from agenthicc.tui.input.unified_session import InputMode  # noqa: PLC0415
        from agenthicc.workflow.runner import WorkflowRunner       # noqa: PLC0415
        ctx = self._ctx
        self._input_session.set_mode(InputMode.STREAMING)
        ctx.approval_svc.reset_turn_memory()
        try:
            runner = WorkflowRunner(wf_defn, self._wf_config_base, ctx.mode_manager)
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
            ctx.app_state.conversation.end_turn()
            self._input_session.set_mode(InputMode.IDLE)
        except Exception as exc:
            ctx.app_state.conversation.fail_turn(str(exc))
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
        # PRD-94: auto-resume any incomplete workflow from a previous session.
        self._schedule_workflow_resume()
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
) -> None:
    """Reactive TUI session — single entry point, no legacy branches."""
    from agenthicc.tui.workspace import Workspace                        # noqa: PLC0415
    from agenthicc.tui.input.unified_session import UnifiedInputSession  # noqa: PLC0415

    ctx = await _build_session_context(resume_id, cli_overrides)
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
        await session.run()
    finally:
        ctx.session_log.close()
        if ctx.mcp_registry:
            await ctx.mcp_registry.shutdown()


# ── sync entry point (unchanged) ─────────────────────────────────────────────

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
