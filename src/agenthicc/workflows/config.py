"""WorkflowConfig — all session-scoped singletons for WorkflowRunner (PRD-95)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.plugins.discovery import PluginToolSet
    from agenthicc.tools.base import ToolLike
    from agenthicc.skills.loader import SkillDef
    from agenthicc.tui.conversation_store import ConversationStore, AppState
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tools.mcp_manager import McpSessionManager
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.config import AgenthiccConfig
    from agenthicc.workflows.plugin import WorkflowParams
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.runners.workflow_handle import WorkflowRunHandle
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.tools.cloakbrowser import BrowserSessionManager
    from agenthicc.tools.workspace_access import WorkspaceScope, WorkspaceAccessPolicy


@dataclass(frozen=True)
class WorkflowConfig:
    """All session-scoped singletons passed to WorkflowRunner.

    Constructed once per TUI session; shared across all workflow runs in that
    session.  ``completed_turns`` is the only field that varies per run — use
    ``dataclasses.replace(config, completed_turns=n)`` to get a per-run copy.
    """

    conv_store: "ConversationStore"
    app_state: "AppState"
    processor: "EventProcessor"
    agent_runner: "AgentRunnerBase"
    approval_svc: "ApprovalService | None"
    cfg: "AgenthiccConfig"
    skills: "dict[str, SkillDef]"
    plugin_tools: "PluginToolSet | list[ToolLike]"
    mcp_registry: "McpToolRegistry | McpSessionManager | None"
    mention_cache: "MentionCache"
    agents_registry: "AgentsRegistry"
    memory_router: "MemoryRouter | None" = field(default=None)
    semantic_index: "SemanticIndex | None" = field(default=None)
    completed_turns: int = field(default=0)
    params: "WorkflowParams | None" = field(default=None)
    """Per-workflow tunable parameters (phase model overrides, etc.) — PRD-111."""
    terminal_wait_policies: dict[str, str] = field(default_factory=dict)
    """Phase-name to terminal policy map (PRD-149)."""
    next_queued_message: Callable[[], str | None] | None = None
    """Claim ordinary TUI input at a completed tool boundary, when present."""
    session_memory: "ShortTermMemory | None" = None
    """The session-wide provider conversation shared by direct/workflow turns."""
    conversation_id: str = ""
    """Stable session conversation identifier passed to lauren-ai."""
    workflow_handle: "WorkflowRunHandle | None" = None
    """Active workflow lifecycle handle, when this config runs a workflow."""
    usage_ledger: "UsageLedger | None" = None
    """Session-scoped provider usage ledger (PRD-157)."""
    browser_manager: "BrowserSessionManager | None" = None
    """Session-owned browser lifecycle; live browser objects never enter checkpoints."""
    browser_tools: list["ToolLike"] = field(default_factory=list)
    """Session-bound CloakBrowser tools; empty when the optional feature is disabled."""
    workspace_scope: "WorkspaceScope | None" = None
    """Canonical filesystem roots for this workflow's parent session."""
    workspace_access: "WorkspaceAccessPolicy | None" = None
    """Mode-aware path policy shared with all phase turns and tools."""

    def all_plugin_tools(self) -> list["ToolLike"]:
        """Return project tools while accepting legacy list-based configs."""
        if isinstance(self.plugin_tools, list):
            tools = list(self.plugin_tools)
        else:
            tools = list(self.plugin_tools.all_tools)
        return [*tools, *self.browser_tools]
