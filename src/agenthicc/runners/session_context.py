"""SessionContext — all session-scoped singletons (PRD-93).

No logic lives here.  ``TUISession`` reads from this context;
``_build_session_context`` constructs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
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
    from agenthicc.tools.mcp import McpToolRegistry


@dataclass
class SessionContext:
    """All session-scoped singletons — no logic, just data.

    Pass to ``TUISession.__init__``; read via ``self._ctx`` inside the session.
    """

    # ── kernel ────────────────────────────────────────────────────────────────
    processor:          "EventProcessor"
    app_state:          "AppState"
    session_log:        "SessionEventLog"

    # ── services ──────────────────────────────────────────────────────────────
    approval_svc:       "ApprovalService"
    mode_manager:       "ModeManager"
    command_bus:        "CommandBus"

    # ── registries ────────────────────────────────────────────────────────────
    workflow_registry:  "WorkflowRegistry"
    agents_registry:    "AgentsRegistry"
    cmd_registry:       "UnifiedCommandRegistry"
    trigger_registry:   "TriggerManager"

    # ── resources ─────────────────────────────────────────────────────────────
    agent_runner:       "AgentRunnerBase"
    session_memory:     "ShortTermMemory"
    mention_cache:      "MentionCache"
    skills:             dict
    project_plugins:    "PluginToolSet"
    mcp_registry:       "McpToolRegistry | None"

    # ── config ────────────────────────────────────────────────────────────────
    cfg:                "AgenthiccConfig"
    session_id:         str
    model_label:        str

    # ── ui ────────────────────────────────────────────────────────────────────
    console:            "Console"

    # ── memory (PRD-101) ──────────────────────────────────────────────────────
    memory_router:      "MemoryRouter | None"  = None
    semantic_index:     "SemanticIndex | None" = None
