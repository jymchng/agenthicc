"""Workflow plugin types — phase topology, definitions, context (PRD-81, PRD-87).

``PhaseSpec`` describes where and when an agent runs in a workflow graph and,
for the generic declarative runner, supplies the phase-specific prompt seed.
The role prompt comes from ``AgentsRegistry`` unless
``PhaseSpec.system_prompt_override`` is non-empty. A custom runner that
implements its own phase methods is different: its explicit ``system_prompt``
argument is the source of truth and it must opt into any ``PhaseSpec`` prompt
it wants to use. Tool visibility is determined by
``PhaseSpec.resolved_allowed_caps`` intersected with the session mode's
blocked-capabilities ceiling.
"""

from __future__ import annotations

import abc
import dataclasses
import re
import time
from dataclasses import field
from collections.abc import Collection, Iterable, Mapping
from typing import TYPE_CHECKING

from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities

if TYPE_CHECKING:
    from agenthicc.workflows.base_runner import BaseWorkflowRunner
    from agenthicc.workflows.checkpoint import WorkflowCheckpointTopology
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.tui.runtime.mode_manager import ModeManager


# These are the built-in workflow handoff tools.  The prompt helper also uses
# ToolCapability.CONTROL below, so custom workflow tools marked with
# ``@tool_control`` are advertised without needing to be added here.  The
# name-based fallback preserves compatibility with older plugin callables that
# predate capability metadata.
_PHASE_TRANSITION_TOOL_NAMES = frozenset(
    {
        "request_plan_approval",
        "finalize_plan",
        "exit_code_plan",
        "mark_execute_complete",
        "approve_review",
        "reject_review",
        "request_design_approval",
        "finalize_design",
        "exit_create_workflow",
        "mark_generation_complete",
        "approve_workflow",
        "reject_workflow",
        "submit_tool_plan",
        "confirm_generation_complete",
        "approve_tool",
        "reject_tool",
        "submit_toc",
        "submit_research",
        "submit_responsive_research",
        "approve_research_baseline",
        "approve_degraded_research",
        "reject_research_baseline",
        "confirm_chapter_complete",
        "confirm_assets_ready",
        "confirm_front_matter_ready",
        "confirm_back_matter_ready",
        "mark_book_complete",
        "reject_book",
        "submit_analysis",
        "submit_plan",
        "request_reanalysis",
        "scaffold_complete",
        "component_built",
        "component_verified",
        "component_verification_failed",
        "final_verify_passed",
        "final_verify_failed",
        "submit_release_plan",
        "release_passed",
        "release_blocked",
    }
)

# These tools carry workflow control metadata because they mutate durable
# workflow state, but they do not end the current phase.  Keeping them out of
# the transition list prevents the generic prompt helper from telling the
# agent to stop after merely discovering additional work.
_NON_TRANSITION_CONTROL_TOOL_NAMES = frozenset({"append_goal", "insert_goal"})


_WORKFLOW_USER_QUESTION_REMINDER = (
    "[REQUIREMENTS CLARIFICATION]\n"
    "Do not guess when an unanswered requirement could materially change the work. "
    "You can ask the user multiple focused questions, including several questions "
    "in one call, by using the `ask_user` tool. Ask about goals, constraints, "
    "priorities, choices, and acceptance criteria whenever they are unclear; use "
    "the available exploration tools first when the answer can be discovered from "
    "the project. Incorporate the user's answers into this workflow before making "
    "the next phase decision."
)


def phase_transition_instruction(
    tools: Iterable[object],
    *,
    phase_name: str = "",
    expected_tool_names: Collection[str] | None = None,
) -> str:
    """Describe the phase's available handoff tools for the agent.

    Workflow transitions are control-plane effects, not prose conventions.  The
    runner appends this block to every phase system prompt so the LLM can see
    the exact callable names it must use.  ``expected_tool_names`` is used by
    the generic declarative runner, which injects several shared tool groups and
    therefore needs to narrow the list to the current phase.
    """
    available: list[str] = []
    mutations: list[str] = []
    expected = set(expected_tool_names) if expected_tool_names is not None else None
    for tool in tools:
        name_value: object = getattr(tool, "__name__", "")
        name = name_value if isinstance(name_value, str) else ""
        if not name:
            fallback: object = getattr(tool, "name", "")
            name = fallback if isinstance(fallback, str) else ""
        # ``ask_user`` is marked CONTROL for safe capability handling, but it
        # clarifies requirements and does not transition the workflow.
        is_control_tool = name != "ask_user" and ToolCapability.CONTROL in get_tool_capabilities(
            tool
        )
        is_known_transition = name in _PHASE_TRANSITION_TOOL_NAMES
        if name in _NON_TRANSITION_CONTROL_TOOL_NAMES:
            if expected is None or name in expected:
                if name not in mutations:
                    mutations.append(name)
            continue
        if (is_control_tool or is_known_transition) and (expected is None or name in expected):
            if name not in available:
                available.append(name)

    phase_label = f" in the {phase_name!r} phase" if phase_name else ""
    if not available and not mutations:
        return (
            "[PHASE TRANSITION TOOLS]\n"
            f"No phase-transition tool is available{phase_label}. Do not claim that the "
            "workflow advanced in prose or invent a replacement tool; the enclosing "
            "runner will apply its declared phase graph after this turn."
        )

    sections: list[str] = []
    if available:
        names = ", ".join(f"`{name}`" for name in available)
        sections.append(
            "[PHASE TRANSITION TOOLS]\n"
            f"Available transition tool(s){phase_label}: {names}. Call exactly the "
            "appropriate tool when this phase's work is complete or a documented branch "
            "must be taken. A phase changes only after a transition tool call succeeds; "
            "prose such as 'done' or 'moving to the next phase' never advances the "
            "workflow. After a successful transition call, stop and let the runner take "
            "control."
        )
    if mutations:
        names = ", ".join(f"`{name}`" for name in mutations)
        sections.append(
            "[GOAL LIST MUTATION TOOLS]\n"
            f"Available non-transition tool(s){phase_label}: {names}. Use these only "
            "to record newly discovered concrete work. They do not change phase or "
            "finish the current goal; after a successful mutation, continue the "
            "current goal and call its normal transition tool when complete."
        )
    return "\n\n".join(sections)


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
    """Prompt seed used by the generic declarative ``WorkflowRunner``.

    When non-empty, this replaces the selected ``AgentsRegistry`` role prompt
    for this phase; it does *not* replace the global/base system prompt and it
    does not remove framework-owned requirements clarification or transition
    instructions. The generic runner appends those framework instructions,
    builds a ``PromptContract``, and places this phase-specific content in the
    dynamic phase-context region so changing phases does not invalidate the
    stable cache prefix.

    Custom runners returned by ``WorkflowPlugin.build_runner()`` do not consume
    this field automatically. Their phase methods must pass the desired
    prompt explicitly to ``run_phase(system_prompt=...)`` or to their own turn
    helper. In that path, the explicit argument is authoritative. A phase with
    ``agent_type="human"`` does not invoke an agent turn, so this field is not
    used for that phase.
    """

    mode_override: str | None = None
    """RuntimeMode name to activate for this phase (e.g. 'Yolo' to allow writes)."""

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

    terminal_wait_policy: str = "foreground"
    """Terminal execution default for this phase: ``foreground`` or ``background``.

    ``background`` is an explicit workflow declaration, not inference from a
    shell command.  It makes the existing run_bash/run_command tools return an
    owned handle; the phase can then call wait_terminal when it needs the
    result.
    """

    command_lifecycle: str = "oneshot"
    """Command lifecycle required by this phase: ``oneshot`` or ``service``."""

    require_successful_commands: bool = False
    """Gate phase transitions on successful command outcomes when enabled."""

    require_readiness: bool = False
    """Require a successful service readiness result before transition."""

    def __post_init__(self) -> None:
        if self.terminal_wait_policy not in {"foreground", "background"}:
            raise ValueError("terminal_wait_policy must be 'foreground' or 'background'")
        if self.command_lifecycle not in {"oneshot", "service"}:
            raise ValueError("command_lifecycle must be 'oneshot' or 'service'")
        if self.command_lifecycle == "service" and self.terminal_wait_policy != "background":
            raise ValueError("service command_lifecycle requires terminal_wait_policy='background'")
        if self.require_readiness and self.command_lifecycle != "service":
            raise ValueError("require_readiness requires command_lifecycle='service'")

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
    current_phase: str | None = None
    phase_iteration: int = 0
    phase_iterations: dict[str, int] = field(default_factory=dict)
    next_phase: str | None = None

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
    required_startup_phases: tuple[str, ...] = ()
    """Session readiness phases required before this workflow can run.

    Names are owned by the session startup coordinator (for example ``mcp`` or
    ``extensions``). Optional integrations should not be listed here; their
    failure must leave unrelated local turns usable.
    """

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

    @classmethod
    def resolve_checkpoint_topology(
        cls, context_payload: Mapping[str, object]
    ) -> "WorkflowCheckpointTopology":
        """Return the phase graph that gives meaning to checkpoint indexes.

        Fixed declarative workflows inherit this implementation.  A workflow
        whose runner filters, profiles, or otherwise computes its phases must
        override it and derive the same ordered graph from the persisted
        context that its runner will use after restart.
        """
        del context_payload
        from agenthicc.workflows.checkpoint import topology_from_phase_specs

        return topology_from_phase_specs(cls.name, tuple(cls.phases))

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

    @classmethod
    def create_initial_context(
        cls,
        intent: str,
        run_id: str,
        memory: object | None = None,
    ) -> object | None:
        """Optionally create typed state before runner construction.

        The session always creates a durable identity and an identity-only
        bootstrap context before calling plugin setup. Custom workflows may
        override this hook to provide their typed checkpoint context even when
        ``build_params()`` or ``build_runner()`` fails. ``memory`` is the
        already-open session memory; implementations must attach it to the
        context without persisting the memory object itself.
        """
        del intent, run_id, memory
        return None

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object] | None:
        """Encode a custom runner context for PRD-156 checkpoints.

        Built-in runners are encoded by the framework. A downstream runner with
        its own context must override this hook and return bounded,
        JSON-compatible fields. The session memory, locks, events, clients, and
        other live resources must not be included; the paired restore hook
        receives the already-open session memory. The inherited ``None`` is
        intentionally a fail-closed default for unsupported third-party
        contexts, not a declaration that the workflow is resumable.
        """
        return None

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> object | None:
        """Restore a custom context produced by
        :meth:`checkpoint_context_to_payload`.

        ``memory`` is the already-open session conversation and must be
        attached by the custom codec rather than reconstructed independently.
        Downstream custom runners must override both codec hooks together.
        """
        return None


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
