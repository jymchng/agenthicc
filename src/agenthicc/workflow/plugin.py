"""Workflow plugin types — phase topology, definitions, context (PRD-81, PRD-87).

PhaseSpec describes WHERE and WHEN an agent runs in a workflow graph.
HOW the agent behaves (system prompt, model) lives in AgentsRegistry.
WHICH tools it receives is determined by PhaseSpec.resolved_allowed_caps
intersected with the session mode's blocked_capabilities ceiling.
"""
from __future__ import annotations

import abc
import dataclasses
import re
import time
from dataclasses import field


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
    name:                          str
    agent_type:                    str            = "auto"
    system_prompt_override:        str            = ""     # overrides registry prompt for this phase
    mode_override:                 str | None     = None   # RuntimeMode name to apply during this phase
    allowed_capabilities:          object         = None   # frozenset | None
    allowed_capabilities_override: object         = None   # frozenset | None
    max_turns:                     int            = 20
    output_schema:                 str | None     = None
    next:                          str | None     = None
    on_reject:                     str | None     = None
    on_error:                      str | None     = None
    max_iterations:                int            = 3
    parallel_with:                 tuple[str,...] = ()

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
    name:          str
    description:   str              = ""
    phases:        tuple[PhaseSpec,...] = ()
    mode_bindings: tuple[str,...]   = ()
    source:        str              = "builtin"
    path:          str | None       = None

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
        return {"content": text, "approved": None}
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
