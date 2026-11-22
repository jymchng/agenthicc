"""SessionContext — all session-scoped singletons (PRD-93).

No logic lives here.  ``TUISession`` reads from this context;
``_build_session_context`` constructs it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from lauren_ai._agents._runner import AgentRunnerBase
    from agenthicc.memory.journaled import JournaledShortTermMemory
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime import CommandBus, ModeManager
    from agenthicc.tui.runtime.session_log import SessionEventLog
    from agenthicc.workflows.registry import WorkflowRegistry
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.subagents.types import SubagentTypeRegistry
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.trigger import TriggerManager
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tools.mcp_manager import McpSessionManager
    from agenthicc.skills.loader import SkillDef
    from agenthicc.background.terminals import TerminalManager
    from agenthicc.session_service import SessionService
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.tools.cloakbrowser import BrowserSessionManager
    from agenthicc.tools.workspace_access import WorkspaceScope, WorkspaceAccessPolicy
    from agenthicc.runners.session_lease import SessionOwnerLease
    from agenthicc.runners.startup import StartupCoordinator
    from agenthicc.runners.run_coordinator import ResumePlan
    from agenthicc.runners.reload import ReloadService
    from agenthicc.runners.authored import AuthoredPublisher
    from asyncio import Task


@dataclass
class SessionContext:
    """All session-scoped singletons — no logic, just data.

    Pass to ``TUISession.__init__``; read via ``self._ctx`` inside the session.
    """

    # ── kernel ────────────────────────────────────────────────────────────────
    processor: "EventProcessor"
    app_state: "AppState"
    session_log: "SessionEventLog"

    # ── services ──────────────────────────────────────────────────────────────
    approval_svc: "ApprovalBroker"
    mode_manager: "ModeManager"
    command_bus: "CommandBus"

    # ── registries ────────────────────────────────────────────────────────────
    workflow_registry: "WorkflowRegistry"
    agents_registry: "AgentsRegistry"
    subagent_registry: "SubagentTypeRegistry"
    cmd_registry: "UnifiedCommandRegistry"
    trigger_registry: "TriggerManager"

    # ── resources ─────────────────────────────────────────────────────────────
    agent_runner: "AgentRunnerBase"
    session_conversation: "SessionConversation"
    mention_cache: "MentionCache"
    skills: InitVar[dict[str, SkillDef]]
    project_plugins: InitVar[PluginToolSet]
    mcp_registry: "McpToolRegistry | McpSessionManager | None"
    terminal_manager: "TerminalManager"

    # ── config ────────────────────────────────────────────────────────────────
    cfg: "AgenthiccConfig"
    session_id: str
    model_label: str

    # ── ui ────────────────────────────────────────────────────────────────────
    console: "Console"
    llm_config_error: str | None = None
    config_path: str | None = None

    # PRD-172: compatibility alias points at the one session-scoped manager;
    # no workflow or turn is allowed to create its own MCP registry.
    mcp_manager: "McpSessionManager | None" = None

    # ── memory (PRD-101) ──────────────────────────────────────────────────────
    memory_router: "MemoryRouter | None" = None
    semantic_index: "SemanticIndex | None" = None

    # ── run resumption (PRD-129 Phase 3) ──────────────────────────────────────
    #: A plan to re-drive a turn the prior session left incomplete (crash
    #: mid-turn).  ``None`` on a clean start.
    pending_resume: "ResumePlan | None" = None

    # ── extension lifecycle ──────────────────────────────────────────────────
    #: Canonical names loaded from normal slash-command plugin files.  Kept
    #: separately so a reload can restore built-ins after a plugin is removed.
    command_plugin_names: InitVar[tuple[str, ...]] = ()
    reload: ReloadService = field(init=False)
    authored: AuthoredPublisher = field(init=False)

    # ── client-neutral session boundary (PRD-150) ───────────────────────────
    session_service: "SessionService | None" = None
    kernel_projection_task: "Task[None] | None" = None

    # ── provider usage (PRD-157) ─────────────────────────────────────────────
    usage_ledger: "UsageLedger | None" = None

    # ── optional browser integration (PRD-159) ─────────────────────────────
    browser_manager: "BrowserSessionManager | None" = None
    browser_tools: "Iterable[ToolLike]" = field(default_factory=tuple)

    # ── invocation configuration ────────────────────────────────────────────
    cfg_overrides: tuple[str, ...] = ()
    cfg_secret_overrides: tuple[str, ...] = ()

    # ── invocation selection ────────────────────────────────────────────────
    #: Explicit ``--workflow`` selection for the initial TUI turn.  Headless
    #: callers use CLIContext.workflow_name directly when dispatching lines.
    initial_workflow: str | None = None

    # ── workspace access (PRD-168) ──────────────────────────────────────────
    #: Canonical roots shared by filesystem tools, commands, mentions, and
    #: workflow turns.  The policy itself is also bound to the session task's
    #: context so legacy tool wrappers cannot silently create a second scope.
    workspace_scope: "WorkspaceScope | None" = None
    workspace_access: "WorkspaceAccessPolicy | None" = None

    # ── visual resume (PRD-158) ──────────────────────────────────────────────
    #: True when this TUI was opened against an existing session and should
    #: replay its persisted scroll transcript after the Live block mounts.
    resumed: bool = False

    # ── process ownership (PRD-171) ─────────────────────────────────────────
    #: The outer lease for every durable session attachment.  It is released
    #: by the owning runner after all session resources have been closed.
    owner_lease: "SessionOwnerLease | None" = None
    #: Goal run this session executes, when started by ``agenthicc run``.
    goal_run_id: str = ""

    # PRD-176: readiness/timing state shared by the TUI, headless runner, and
    # deferred optional integrations.  It is observational only; runtime
    # resources retain their existing ownership boundaries.
    startup: "StartupCoordinator | None" = None

    def __post_init__(
        self,
        skills: dict[str, SkillDef],
        project_plugins: PluginToolSet,
        command_plugin_names: tuple[str, ...],
    ) -> None:
        from agenthicc.runners.authored import AuthoredPublisher
        from agenthicc.runners.reload import ReloadService

        root = self.workspace_scope.primary_root if self.workspace_scope is not None else Path.cwd()
        default_skill_directory = self.cfg.skills.default_skill_directory
        self.reload = ReloadService(
            processor=self.processor,
            project_dir=root / ".agenthicc",
            user_dir=Path.home() / ".agenthicc",
            workflow_registry=self.workflow_registry,
            command_registry=self.cmd_registry,
            subagent_registry=self.subagent_registry,
            skill_user_dir=Path(default_skill_directory).expanduser()
            if default_skill_directory
            else None,
            skills=skills,
            tools=project_plugins.all_tools,
            command_plugin_names=command_plugin_names,
        )
        self.authored = AuthoredPublisher(root, self.reload, self.processor)

    @property
    def session_memory(self) -> "JournaledShortTermMemory":
        """The session's provider memory, folded from its journal on first use.

        The TUI loads it on a worker thread after the first frame (AGX-02);
        reading it earlier blocks until that fold completes.
        """
        return self.session_conversation.memory
