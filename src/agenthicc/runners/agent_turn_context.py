"""AgentTurnContext — typed configuration for a single agent turn (PRD-92).

All parameters that were previously passed as ``Any`` to ``_run_agent_turn``
are gathered here as a frozen dataclass with real types.  ``AgentTurnRunner``
reads from this context; call sites construct it and pass it to the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.config import ExecutionSettings
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tui.conversation_store import AppState, ConversationStore
    from agenthicc.skills.loader import SkillPermissionSet, SkillDef


@dataclass(frozen=True)
class AgentTurnContext:
    """All configuration for one agent turn — immutable after construction.

    Pass to ``AgentTurnRunner(ctx).run()`` to execute the turn.
    """

    # ── required ──────────────────────────────────────────────────────────────
    text: str
    runner: "AgentRunnerBase | None"  # transport + signals
    processor: "EventProcessor"  # kernel event bus

    # ── memory ────────────────────────────────────────────────────────────────
    session_memory: "ShortTermMemory | None" = None
    max_agent_turns: int = 200

    # ── observability ─────────────────────────────────────────────────────────
    conv_store: "ConversationStore | None" = None
    app_state: "AppState | None" = None
    exec_cfg: "ExecutionSettings | None" = None

    # ── content injection ─────────────────────────────────────────────────────
    skills: "dict[str, SkillDef] | None" = None
    skill_permissions: "SkillPermissionSet | None" = None
    mention_cache: "MentionCache | None" = None
    project_plugin_tools: "list | None" = None
    mcp_registry: "McpToolRegistry | None" = None

    # ── agent identity ────────────────────────────────────────────────────────
    active_agent: "str | None" = None  # None → "default"
    completed_turns: int = 0

    # ── approval / hooks ──────────────────────────────────────────────────────
    approval_svc: "ApprovalService | None" = None

    # ── memory (PRD-101) ──────────────────────────────────────────────────────
    memory_router: "MemoryRouter | None" = None
    semantic_index: "SemanticIndex | None" = None

    # ── output capture ────────────────────────────────────────────────────────
    output_collector: "list[str] | None" = None
    system_prompt_suffix: str = ""

    # ── transport retry (PRD-126) ─────────────────────────────────────────────
    #: Absolute ``time.monotonic()`` deadline for retry scheduling.  When a turn
    #: timeout wraps the caller, this prevents scheduling a retry that cannot
    #: meaningfully run before the timeout fires.  ``None`` = no deadline.
    retry_deadline_monotonic: "float | None" = None

    # ── run resumption (PRD-129 Phase 3) ──────────────────────────────────────
    #: When re-driving a turn that a crash interrupted, the original turn id to
    #: reuse (so durable tool records line up).  ``None`` = a fresh turn.
    resume_turn_id: "str | None" = None
    #: A pre-seeded ``DurableIdempotencyLedger`` for the resumed turn, loaded with
    #: the tools the crashed attempt already ran.  ``None`` = build a fresh one.
    resume_ledger: "object | None" = None
