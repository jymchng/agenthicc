"""AppState — the immutable, event-sourced kernel state (PRD-01)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from uuid import uuid4

__all__ = [
    "AgentInstance",
    "AgentStatus",
    "AppState",
    "Intent",
    "IntentStatus",
    "NodeStatus",
    "PermissionRule",
    "SecurityPolicy",
    "SystemSettings",
    "Task",
    "ToolRegistration",
    "Workflow",
    "WorkflowNode",
]


class IntentStatus(str, Enum):
    pending = "pending"
    validating = "validating"
    planning = "planning"
    running = "running"
    complete = "complete"
    failed = "failed"
    rejected = "rejected"


class NodeStatus(str, Enum):
    pending = "pending"
    running = "running"
    complete = "complete"
    failed = "failed"
    skipped = "skipped"


class AgentStatus(str, Enum):
    idle = "idle"
    busy = "busy"
    terminated = "terminated"


@dataclass(frozen=True)
class Intent:
    intent_id: str
    raw_text: str
    status: IntentStatus
    workflow_id: str | None
    created_at: float
    metadata: dict[str, object] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    task_id: str
    label: str
    dependencies: frozenset[str]
    status: NodeStatus
    agent_id: str | None = None
    result: object = None
    error: str | None = None


@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    intent_id: str
    nodes: dict[str, WorkflowNode]
    status: NodeStatus
    created_at: float
    name: str = ""  # workflow definition name — used for resume lookup
    intent_text: str = ""  # original intent text — used to reconstruct WorkflowContext


@dataclass(frozen=True)
class Task:
    task_id: str
    workflow_id: str
    node_id: str
    description: str
    status: NodeStatus
    assigned_agent_id: str | None
    created_at: float
    result: object = None


@dataclass(frozen=True)
class AgentInstance:
    agent_id: str
    agent_type: str
    status: AgentStatus
    current_task_id: str | None
    parent_agent_id: str | None
    created_at: float
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRegistration:
    tool_id: str
    name: str
    description: str
    parameters_schema: dict[str, object]
    is_builtin: bool = False
    source_agent_id: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    tool_pattern: str
    action: str  # "allow" | "deny" | "require_confirmation"
    conditions: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityPolicy:
    permission_rules: tuple[PermissionRule, ...] = ()
    default_action: str = "deny"


@dataclass(frozen=True)
class SystemSettings:
    max_concurrent_intents: int = 10
    max_parallel_tasks: int = 20
    agent_pool_size: int = 30
    snapshot_every_n_events: int = 100
    event_log_path: str = ".agenthicc/events.jsonl"
    snapshot_path: str = ".agenthicc/snapshot.json"


@dataclass(frozen=True)
class AppState:
    """Immutable kernel state. ``with_*`` helpers return updated copies that
    share unchanged sub-dicts by reference (copy-on-write)."""

    session_id: str
    run_id: str
    intents: dict[str, Intent]
    workflows: dict[str, Workflow]
    tasks: dict[str, Task]
    agents: dict[str, AgentInstance]
    tools: dict[str, ToolRegistration]
    hooks: dict[str, dict[str, object]]
    snapshot_index: int
    settings: SystemSettings
    policy: SecurityPolicy
    agent_types: dict[str, type] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        settings: SystemSettings | None = None,
        policy: SecurityPolicy | None = None,
    ) -> AppState:
        return cls(
            session_id=uuid4().hex,
            run_id=uuid4().hex,
            intents={},
            workflows={},
            tasks={},
            agents={},
            tools={},
            hooks={},
            snapshot_index=0,
            settings=settings or SystemSettings(),
            policy=policy or SecurityPolicy(),
        )

    # ── copy-on-write helpers ────────────────────────────────────────────

    def with_intent(self, intent: Intent) -> AppState:
        return replace(self, intents={**self.intents, intent.intent_id: intent})

    def with_workflow(self, workflow: Workflow) -> AppState:
        return replace(self, workflows={**self.workflows, workflow.workflow_id: workflow})

    def with_task(self, task: Task) -> AppState:
        return replace(self, tasks={**self.tasks, task.task_id: task})

    def with_agent(self, agent: AgentInstance) -> AppState:
        return replace(self, agents={**self.agents, agent.agent_id: agent})

    def with_tool(self, tool: ToolRegistration) -> AppState:
        return replace(self, tools={**self.tools, tool.name: tool})

    def with_hook(self, hook_id: str, hook_spec: dict[str, object]) -> AppState:
        return replace(self, hooks={**self.hooks, hook_id: hook_spec})
