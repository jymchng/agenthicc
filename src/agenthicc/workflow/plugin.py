"""Workflow plugin types — graph-based workflow model (PRD-101)."""
from __future__ import annotations

import abc
import dataclasses
import time
from dataclasses import field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lauren_ai._config import AgentConfig, LLMConfig


# ── PhaseRole — string constants for agent_type ───────────────────────────────

class PhaseRole(str):
    """String constants equal to builtin agent registry keys."""
    PLANNER  = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    EXPLORER = "explorer"
    VERIFIER = "verifier"
    HUMAN    = "human"
    CUSTOM   = "custom"
    AUTO     = "auto"


# ── Audit trail (used by runner for phase history) ────────────────────────────

@dataclasses.dataclass
class PhaseRunRecord:
    phase_name:     str
    role:           str
    approved:       bool | None   # kept for audit; not used for routing
    output_summary: str
    iteration:      int
    duration_s:     float


@dataclasses.dataclass
class WorkflowRun:
    run_id:              str
    workflow_name:       str
    intent:              str
    current_phase:       str | None
    phase_history:       list[PhaseRunRecord] = field(default_factory=list)
    status:              str                  = "running"
    created_at:          float                = field(default_factory=time.time)
    total_phases:        int                  = 0
    current_phase_index: int                  = 0
    """Zero-based definition position of current_phase.
    Used by the TUI to display 'Phase N/M' — N = current_phase_index + 1.
    Fixed at the definition index regardless of retry count."""


# ── WorkflowPlugin ABC ────────────────────────────────────────────────────────

class WorkflowPlugin(abc.ABC):
    """ABC for Python workflow definitions (graph-based).

    Subclasses define a ``graph: WorkflowGraph`` class attribute.
    ``to_definition()`` returns that graph.  The loader calls this method to
    register workflows from plugin files.
    """

    name:          str       = ""
    description:   str       = ""
    mode_bindings: list[str] = []

    def to_definition(
        self, source: str = "user", path: str | None = None,
    ) -> WorkflowGraph:
        graph: WorkflowGraph | None = getattr(type(self), "graph", None)
        if graph is None:
            raise NotImplementedError(
                f"WorkflowPlugin subclass {type(self).__name__!r} must define "
                f"a 'graph: WorkflowGraph' class attribute or override to_definition()."
            )
        # Propagate class-level metadata (mode_bindings, source, path) to the
        # graph object.  The class is the single source of truth for these fields;
        # the graph attribute carries the topology only.
        cls_bindings = tuple(type(self).mode_bindings)
        return dataclasses.replace(
            graph,
            mode_bindings=cls_bindings,
            source=source,
            path=path,
        )


# ── PRD-101 graph types ───────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class EdgeGate:
    """Human-in-the-loop gate shown before a transition commits.

    ``kind`` is resolved against ``_OVERLAY_REGISTRY`` in tui_session.py:
      'plan_review'   → PlanApprovalOverlay
      'tool_approval' → ApprovalOverlay
    Plugins register custom kinds at startup: _OVERLAY_REGISTRY["kind"] = Cls.
    """

    kind:  str = "plan_review"
    title: str = ""


@dataclasses.dataclass(frozen=True)
class EdgeSpec:
    """A directed edge in a WorkflowGraph.

    The agent calls ``complete_phase(next=label)`` to follow this edge.
    ``target=None`` marks a terminal edge — the workflow ends after it.
    """

    target: str | None
    """Destination node name.  None = terminal."""

    label:  str
    """Semantic name the agent uses: 'approve', 'reject', 'complete', …"""

    gate:   EdgeGate | None = None
    """When set, complete_phase suspends and shows an overlay before committing."""


@dataclasses.dataclass(frozen=True)
class PhaseNode:
    """One node in a WorkflowGraph.

    ``agent_config`` (lauren_ai.AgentConfig) carries system_prompt and max_turns.
    ``llm_config``   (lauren_ai.LLMConfig)   overrides the session provider/model.
    Both default to None (inherit session config).
    """

    name: str
    """Unique node identifier within the graph."""

    agent_config: "AgentConfig | None" = None
    """Per-node behaviour: system_prompt, max_turns, thinking, parallel_tool_calls, …
    Passed to run_stream(config_override=…).  None = session defaults."""

    llm_config: "LLMConfig | None" = None
    """Per-node provider/model override.  None = use session transport."""

    agent_type: str = "auto"
    """Key into AgentsRegistry for capability defaults."""

    edges: tuple[EdgeSpec, ...] = ()
    """Outgoing edges.  Empty = terminal node."""

    allowed_capabilities: Any = None
    """frozenset[ToolCapability] | None — tool allowlist for this node."""

    mode_override: str | None = None
    """RuntimeMode to activate for this node's turn (restored in finally)."""

    max_continuations: int = 10
    """Max continuation loop iterations before giving up."""

    parallel_with: tuple[str, ...] = ()
    """Names of sibling nodes to run concurrently."""


@dataclasses.dataclass(frozen=True)
class WorkflowGraph:
    """A directed graph of PhaseNodes — the authoritative workflow definition."""

    name:  str
    """Unique identifier used in registry lookups and kernel events."""

    entry: str
    """Name of the first node."""

    nodes: dict[str, PhaseNode]
    """Ordered mapping: name → node.  Insertion order = Phase N/M display order."""

    description:          str           = ""
    mode_bindings:        tuple[str,...] = ()
    source:               str           = "builtin"
    path:                 str | None    = None
    max_total_phase_runs: int           = 0
    """Opt-in global cap on total node runs (0 = unlimited)."""

    def get_node(self, name: str) -> PhaseNode | None:
        return self.nodes.get(name)

    def node_index(self, name: str) -> int:
        keys = list(self.nodes.keys())
        return keys.index(name) if name in self.nodes else 0

    def node_names(self) -> list[str]:
        return list(self.nodes.keys())


@dataclasses.dataclass
class DataBus:
    """Structured blackboard of node outputs — replaces WorkflowContext."""

    intent:      str
    run_id:      str
    outputs:     dict[str, dict]  = dataclasses.field(default_factory=dict)
    edge_history: dict[str, str]  = dataclasses.field(default_factory=dict)

    def set(self, node_name: str, data: dict) -> None:
        self.outputs[node_name] = data

    def get(self, node_name: str) -> dict | None:
        return self.outputs.get(node_name)

    def record_edge(self, node_name: str, edge_label: str) -> None:
        self.edge_history[node_name] = edge_label

    def as_context_block(self) -> str:
        if not self.outputs:
            return f"[WORKFLOW CONTEXT]\nOriginal intent: {self.intent}"
        lines = ["[WORKFLOW CONTEXT]", f"Original intent: {self.intent}", ""]
        for name, output in self.outputs.items():
            lines.append(f"{name}:")
            for k, v in output.items():
                if k.startswith("_"):
                    continue
                v_str = str(v)
                if len(v_str) > 500:
                    v_str = v_str[:500] + "…"
                lines.append(f"  {k}: {v_str}")
        return "\n".join(lines)


@dataclasses.dataclass
class NodeResult:
    """Outcome of running one PhaseNode — runner-internal routing."""

    node_name:  str
    edge_label: str | None   # None = terminal or failed
    output:     dict
    duration_s: float = 0.0
