"""WorkflowConfig — all session-scoped singletons for WorkflowRunner (PRD-95)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.tui.conversation_store import ConversationStore, AppState
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.config import AgenthiccConfig


@dataclass(frozen=True)
class WorkflowConfig:
    """All session-scoped singletons passed to WorkflowRunner.

    Constructed once per TUI session; shared across all workflow runs in that
    session.  ``completed_turns`` is the only field that varies per run — use
    ``dataclasses.replace(config, completed_turns=n)`` to get a per-run copy.
    """

    conv_store:      "ConversationStore"
    app_state:       "AppState"
    processor:       "EventProcessor"
    agent_runner:    "AgentRunnerBase"
    approval_svc:    "ApprovalService | None"
    cfg:             "AgenthiccConfig"
    skills:          dict
    plugin_tools:    list
    mcp_registry:    Any             # McpToolRegistry | None
    mention_cache:   "MentionCache"
    agents_registry: "AgentsRegistry"
    memory_router:   "MemoryRouter | None"  = field(default=None)
    semantic_index:  "SemanticIndex | None" = field(default=None)
    completed_turns: int                    = field(default=0)
