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
from collections.abc import Mapping
from typing import TYPE_CHECKING

from agenthicc.tools.capabilities import ToolCapability

if TYPE_CHECKING:
    from agenthicc.workflows.base_runner import BaseWorkflowRunner
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.tui.runtime.mode_manager import ModeManager


# ── WorkflowParams — per-workflow tunable parameters (PRD-111) ───────────────


@dataclasses.dataclass
class WorkflowParams:
    """Tunable parameters for one workflow run.

    Distinct from ``WorkflowConfig`` (session-scoped infrastructure).
    Subclasses add typed fields for workflow-specific settings such as
    per-phase model overrides and override ``get_phase_models()`` to expose
    those fields as a phase → model mapping.

    Populated from ``[workflows.<name>]`` in TOML, ``--set`` CLI overrides,
    or environment variables; defaults come from field declarations.
    """

    def get_phase_models(self) -> dict[str, str]:
        """Return a mapping of phase name → model ID.

        Empty string values mean "use the global execution model".
        Override in subclasses to expose typed per-phase model fields.
        """
        return {}

    def model_for_phase(self, phase_name: str, fallback: str) -> str:
        """Return the model to use for *phase_name*, or *fallback* when unset."""
        m = self.get_phase_models().get(phase_name, "")
        return m if m else fallback


# ── PhaseRole — typed string constants matching builtin agent type names ──────


class PhaseRole(str):
    """String constants equal to builtin agent registry keys.

    Using PhaseRole.PLANNER is identical to using the string "planner".
    """

    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    EXPLORER = "explorer"
    VERIFIER = "verifier"
    HUMAN = "human"
    CUSTOM = "custom"
    AUTO = "auto"


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

    allowed_capabilities: frozenset[ToolCapability] | None = None
    """frozenset[ToolCapability] | None — tool capability allowlist for this phase.
    None means fall back to ROLE_DEFAULT_ALLOWED[agent_type], then the session mode ceiling."""

    allowed_capabilities_override: frozenset[ToolCapability] | None = None
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

    require_explicit_completion: bool = False
    """When True, _run_phase loops until the phase's completion tool is called
    (mark_execute_complete for execute phases).  Each loop iteration runs a full
    _run_agent_turn with a continuation prompt; the shared ShortTermMemory carries
    full context forward so the agent resumes exactly where it left off.
    If the loop exhausts max_iterations continuations without the event being set,
    the phase returns approved=False."""

    require_plan_finalization: bool = False
    """When True, _run_phase loops until finalize_plan() is called.  If the agent
    ends its turn without calling finalize_plan(), a reminder prompt re-states
    the user's task so the agent stays focused on producing and approving a plan."""

    require_explicit_review: bool = False
    """When True, the agent must call approve_review() or reject_review() instead
    of outputting an XML <review> tag.  Eliminates brittle text parsing where
    phrases like 'The code is approved' were misclassified as rejection because
    the content did not start with the literal word 'approved'."""

    parallel_with: tuple[str, ...] = ()
    """Names of sibling phases to run concurrently with this one via asyncio.gather."""

    @property
    def resolved_allowed_caps(self) -> frozenset[ToolCapability] | None:
        """Effective allowed capabilities: override → field → role default."""
        if self.allowed_capabilities_override is not None:
            return self.allowed_capabilities_override
        if self.allowed_capabilities is not None:
            return self.allowed_capabilities
        from agenthicc.agents.plugin import ROLE_DEFAULT_ALLOWED  # noqa: PLC0415

        return ROLE_DEFAULT_ALLOWED.get(self.agent_type)


# ── Runtime output types ──────────────────────────────────────────────────────


@dataclasses.dataclass
class PhaseOutput:
    phase_name: str
    role: str  # agent_type that ran this phase
    full_text: str = ""
    structured: dict[str, object] | None = None
    approved: bool | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    agent_id: str = ""
    duration_s: float = 0.0


@dataclasses.dataclass
class PhaseRunRecord:
    phase_name: str
    role: str
    approved: bool | None
    output_summary: str
    iteration: int
    duration_s: float


@dataclasses.dataclass
class WorkflowRun:
    run_id: str
    workflow_name: str
    intent: str
    current_phase: str | None
    phase_history: list[PhaseRunRecord] = field(default_factory=list)
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    total_phases: int = 0
    current_phase_index: int = 0
    """Zero-based position of current_phase within WorkflowDefinition.phases.
    Used by the TUI to display "Phase N/M" where N = current_phase_index + 1.
    Stays fixed at the definition position regardless of how many times the
    phase is retried via on_reject, so plan always shows Phase 1/M."""
    current_phase_model: str = ""
    """Model override active for the current phase (PRD-118).
    Non-empty when the phase uses a per-phase model that differs from the
    global ``execution.model``; the status bar shows this instead of the
    session model while the run is active.  Empty string = show session model."""


@dataclasses.dataclass
class WorkflowContext:
    intent: str
    run_id: str
    workflow_name: str
    phase_outputs: dict[str, PhaseOutput] = field(default_factory=dict)

    def as_system_block(self) -> str:
        if not self.phase_outputs:
            return f"[WORKFLOW CONTEXT]\nOriginal intent: {self.intent}"
        lines = ["[WORKFLOW CONTEXT]", f"Original intent: {self.intent}", "", "Completed phases:"]
        for name, output in self.phase_outputs.items():
            snippet = output.full_text[:200]
            if len(output.full_text) > 200:
                snippet += "..."
            lines.append(f"- {name} ({output.role}): {snippet}")
        return "\n".join(lines)

    def add_output(self, output: PhaseOutput) -> None:
        self.phase_outputs[output.phase_name] = output


# ── Output schema parsing ─────────────────────────────────────────────────────


def _parse_output_schema(text: str, schema: str | None) -> dict[str, object] | None:
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
    """ABC for Python workflow definitions (PRD-116).

    Subclasses declare workflow identity and behaviour as class attributes and
    override the three factory classmethods to return specialised objects.

    The registry stores the plugin *class* (wrapped in a ``WorkflowEntry`` for
    provenance).  Agenthicc calls ``build_runner()``, ``build_params()``, and
    the query helpers directly on the class.
    """

    # ── Class-level identity / structure (set as class attributes) ────────────
    name: str = ""
    description: str = ""
    mode_bindings: list[str] = []
    phases: list[PhaseSpec] = []
    max_total_phase_runs: int = 0
    """Hard ceiling on total phase runs (0 = no cap)."""

    # ── Query helpers ─────────────────────────────────────────────────────────

    @classmethod
    def first_phase(cls) -> PhaseSpec | None:
        """Return the first phase, or ``None`` if the workflow has no phases."""
        return cls.phases[0] if cls.phases else None

    @classmethod
    def get_phase(cls, name: str) -> PhaseSpec | None:
        """Return the phase named *name*, or ``None``."""
        return next((p for p in cls.phases if p.name == name), None)

    @classmethod
    def phase_names(cls) -> list[str]:
        """Return an ordered list of phase names."""
        return [p.name for p in cls.phases]

    # ── Factory classmethods (override to return specialised objects) ─────────

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> BaseWorkflowRunner:
        """Return the runner for this workflow.

        Default: generic ``WorkflowRunner`` driven by ``cls.phases``.
        Override to return a specialised runner (e.g. ``CodePlanRunner``).
        """
        from agenthicc.workflows.default.runner import WorkflowRunner  # noqa: PLC0415

        return WorkflowRunner(cls, config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> WorkflowParams:
        """Return typed params built from *source* (merged TOML/CLI/env dict).

        Default: returns base ``WorkflowParams()`` with no phase model overrides.
        Override to return a specialised ``WorkflowParams`` subclass.
        """
        return WorkflowParams()


# ── WorkflowEntry — registry provenance record (PRD-116) ─────────────────────
# Defined after WorkflowPlugin so it can annotate type[WorkflowPlugin].


@dataclasses.dataclass(frozen=True)
class WorkflowEntry:
    """Registry artifact: plugin class + discovery provenance.

    The registry stores one ``WorkflowEntry`` per workflow name.  All
    workflow metadata is accessed via ``plugin_cls.*``; ``source`` and
    ``path`` record where the plugin was discovered.
    """

    plugin_cls: type[WorkflowPlugin]
    source: str = "builtin"  # "builtin" | "user" | "project"
    path: str | None = None  # filesystem path for user / project plugins
