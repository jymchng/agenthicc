"""AgentTurnContext — typed configuration for a single agent turn (PRD-92).

All parameters that were previously passed as ``Any`` to ``_run_agent_turn``
are gathered here as a frozen dataclass with real types.  ``AgentTurnRunner``
reads from this context; call sites construct it and pass it to the runner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from lauren_ai import IdempotencyLedger
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.config import ExecutionSettings
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tools.mcp_manager import McpSessionManager
    from agenthicc.tui.conversation_store import AppState, ConversationStore
    from agenthicc.skills.loader import SkillPermissionSet, SkillDef
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.runners.prompt_contract import PromptContract
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy


class AgentTurnOptions(TypedDict):
    """Keyword arguments shared by workflow phase turn invocations."""

    runner: "AgentRunnerBase"
    processor: "EventProcessor"
    session_memory: "ShortTermMemory | None"
    conversation_id: NotRequired[str]
    max_agent_turns: int
    conv_store: "ConversationStore | None"
    app_state: "AppState | None"
    exec_cfg: "ExecutionSettings | None"
    skills: "dict[str, SkillDef] | None"
    skill_permissions: "SkillPermissionSet | None"
    mention_cache: "MentionCache | None"
    project_plugin_tools: "list[ToolLike] | None"
    mcp_registry: "McpToolRegistry | McpSessionManager | None"
    active_agent: str
    completed_turns: int
    approval_svc: "ApprovalService | None"
    output_collector: "list[str] | None"
    command_outcomes: "list[dict[str, object]] | None"
    next_queued_message: NotRequired[Callable[[], str | None] | None]
    usage_ledger: NotRequired["UsageLedger | None"]
    browser_manager: NotRequired[object | None]
    system_prompt_suffix: str
    prompt_contract: NotRequired["PromptContract | None"]
    excluded_capabilities: NotRequired[frozenset[str]]
    allowed_tool_names: NotRequired[frozenset[str] | None]
    workspace_access: NotRequired["WorkspaceAccessPolicy | None"]


@dataclass(frozen=True)
class AgentTurnContext:
    """All configuration for one agent turn — immutable after construction.

    Pass to ``AgentTurnRunner(ctx).run()`` to execute the turn.
    """

    # ── required ──────────────────────────────────────────────────────────────
    text: str
    runner: "AgentRunnerBase"  # transport + signals
    processor: "EventProcessor"  # kernel event bus

    # ── memory ────────────────────────────────────────────────────────────────
    session_memory: "ShortTermMemory | None" = None
    conversation_id: str = ""
    max_agent_turns: int = 200

    # ── observability ─────────────────────────────────────────────────────────
    conv_store: "ConversationStore | None" = None
    app_state: "AppState | None" = None
    exec_cfg: "ExecutionSettings | None" = None

    # ── content injection ─────────────────────────────────────────────────────
    skills: "dict[str, SkillDef] | None" = None
    skill_permissions: "SkillPermissionSet | None" = None
    mention_cache: "MentionCache | None" = None
    project_plugin_tools: "list[ToolLike] | None" = None
    mcp_registry: "McpToolRegistry | McpSessionManager | None" = None

    # ── agent identity ────────────────────────────────────────────────────────
    active_agent: "str | None" = None  # None → "default"
    completed_turns: int = 0

    # ── approval / hooks ──────────────────────────────────────────────────────
    approval_svc: "ApprovalService | None" = None
    workspace_access: "WorkspaceAccessPolicy | None" = None

    # ── memory (PRD-101) ──────────────────────────────────────────────────────
    memory_router: "MemoryRouter | None" = None
    semantic_index: "SemanticIndex | None" = None

    # ── output capture ────────────────────────────────────────────────────────
    output_collector: "list[str] | None" = None
    system_prompt_suffix: str = ""
    #: Structured workflow prompt/cache contract.  When present, changing
    #: phase content is rendered into the append-only user context rather than
    #: rebuilding the cacheable system prefix.
    prompt_contract: "PromptContract | None" = None
    #: Capability tags excluded from this turn's tool surface. Unannotated
    #: phase-control tools remain available.
    excluded_capabilities: frozenset[str] = frozenset()
    #: Optional exact tool-name allowlist for phase-local workflows.  ``None``
    #: preserves the normal full registry surface.
    allowed_tool_names: frozenset[str] | None = None

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
    resume_ledger: "IdempotencyLedger | None" = None

    #: Structured outcomes from command tools used during this turn.
    command_outcomes: "list[dict[str, object]] | None" = None

    #: Claim one ordinary message queued during this turn after a tool batch
    #: finishes. The returned message is appended to the shared agent memory
    #: before the next model call; slash commands remain FIFO-gated until the
    #: outer turn is idle and can route them locally.
    next_queued_message: "Callable[[], str | None] | None" = None

    #: Canonical session-scoped provider usage accounting (PRD-157).
    usage_ledger: "UsageLedger | None" = None

    #: Session-scoped browser manager used to reset per-turn browser quotas.
    browser_manager: object | None = None
