"""AgentTurnContext — typed configuration for a single agent turn (PRD-92).

All parameters that were previously passed as ``Any`` to ``_run_agent_turn``
are gathered here as a frozen dataclass with real types.  ``AgentTurnRunner``
reads from this context; call sites construct it and pass it to the runner.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NotRequired, TypedDict

from agenthicc.supervised_tasks import SupervisedTasks


@dataclass(frozen=True)
class QueuedMessageSource:
    """Session-owned source for input accepted during an agent turn.

    ``claim`` yields one FIFO message at a safe tool boundary.
    """

    claim: Callable[[], str | None]


_queued_message_source: ContextVar[QueuedMessageSource | None] = ContextVar(
    "agenthicc_queued_message_source", default=None
)
_provider_diagnostic_context: ContextVar[dict[str, str | int] | None] = ContextVar(
    "agenthicc_provider_diagnostic_context", default=None
)
_background_task_owner: ContextVar[SupervisedTasks | None] = ContextVar(
    "agenthicc_background_task_owner", default=None
)


@contextmanager
def bind_background_task_owner(owner: SupervisedTasks) -> Iterator[None]:
    """Bind the owning session supervisor to background work started by a turn."""
    token = _background_task_owner.set(owner)
    try:
        yield
    finally:
        _background_task_owner.reset(token)


def current_background_task_owner() -> SupervisedTasks | None:
    """Return the session supervisor bound to the current turn, if any."""
    return _background_task_owner.get()


@contextmanager
def bind_queued_message_source(source: QueuedMessageSource) -> Iterator[None]:
    """Bind a session-owned input source to turns in the current task scope."""

    token = _queued_message_source.set(source)
    try:
        yield
    finally:
        _queued_message_source.reset(token)


def current_queued_message_source() -> QueuedMessageSource | None:
    """Return the input source bound to the current context, if any."""

    return _queued_message_source.get()


@contextmanager
def bind_provider_diagnostic_context(
    context: dict[str, str | int],
) -> Iterator[None]:
    """Bind bounded session/input correlation fields to provider failures.

    The mapping is intentionally mutable so a queued message source can update
    the active phase and message ID at the exact point it delivers that input.
    Callers must only store non-secret identifiers and small enum-like values.
    """
    token = _provider_diagnostic_context.set(context)
    try:
        yield
    finally:
        _provider_diagnostic_context.reset(token)


def current_provider_diagnostic_context() -> dict[str, str | int] | None:
    """Return the current turn's bounded provider diagnostic context."""
    return _provider_diagnostic_context.get()


if TYPE_CHECKING:
    from lauren_ai import IdempotencyLedger
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.config import ExecutionSettings
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.router import MemoryRouter
    from agenthicc.memory.vector import SemanticIndex
    from agenthicc.mentions.cache import MentionCache
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.tools.base import ToolLike
    from agenthicc.tools.mcp import McpToolRegistry
    from agenthicc.tools.mcp_manager import McpSessionManager
    from agenthicc.tui.conversation_store import AppState, ConversationStore
    from agenthicc.skills.loader import SkillPermissionSet, SkillDef
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.runners.prompt_contract import PromptContract
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy
    from agenthicc.agents.definitions import AgentSpec
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.subagents.types import SubagentTypeRegistry
    from agenthicc.config import AgenthiccConfig


def _new_subagent_registry() -> "SubagentTypeRegistry":
    from agenthicc.subagents.types import DEFAULT_REGISTRY

    return DEFAULT_REGISTRY.copy()


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
    approval_svc: "ApprovalBroker | None"
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
    provider_diagnostic_context: NotRequired[dict[str, str | int] | None]
    agent_spec: NotRequired["AgentSpec | None"]
    agents_registry: NotRequired["AgentsRegistry | None"]
    subagent_registry: NotRequired["SubagentTypeRegistry"]
    agent_config: NotRequired["AgenthiccConfig | None"]


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
    #: The primary agent's definition for a direct turn (AGX-09): its prompt,
    #: tool and capability allow-lists and ceiling restrict this turn.
    agent_spec: "AgentSpec | None" = None
    #: Definitions and provider profiles for ``@agent:NAME`` invocation (AGX-09).
    agents_registry: "AgentsRegistry | None" = None
    #: Session-owned base registry for spawn_subagents, before agent definitions project in.
    subagent_registry: "SubagentTypeRegistry" = field(default_factory=_new_subagent_registry)
    agent_config: "AgenthiccConfig | None" = None
    completed_turns: int = 0

    # ── approval / hooks ──────────────────────────────────────────────────────
    approval_svc: "ApprovalBroker | None" = None
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

    #: Redacted session/attempt/input correlation for provider error events.
    provider_diagnostic_context: dict[str, str | int] | None = None

    def __post_init__(self) -> None:
        """Inherit an explicitly bound session input source when not overridden."""

        source = current_queued_message_source()
        if source is not None and self.next_queued_message is None:
            object.__setattr__(self, "next_queued_message", source.claim)
        diagnostic_context = current_provider_diagnostic_context()
        if self.provider_diagnostic_context is None and diagnostic_context is not None:
            object.__setattr__(self, "provider_diagnostic_context", diagnostic_context)
