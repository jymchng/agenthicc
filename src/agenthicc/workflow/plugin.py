"""Workflow plugin types — phase topology, definitions, context (PRD-81, PRD-87, PRD-101).

PRD-101 introduces the graph-based workflow model:
  WorkflowGraph  — replaces WorkflowDefinition
  PhaseNode      — replaces PhaseSpec
  EdgeSpec       — replaces spec.next / spec.on_reject
  EdgeGate       — configures the overlay shown before a transition
  DataBus        — replaces WorkflowContext (structured dicts, no text truncation)
  NodeResult     — replaces PhaseOutput for runner-internal routing

The legacy types (PhaseSpec, WorkflowDefinition, WorkflowContext, PhaseOutput)
remain for backward compatibility and are used by WorkflowRunner's legacy path.
"""
from __future__ import annotations

import abc
import dataclasses
import re
import time
from dataclasses import field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lauren_ai._config import AgentConfig, LLMConfig


# ── PhaseRole — typed string constants matching builtin agent type names ──────

class PhaseRole(str):
    """String constants equal to builtin agent registry keys.

    Using PhaseRole.PLANNER is identical to using the string "planner".
    """
    PLANNER  = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    EXPLORER = "explorer"
    VERIFIER = "verifier"
    HUMAN    = "human"
    CUSTOM   = "custom"
    AUTO     = "auto"


# ── PhaseSpec ─────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class PhaseSpec:
    """Describes one node in a workflow phase graph.

    agent_type  — key into AgentsRegistry; defaults to "auto".
    allowed_capabilities — frozenset[ToolCapability] | None:
        None  → use ROLE_DEFAULT_ALLOWED[agent_type], then mode ceiling.
        frozenset → only tools whose caps ⊆ this set reach @use_tools.
    allowed_capabilities_override — explicit per-instance override; takes
        priority over allowed_capabilities and role default.
    """
    name: str
    """Unique phase identifier within the workflow; used as the transition target in next/on_reject."""

    agent_type: str = "auto"
    """Key into AgentsRegistry that selects the system prompt and allowed capabilities for this phase."""

    system_prompt_override: str = ""
    """When non-empty, replaces the registry's system prompt for this phase entirely."""

    mode_override: str | None = None
    """RuntimeMode name to activate for the duration of this phase (e.g. 'Auto' to allow writes)."""

    allowed_capabilities: object = None
    """frozenset[ToolCapability] | None — tool capability allowlist for this phase.
    None means fall back to ROLE_DEFAULT_ALLOWED[agent_type], then the session mode ceiling."""

    allowed_capabilities_override: object = None
    """Explicit per-instance capability override; takes priority over allowed_capabilities and role default."""

    max_turns: int = 20
    """Maximum number of LLM sub-turns (tool-call → response cycles) within a single phase run."""

    output_schema: str | None = None
    """Schema name used to parse structured output from the phase's full_text ('plan', 'review_result', 'free_text')."""

    next: str | None = None
    """Name of the phase to run after this one completes successfully; None ends the workflow."""

    on_reject: str | None = None
    """Name of the phase to run when this phase's output has approved=False; enables retry loops."""

    on_error: str | None = None
    """Name of the phase to run when this phase raises an unhandled exception (reserved, not yet used)."""

    max_iterations: int = -1
    """Maximum number of times this specific phase may be entered during one workflow run.
    -1 means unlimited.  Any positive integer is a hard per-phase ceiling.
    When require_explicit_completion=True this also caps the number of continuation
    turns within the phase (default 10 when -1)."""

    require_plan_finalization: bool = False
    """When True, _run_phase gates on plan_event after _run_agent_turn returns.
    The phase succeeds only if finalize_plan() was called during the turn.
    Any other end-of-turn returns approved=False.  Set on the plan phase only."""

    require_explicit_completion: bool = False
    """When True, _run_phase loops until the phase's completion tool is called
    (mark_execute_complete for execute phases).  Each loop iteration runs a full
    _run_agent_turn with a continuation prompt; the shared ShortTermMemory carries
    full context forward so the agent resumes exactly where it left off.
    If the loop exhausts max_iterations continuations without the event being set,
    the phase returns approved=False."""

    parallel_with: tuple[str, ...] = ()
    """Names of sibling phases to run concurrently with this one via asyncio.gather."""

    @property
    def resolved_allowed_caps(self) -> object:  # frozenset | None
        """Effective allowed capabilities: override → field → role default."""
        if self.allowed_capabilities_override is not None:
            return self.allowed_capabilities_override
        if self.allowed_capabilities is not None:
            return self.allowed_capabilities
        from agenthicc.agents.plugin import ROLE_DEFAULT_ALLOWED  # noqa: PLC0415
        return ROLE_DEFAULT_ALLOWED.get(self.agent_type)


# ── WorkflowDefinition ────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    """Unique workflow identifier; used in WorkflowRegistry lookups and kernel event payloads."""

    description: str = ""
    """Human-readable summary shown in mode menus and help text."""

    phases: tuple[PhaseSpec, ...] = ()
    """Ordered tuple of PhaseSpec nodes defining the workflow graph."""

    mode_bindings: tuple[str, ...] = ()
    """RuntimeMode names that automatically trigger this workflow when the user sends a message."""

    source: str = "builtin"
    """Origin of this definition: 'builtin', 'user', or 'project'."""

    path: str | None = None
    """Filesystem path of the .py file that defined this workflow (None for builtins)."""

    max_total_phase_runs: int = 0
    """Hard ceiling on total phase runs across all phases for one workflow run.
    0 (default) means no global cap — the execute↔review loop can iterate freely.
    Set to a positive integer to add an opt-in safety net for workflows that
    should not loop indefinitely (e.g. a workflow with no per-phase max_iterations)."""

    def get_phase(self, name: str) -> PhaseSpec | None:
        for phase in self.phases:
            if phase.name == name:
                return phase
        return None

    def first_phase(self) -> PhaseSpec | None:
        return self.phases[0] if self.phases else None

    def phase_names(self) -> list[str]:
        return [phase.name for phase in self.phases]


# ── Runtime output types ──────────────────────────────────────────────────────

@dataclasses.dataclass
class PhaseOutput:
    phase_name:  str
    role:        str        # agent_type that ran this phase
    full_text:   str        = ""
    structured:  dict | None = None
    approved:    bool | None = None
    metadata:    dict        = field(default_factory=dict)
    agent_id:    str         = ""
    duration_s:  float       = 0.0


@dataclasses.dataclass
class PhaseRunRecord:
    phase_name:     str
    role:           str
    approved:       bool | None
    output_summary: str
    iteration:      int
    duration_s:     float


@dataclasses.dataclass
class WorkflowRun:
    run_id:        str
    workflow_name: str
    intent:        str
    current_phase: str | None
    phase_history: list[PhaseRunRecord] = field(default_factory=list)
    status:        str                  = "running"
    created_at:    float                = field(default_factory=time.time)
    total_phases:  int                  = 0
    current_phase_index: int            = 0
    """Zero-based position of current_phase within WorkflowDefinition.phases.
    Used by the TUI to display "Phase N/M" where N = current_phase_index + 1.
    Stays fixed at the definition position regardless of how many times the
    phase is retried via on_reject, so plan always shows Phase 1/M."""


@dataclasses.dataclass
class WorkflowContext:
    intent:        str
    run_id:        str
    workflow_name: str
    phase_outputs: dict[str, PhaseOutput] = field(default_factory=dict)

    def as_system_block(self) -> str:
        if not self.phase_outputs:
            return f"[WORKFLOW CONTEXT]\nOriginal intent: {self.intent}"
        lines = ["[WORKFLOW CONTEXT]", f"Original intent: {self.intent}",
                 "", "Completed phases:"]
        for name, output in self.phase_outputs.items():
            snippet = output.full_text[:200]
            if len(output.full_text) > 200:
                snippet += "..."
            lines.append(f"- {name} ({output.role}): {snippet}")
        return "\n".join(lines)

    def add_output(self, output: PhaseOutput) -> None:
        self.phase_outputs[output.phase_name] = output


# ── Output schema parsing ─────────────────────────────────────────────────────

def _parse_output_schema(text: str, schema: str | None) -> dict | None:
    if schema is None:
        return None
    if schema == "plan":
        match = re.search(r"<plan>(.*?)</plan>", text, re.DOTALL)
        if match:
            return {"plan_text": match.group(1).strip()}
        return {"plan_text": text}
    if schema == "review_result":
        match = re.search(r"<review>(.*?)</review>", text, re.DOTALL | re.IGNORECASE)
        if match:
            content = match.group(1).strip()
            approved = content.lower() == "approved" or content.lower().startswith("approved")
            return {"content": content, "approved": approved}
        # No <review> tag — review turn ended without a decision.
        # Mark as incomplete so _run_phase retries the review phase itself,
        # not the execute phase (which is what approved=False would trigger).
        return {"content": text, "approved": None, "incomplete": True}
    if schema == "free_text":
        return {"text": text}
    return {"raw": text}


# ── WorkflowPlugin ABC ────────────────────────────────────────────────────────

class WorkflowPlugin(abc.ABC):
    """ABC for Python workflow definitions.

    Subclasses set name, description, mode_bindings, and phases as class
    attributes.  to_definition() converts them to a WorkflowDefinition.
    """
    name:          str            = ""
    description:   str            = ""
    mode_bindings: list[str]      = []
    phases:        list[PhaseSpec] = []

    def to_definition(
        self, source: str = "user", path: str | None = None,
    ) -> WorkflowDefinition:
        return WorkflowDefinition(
            name=self.name,
            description=self.description,
            phases=tuple(self.phases),
            mode_bindings=tuple(self.mode_bindings),
            source=source,
            path=path,
        )

    def determine_transition(
        self, spec: PhaseSpec, output: PhaseOutput, ctx: WorkflowContext,
    ) -> str | None:
        if output.approved is False and spec.on_reject:
            return spec.on_reject
        return spec.next


# ── PRD-101: Graph-based workflow types ───────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class EdgeGate:
    """Human-in-the-loop gate on an edge transition.

    When a PhaseNode's complete_phase tool is called with an edge that has a
    gate, the runner suspends and shows an overlay before committing.  The
    overlay class is resolved from ``_OVERLAY_REGISTRY`` in tui_session.py
    using ``kind`` as the key — new overlay types can be registered without
    touching EdgeGate or EdgeSpec.
    """

    kind: str = "plan_review"
    """Overlay identifier.  Built-in values:
      'plan_review'   → PlanApprovalOverlay  (plan text + approve/reject/instructions)
      'tool_approval' → ApprovalOverlay       (y/a/A/n inline approval)
    Plugins register custom kinds at startup via ``_OVERLAY_REGISTRY[kind] = cls``."""

    title: str = ""
    """Optional header override shown in the overlay.  Empty = use the node name."""


@dataclasses.dataclass(frozen=True)
class EdgeSpec:
    """A directed edge in a WorkflowGraph.

    The agent calls ``complete_phase(next=label)`` to follow this edge.
    ``target=None`` marks a terminal edge — the workflow ends after it.
    """

    target: str | None
    """Destination node name.  None = terminal (workflow ends after this edge)."""

    label: str
    """Semantic name the agent uses: 'approve', 'reject', 'complete', 'revise', …
    Shown to the agent in the complete_phase tool docstring."""

    gate: EdgeGate | None = None
    """When set, complete_phase suspends and shows an overlay before committing.
    None = automatic transition (no human step)."""


@dataclasses.dataclass(frozen=True)
class PhaseNode:
    """One node in a WorkflowGraph.

    Replaces PhaseSpec.  Transitions are expressed as EdgeSpec objects rather
    than plain ``next``/``on_reject`` strings.  The agent MUST call
    ``complete_phase(output=..., next=label)`` to advance — no implicit
    end-of-turn advancement.
    """

    name: str
    """Unique node identifier within the workflow graph."""

    agent_config: "AgentConfig | None" = None
    """Per-node agent behaviour from lauren_ai.AgentConfig
    (system_prompt, max_turns, temperature, thinking, parallel_tool_calls, …).
    Passed as config_override to run_stream().  None = session defaults.
    system_prompt replaces the old PhaseSpec.system_prompt_override.
    max_turns replaces the old top-level field."""

    llm_config: "LLMConfig | None" = None
    """Per-node provider/model override from lauren_ai.LLMConfig.
    None = use the session transport as-is (the common path).
    Set only when a node needs a different model or provider."""

    agent_type: str = "auto"
    """Key into AgentsRegistry for role-based capability defaults."""

    edges: tuple[EdgeSpec, ...] = ()
    """Outgoing edges.  Empty tuple = terminal node."""

    allowed_capabilities: Any = None
    """frozenset[ToolCapability] | None — tool capability allowlist.
    None = use ROLE_DEFAULT_ALLOWED[agent_type], then session mode ceiling."""

    mode_override: str | None = None
    """RuntimeMode name to activate for the duration of this node's turn.
    Restored in a finally block after the turn — even on error/cancellation."""

    max_continuations: int = 10
    """Maximum continuation loop iterations before giving up.
    Each iteration runs a full _run_agent_turn with a short continuation prompt.
    The shared ShortTermMemory carries context between iterations."""

    parallel_with: tuple[str, ...] = ()
    """Names of sibling nodes to run concurrently via asyncio.gather."""


@dataclasses.dataclass(frozen=True)
class WorkflowGraph:
    """A directed graph of PhaseNodes.

    Replaces WorkflowDefinition.  Edges are typed (EdgeSpec) rather than plain
    ``next``/``on_reject`` strings.  Transition routing is agent-driven via
    ``complete_phase(next=label)`` rather than inferred from ``approved: bool``.
    """

    name: str
    """Unique workflow identifier used in registry lookups and kernel events."""

    entry: str
    """Name of the first node to execute."""

    nodes: dict[str, "PhaseNode"]
    """Ordered mapping of node_name → PhaseNode.
    Insertion order = definition order used for Phase N/M display."""

    description: str = ""
    mode_bindings: tuple[str, ...] = ()
    source: str = "builtin"
    path: str | None = None

    max_total_phase_runs: int = 0
    """Opt-in global cap on total node runs for one workflow execution.
    0 (default) = unlimited.  Set to a positive integer to add a hard ceiling."""

    def get_node(self, name: str) -> "PhaseNode | None":
        return self.nodes.get(name)

    def node_index(self, name: str) -> int:
        """0-based position of *name* in insertion order."""
        keys = list(self.nodes.keys())
        return keys.index(name) if name in self.nodes else 0

    def node_names(self) -> list[str]:
        return list(self.nodes.keys())


@dataclasses.dataclass
class DataBus:
    """Structured blackboard of node outputs, readable by all subsequent nodes.

    Replaces WorkflowContext.  Each node writes a plain dict via complete_phase;
    downstream nodes read fields directly.  No 200-char text truncation.
    """

    intent: str
    """The original user intent that started this workflow run."""

    run_id: str
    """Unique run identifier matching the kernel WorkflowRun entry."""

    outputs: dict[str, dict] = dataclasses.field(default_factory=dict)
    """node_name → structured output dict written by complete_phase()."""

    edge_history: dict[str, str] = dataclasses.field(default_factory=dict)
    """node_name → edge_label taken — used by _find_resume_node."""

    def set(self, node_name: str, data: dict) -> None:
        self.outputs[node_name] = data

    def get(self, node_name: str) -> dict | None:
        return self.outputs.get(node_name)

    def record_edge(self, node_name: str, edge_label: str) -> None:
        self.edge_history[node_name] = edge_label

    def as_context_block(self) -> str:
        """Structured prompt injection for each node's system prompt."""
        if not self.outputs:
            return f"[WORKFLOW CONTEXT]\nOriginal intent: {self.intent}"
        lines = ["[WORKFLOW CONTEXT]", f"Original intent: {self.intent}", ""]
        for name, output in self.outputs.items():
            lines.append(f"{name}:")
            for k, v in output.items():
                if k.startswith("_"):
                    continue   # internal keys
                v_str = str(v)
                if len(v_str) > 500:
                    v_str = v_str[:500] + "…"
                lines.append(f"  {k}: {v_str}")
        return "\n".join(lines)


@dataclasses.dataclass
class NodeResult:
    """Outcome of running one PhaseNode — for runner-internal routing.

    Replaces PhaseOutput for the purpose of edge traversal.  PhaseRunRecord
    (the audit trail) is updated separately from this.
    """

    node_name: str
    """Name of the node that produced this result."""

    edge_label: str | None
    """Which edge the agent chose via complete_phase(next=label).
    None = terminal (no edges on node) or failed (complete_phase never called)."""

    output: dict
    """Structured output from complete_phase(output=…).  Empty on failure."""

    duration_s: float = 0.0
