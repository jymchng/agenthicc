"""SessionContext — all session-scoped singletons (PRD-93).

No logic lives here.  ``TUISession`` reads from this context;
``_build_session_context`` constructs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.runners.session_conversation import SessionConversation
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime import CommandBus, ModeManager
    from agenthicc.tui.runtime.session_log import SessionEventLog
    from agenthicc.workflows.registry import WorkflowRegistry
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.config import AgenthiccConfig
    from agenthicc.tui.trigger import TriggerManager
    from agenthicc.commands.registry import UnifiedCommandRegistry
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.skills.loader import SkillDef
    from agenthicc.background.terminals import TerminalManager
    from agenthicc.session_service import SessionService
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.tools.cloakbrowser import BrowserSessionManager
    from agenthicc.tools.workspace_access import WorkspaceScope, WorkspaceAccessPolicy
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
    approval_svc: "ApprovalService"
    mode_manager: "ModeManager"
    command_bus: "CommandBus"

    # ── registries ────────────────────────────────────────────────────────────
    workflow_registry: "WorkflowRegistry"
    agents_registry: "AgentsRegistry"
    cmd_registry: "UnifiedCommandRegistry"
    trigger_registry: "TriggerManager"

    # ── resources ─────────────────────────────────────────────────────────────
    agent_runner: "AgentRunnerBase"
    session_memory: "ShortTermMemory"
    mention_cache: "MentionCache"
    skills: "dict[str, SkillDef]"
    project_plugins: "PluginToolSet"
    mcp_registry: "McpToolRegistry | None"
    terminal_manager: "TerminalManager"

    # ── config ────────────────────────────────────────────────────────────────
    cfg: "AgenthiccConfig"
    session_id: str
    model_label: str

    # ── ui ────────────────────────────────────────────────────────────────────
    console: "Console"
    session_conversation: "SessionConversation | None" = None

    # ── memory (PRD-101) ──────────────────────────────────────────────────────
    memory_router: "MemoryRouter | None" = None
    semantic_index: "SemanticIndex | None" = None

    # ── run resumption (PRD-129 Phase 3) ──────────────────────────────────────
    #: A plan to re-drive a turn the prior session left incomplete (crash
    #: mid-turn).  ``None`` on a clean start.
    pending_resume: "object | None" = None

    # ── extension lifecycle ──────────────────────────────────────────────────
    #: Canonical names loaded from normal slash-command plugin files.  Kept
    #: separately so a reload can restore built-ins after a plugin is removed.
    command_plugin_names: set[str] = field(default_factory=set)

    # ── client-neutral session boundary (PRD-150) ───────────────────────────
    session_service: "SessionService | None" = None
    kernel_projection_task: "Task[None] | None" = None

    # ── provider usage (PRD-157) ─────────────────────────────────────────────
    usage_ledger: "UsageLedger | None" = None

    # ── optional browser integration (PRD-159) ─────────────────────────────
    browser_manager: "BrowserSessionManager | None" = None
    browser_tools: list["ToolLike"] = field(default_factory=list)

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
