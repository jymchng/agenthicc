"""AgentTurnContext — typed configuration for a single agent turn (PRD-92).

All parameters that were previously passed as ``Any`` to ``_run_agent_turn``
are gathered here as a frozen dataclass with real types.  ``AgentTurnRunner``
reads from this context; call sites construct it and pass it to the runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Import real types for IDE/type-checker; all are deferred at runtime to
    # avoid circular imports and keep startup fast.
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.tui.conversation_store import ConversationStore
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.tools.approval import ApprovalService


@dataclass(frozen=True)
class AgentTurnContext:
    """All configuration for one agent turn — immutable after construction.

    Pass to ``AgentTurnRunner(ctx).run()`` to execute the turn.
    """

    # ── required ──────────────────────────────────────────────────────────────
    text:      str
    runner:    "AgentRunnerBase"     # transport + signals
    processor: "EventProcessor"      # kernel event bus

    # ── memory ────────────────────────────────────────────────────────────────
    session_memory:  "ShortTermMemory | None" = None
    max_agent_turns: int                      = 200

    # ── observability ─────────────────────────────────────────────────────────
    conv_store: "ConversationStore | None" = None
    app_state:  Any                        = None   # tui.AppState; Any avoids circular
    exec_cfg:   Any                        = None   # ExecutionSettings; Any avoids circular

    # ── content injection ─────────────────────────────────────────────────────
    skills:               "dict | None"       = None
    mention_cache:        "MentionCache | None" = None
    project_plugin_tools: "list | None"       = None
    mcp_registry:         Any                 = None

    # ── agent identity ────────────────────────────────────────────────────────
    active_agent:     "str | None" = None   # None → "default"
    completed_turns:  int          = 0

    # ── approval / hooks ──────────────────────────────────────────────────────
    approval_svc: "ApprovalService | None" = None

    # ── output capture ────────────────────────────────────────────────────────
    output_collector:     "list[str] | None" = None
    system_prompt_suffix: str                = ""
