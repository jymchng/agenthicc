"""Native workflow graph and bounded run parameters for UI/UX Pro Max."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agenthicc.workflows.checkpoint import WorkflowCheckpointTopology
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin
from agenthicc.workflows.ui_ux_pro_max.state import (
    PHASES_BY_PROFILE,
    UiUxProMaxContext,
    UiUxProMaxState,
)

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.workflows.ui_ux_pro_max.runner import UiUxProMaxRunner


@dataclass
class UiUxProMaxParams(WorkflowParams):
    """Per-phase model overrides and bounded review/remediation controls."""

    product_discovery_model: str = ""
    design_intelligence_model: str = ""
    targeted_design_research_model: str = ""
    design_system_synthesis_model: str = ""
    design_system_review_model: str = ""
    stack_adaptation_model: str = ""
    experience_architecture_model: str = ""
    design_implementation_model: str = ""
    visual_ux_audit_model: str = ""
    design_remediation_model: str = ""
    design_acceptance_model: str = ""
    delivery_model: str = ""
    max_design_review_attempts: int = 2
    max_remediation_cycles: int = 2
    max_phase_turns: int = 24

    def __post_init__(self) -> None:
        self.max_design_review_attempts = _bounded_int(self.max_design_review_attempts, 2, 1, 5)
        self.max_remediation_cycles = _bounded_int(self.max_remediation_cycles, 2, 0, 5)
        self.max_phase_turns = _bounded_int(self.max_phase_turns, 24, 1, 40)

    def get_phase_models(self) -> dict[str, str]:
        return {
            "product_discovery": self.product_discovery_model,
            "design_intelligence": self.design_intelligence_model,
            "targeted_design_research": self.targeted_design_research_model,
            "design_system_synthesis": self.design_system_synthesis_model,
            "design_system_review": self.design_system_review_model,
            "stack_adaptation": self.stack_adaptation_model,
            "experience_architecture": self.experience_architecture_model,
            "design_implementation": self.design_implementation_model,
            "visual_ux_audit": self.visual_ux_audit_model,
            "design_remediation": self.design_remediation_model,
            "design_acceptance": self.design_acceptance_model,
            "delivery": self.delivery_model,
        }


def _bounded_int(value: object, default: int, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(upper, max(lower, value))


_PHASE_PROMPT_SEEDS: dict[str, str] = {
    "product_discovery": "Inspect the project and produce an evidence-grounded product brief.",
    "design_intelligence": "Use verified UI/UX Pro Max design-system research capabilities.",
    "targeted_design_research": "Use the narrowest verified UX/accessibility research applicable.",
    "design_system_synthesis": "Synthesize one coherent, evidence-informed master design system.",
    "design_system_review": "Return APPROVED, REVISE, or BLOCKED against explicit criteria.",
    "stack_adaptation": "Adapt decisions to the actual framework, project, and platform.",
    "experience_architecture": "Specify the actual screens, states, component graph, and implementation plan.",
    "design_implementation": "Implement only the approved design and interaction contract.",
    "visual_ux_audit": "Audit rendered or terminal output and report evidence-backed findings.",
    "design_remediation": "Fix only actionable findings, with bounded retries.",
    "design_acceptance": "Assign explicit evidence-backed status to every criterion.",
    "delivery": "Report implementation, evidence, test outcomes, and limitations honestly.",
}


class UiUxProMax(WorkflowPlugin):
    """Research, synthesize, adapt, implement, audit, and accept UI/UX work."""

    name = "ui_ux_pro_max"
    description = "Specialized, evidence-led UI/UX design and implementation workflow"
    mode_bindings: list[str] = []
    max_total_phase_runs = 64
    phases = [
        PhaseSpec(
            name=name,
            agent_type=(
                "executor"
                if name == "design_implementation" or name == "design_remediation"
                else "reviewer"
                if name == "design_system_review"
                or name == "visual_ux_audit"
                or name == "design_acceptance"
                else "planner"
            ),
            max_turns=40 if name == "design_implementation" else 24,
            max_iterations=5,
            mode_override=None,
            system_prompt_override=_PHASE_PROMPT_SEEDS[name],
        )
        for name in (
            "product_discovery",
            "design_intelligence",
            "targeted_design_research",
            "design_system_synthesis",
            "design_system_review",
            "stack_adaptation",
            "experience_architecture",
            "design_implementation",
            "visual_ux_audit",
            "design_remediation",
            "design_acceptance",
            "delivery",
        )
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> UiUxProMaxRunner:
        from agenthicc.workflows.ui_ux_pro_max.runner import UiUxProMaxRunner

        return UiUxProMaxRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> UiUxProMaxParams:
        model_names = tuple(f"{phase}_model" for phase in _PHASE_PROMPT_SEEDS)
        values = {
            name: value.strip()
            for name in model_names
            if isinstance(value := source.get(name), str)
        }
        return UiUxProMaxParams(
            **values,
            max_design_review_attempts=_bounded_int(
                source.get("max_design_review_attempts"), 2, 1, 5
            ),
            max_remediation_cycles=_bounded_int(source.get("max_remediation_cycles"), 2, 0, 5),
            max_phase_turns=_bounded_int(source.get("max_phase_turns"), 24, 1, 40),
        )

    @classmethod
    def resolve_checkpoint_topology(
        cls, context_payload: Mapping[str, object]
    ) -> WorkflowCheckpointTopology:
        profile_value = context_payload.get("route_profile", "broad")
        profile = (
            profile_value
            if isinstance(profile_value, str) and profile_value in PHASES_BY_PROFILE
            else "broad"
        )
        phase_names = PHASES_BY_PROFILE[profile]
        next_edges: list[str | None] = []
        reject_edges: list[str | None] = []
        for index, phase in enumerate(phase_names):
            next_phase = phase_names[index + 1] if index + 1 < len(phase_names) else None
            if phase == "design_system_review":
                reject = "design_system_synthesis"
            elif phase == "visual_ux_audit" and "design_remediation" in phase_names:
                reject = "design_remediation"
            else:
                reject = None
            next_edges.append(next_phase)
            reject_edges.append(reject)
        return WorkflowCheckpointTopology(
            workflow_name=cls.name,
            topology_version="ui_ux_pro_max.v2",
            profile=profile,
            phase_names=phase_names,
            next_phases=tuple(next_edges),
            reject_phases=tuple(reject_edges),
        )

    @classmethod
    def create_initial_context(
        cls,
        intent: str,
        run_id: str,
        memory: object | None = None,
    ) -> UiUxProMaxContext:
        return UiUxProMaxContext(
            intent=intent[:20_000],
            run_id=run_id[:256],
            shared_memory=cast("ShortTermMemory | None", memory),
        )

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object] | None:
        if not isinstance(context, UiUxProMaxContext):
            return None
        return context.to_checkpoint_payload()

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> UiUxProMaxContext | None:
        state = payload.get("state")
        if (
            not isinstance(payload.get("intent"), str)
            or not isinstance(payload.get("run_id"), str)
            or not isinstance(state, str)
            or state not in UiUxProMaxState.__members__
        ):
            return None
        try:
            return UiUxProMaxContext.from_checkpoint_payload(
                payload,
                memory=cast("ShortTermMemory | None", memory),
            )
        except ValueError:
            return None
