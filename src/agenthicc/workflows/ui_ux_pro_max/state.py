"""Typed, bounded checkpoint state for the UI/UX Pro Max workflow."""

from __future__ import annotations

import dataclasses
import math
import time
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory


MAX_TEXT_CHARS = 4_000
MAX_LIST_ITEMS = 40
MAX_ITEM_CHARS = 512
MAX_RESUME_HISTORY = 100

PHASES_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "broad": (
        "product_discovery",
        "design_intelligence",
        "design_system_synthesis",
        "design_system_review",
        "stack_adaptation",
        "experience_architecture",
        "design_implementation",
        "visual_ux_audit",
        "design_remediation",
        "design_acceptance",
        "delivery",
    ),
    "targeted": (
        "product_discovery",
        "targeted_design_research",
        "stack_adaptation",
        "experience_architecture",
        "design_implementation",
        "visual_ux_audit",
        "design_remediation",
        "design_acceptance",
        "delivery",
    ),
    "accessibility_audit": (
        "product_discovery",
        "targeted_design_research",
        "stack_adaptation",
        "visual_ux_audit",
        "design_acceptance",
        "delivery",
    ),
}


class UiUxProMaxState(Enum):
    """Durable cursor for the specialized design-intelligence phases."""

    PRODUCT_DISCOVERY = auto()
    DESIGN_INTELLIGENCE = auto()
    TARGETED_DESIGN_RESEARCH = auto()
    DESIGN_SYSTEM_SYNTHESIS = auto()
    DESIGN_SYSTEM_REVIEW = auto()
    STACK_ADAPTATION = auto()
    EXPERIENCE_ARCHITECTURE = auto()
    DESIGN_IMPLEMENTATION = auto()
    VISUAL_UX_AUDIT = auto()
    DESIGN_REMEDIATION = auto()
    DESIGN_ACCEPTANCE = auto()
    DELIVERY = auto()
    COMPLETE = auto()
    BLOCKED = auto()
    FAILED = auto()

    @property
    def is_terminal(self) -> bool:
        return self in {
            UiUxProMaxState.COMPLETE,
            UiUxProMaxState.BLOCKED,
            UiUxProMaxState.FAILED,
        }


@dataclasses.dataclass
class PhaseArtifact:
    """Compact reference to a phase artifact persisted in the workspace."""

    phase: str
    kind: str
    summary: str
    references: list[str] = dataclasses.field(default_factory=list)
    metadata: dict[str, object] = dataclasses.field(default_factory=dict)
    created_at: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class UiUxProMaxContext:
    """Workflow cursor and bounded summaries; full reports remain workspace files."""

    intent: str
    run_id: str
    state: UiUxProMaxState = UiUxProMaxState.PRODUCT_DISCOVERY
    plan_version: str = "ui_ux_pro_max.v2"
    route_profile: str = "broad"
    phase_iteration: int = 0
    phase_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    completed_phases: list[str] = dataclasses.field(default_factory=list)
    phase_history: list[dict[str, object]] = dataclasses.field(default_factory=list)
    last_boundary: dict[str, object] = dataclasses.field(default_factory=dict)
    resume_resolution_source: str = ""
    resume_resolution_reason: str = ""
    resume_reconciled: bool = False

    project_root: str = ""
    project_name: str = ""
    project_type: str = ""
    platform: str = ""
    stack: str = ""
    request_kind: str = ""
    product_summary: str = ""
    target_users: list[str] = dataclasses.field(default_factory=list)
    user_journeys: list[str] = dataclasses.field(default_factory=list)
    required_surfaces: list[str] = dataclasses.field(default_factory=list)
    functional_requirements: list[str] = dataclasses.field(default_factory=list)
    ux_requirements: list[str] = dataclasses.field(default_factory=list)
    accessibility_expectations: list[str] = dataclasses.field(default_factory=list)
    technical_constraints: list[str] = dataclasses.field(default_factory=list)
    existing_visual_conventions: list[str] = dataclasses.field(default_factory=list)
    acceptance_criteria: list[str] = dataclasses.field(default_factory=list)
    assumptions: list[str] = dataclasses.field(default_factory=list)

    skill_available: bool | None = None
    skill_name: str = ""
    skill_path: str = ""
    skill_status: str = "not_checked"

    research_summary: str = ""
    research_queries: list[str] = dataclasses.field(default_factory=list)
    research_datasets: list[str] = dataclasses.field(default_factory=list)
    research_findings: list[str] = dataclasses.field(default_factory=list)
    research_provenance: list[str] = dataclasses.field(default_factory=list)
    applicable_recommendations: list[str] = dataclasses.field(default_factory=list)
    rejected_recommendations: list[str] = dataclasses.field(default_factory=list)
    research_conflicts: list[str] = dataclasses.field(default_factory=list)
    research_confidence: str = "unknown"
    research_artifact_refs: list[str] = dataclasses.field(default_factory=list)
    research_limitations: list[str] = dataclasses.field(default_factory=list)

    design_summary: str = ""
    design_artifact_refs: list[str] = dataclasses.field(default_factory=list)
    visual_direction: str = ""
    design_rationale: list[str] = dataclasses.field(default_factory=list)
    design_style: str = ""
    color_tokens: list[str] = dataclasses.field(default_factory=list)
    typography_tokens: list[str] = dataclasses.field(default_factory=list)
    spacing_rules: list[str] = dataclasses.field(default_factory=list)
    layout_rules: list[str] = dataclasses.field(default_factory=list)
    component_rules: list[str] = dataclasses.field(default_factory=list)
    interaction_states: list[str] = dataclasses.field(default_factory=list)
    responsive_behavior: list[str] = dataclasses.field(default_factory=list)
    accessibility_rules: list[str] = dataclasses.field(default_factory=list)
    motion_rules: list[str] = dataclasses.field(default_factory=list)
    anti_patterns: list[str] = dataclasses.field(default_factory=list)
    design_review_status: str = "pending"
    design_review_findings: list[str] = dataclasses.field(default_factory=list)
    design_review_attempts: int = 0

    implementation_guide_summary: str = ""
    detected_stack: str = ""
    stack_evidence: list[str] = dataclasses.field(default_factory=list)
    token_mappings: list[str] = dataclasses.field(default_factory=list)
    component_strategy: list[str] = dataclasses.field(default_factory=list)
    responsive_strategy: list[str] = dataclasses.field(default_factory=list)
    state_handling_conventions: list[str] = dataclasses.field(default_factory=list)
    accessibility_strategy: list[str] = dataclasses.field(default_factory=list)
    test_tools: list[str] = dataclasses.field(default_factory=list)
    performance_constraints: list[str] = dataclasses.field(default_factory=list)
    implementation_anti_patterns: list[str] = dataclasses.field(default_factory=list)
    validation_commands: list[str] = dataclasses.field(default_factory=list)
    stack_limitations: list[str] = dataclasses.field(default_factory=list)

    screen_inventory: list[str] = dataclasses.field(default_factory=list)
    interaction_state_matrix: list[str] = dataclasses.field(default_factory=list)
    component_dependency_graph: list[str] = dataclasses.field(default_factory=list)
    page_component_specs: list[str] = dataclasses.field(default_factory=list)
    acceptance_mapping: list[str] = dataclasses.field(default_factory=list)
    implementation_plan: list[str] = dataclasses.field(default_factory=list)
    implementation_approval_feedback: str = ""

    implementation_summary: str = ""
    changed_files: list[str] = dataclasses.field(default_factory=list)
    implemented_states: list[str] = dataclasses.field(default_factory=list)
    design_deviations: list[str] = dataclasses.field(default_factory=list)
    remaining_implementation_work: list[str] = dataclasses.field(default_factory=list)

    audit_summary: str = ""
    audit_findings: list[str] = dataclasses.field(default_factory=list)
    visual_inspection_status: str = "not_attempted"
    visual_evidence_refs: list[str] = dataclasses.field(default_factory=list)
    audit_command_results: list[str] = dataclasses.field(default_factory=list)
    audit_limitations: list[str] = dataclasses.field(default_factory=list)
    remediation_cycles: int = 0
    remediation_summary: str = ""
    resolved_findings: list[str] = dataclasses.field(default_factory=list)
    unresolved_findings: list[str] = dataclasses.field(default_factory=list)
    remediation_verification: list[str] = dataclasses.field(default_factory=list)

    criterion_results: list[str] = dataclasses.field(default_factory=list)
    passed_criteria: list[str] = dataclasses.field(default_factory=list)
    failed_criteria: list[str] = dataclasses.field(default_factory=list)
    unverified_criteria: list[str] = dataclasses.field(default_factory=list)
    not_applicable_criteria: list[str] = dataclasses.field(default_factory=list)
    acceptance_summary: str = ""
    delivery_summary: str = ""
    fail_reason: str = ""
    artifacts: dict[str, PhaseArtifact] = dataclasses.field(default_factory=dict)
    shared_memory: ShortTermMemory | None = None

    def add_artifact(
        self,
        phase: str,
        kind: str,
        summary: str,
        references: list[str] | None = None,
    ) -> None:
        """Record bounded metadata and references, never the report body."""
        self.artifacts[phase] = PhaseArtifact(
            phase=phase[:64],
            kind=kind[:64],
            summary=bounded_text(summary),
            references=bounded_list(references or []),
        )

    def to_checkpoint_payload(self) -> dict[str, object]:
        """Return a JSON-safe bounded checkpoint without conversation memory."""
        payload: dict[str, object] = {}
        for name, value in vars(self).items():
            if name == "shared_memory":
                continue
            if name == "state":
                payload[name] = self.state.name
            elif name == "artifacts":
                payload[name] = {
                    key[:64]: {
                        "phase": artifact.phase[:64],
                        "kind": artifact.kind[:64],
                        "summary": bounded_text(artifact.summary),
                        "references": bounded_list(artifact.references),
                        "metadata": _bounded_mapping(artifact.metadata),
                        "created_at": _finite_timestamp(artifact.created_at),
                    }
                    for key, artifact in list(self.artifacts.items())[-MAX_LIST_ITEMS:]
                    if isinstance(artifact, PhaseArtifact)
                }
            elif isinstance(value, str):
                payload[name] = bounded_text(value)
            elif isinstance(value, list):
                if name == "phase_history":
                    payload[name] = [
                        _bounded_mapping(item)
                        for item in value[-MAX_RESUME_HISTORY:]
                        if isinstance(item, dict)
                    ]
                else:
                    payload[name] = bounded_list(value)
            elif isinstance(value, dict):
                payload[name] = _bounded_mapping(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                payload[name] = value
        return payload

    @classmethod
    def from_checkpoint_payload(
        cls,
        payload: dict[str, object],
        memory: ShortTermMemory | None = None,
    ) -> UiUxProMaxContext:
        """Restore validated state and reattach the session's shared memory."""
        state_name = payload.get("state")
        if not isinstance(state_name, str) or state_name not in UiUxProMaxState.__members__:
            raise ValueError("ui_ux_pro_max checkpoint contains an unknown workflow state")
        route_profile = payload.get("route_profile", "broad")
        if not isinstance(route_profile, str) or route_profile not in PHASES_BY_PROFILE:
            raise ValueError("ui_ux_pro_max checkpoint contains an unknown route profile")
        context = cls(
            intent=bounded_text(payload.get("intent", ""), 20_000),
            run_id=bounded_text(payload.get("run_id", ""), 256),
            state=UiUxProMaxState[state_name],
            route_profile=route_profile,
            shared_memory=memory,
        )
        string_fields = (
            "plan_version",
            "project_root",
            "project_name",
            "project_type",
            "platform",
            "stack",
            "request_kind",
            "product_summary",
            "skill_name",
            "skill_path",
            "skill_status",
            "research_summary",
            "research_confidence",
            "design_summary",
            "visual_direction",
            "design_style",
            "design_review_status",
            "implementation_guide_summary",
            "detected_stack",
            "implementation_approval_feedback",
            "implementation_summary",
            "audit_summary",
            "visual_inspection_status",
            "remediation_summary",
            "acceptance_summary",
            "delivery_summary",
            "fail_reason",
            "resume_resolution_source",
            "resume_resolution_reason",
        )
        for name in string_fields:
            value = payload.get(name)
            if isinstance(value, str):
                setattr(context, name, bounded_text(value))
        list_fields = (
            "completed_phases",
            "target_users",
            "user_journeys",
            "required_surfaces",
            "functional_requirements",
            "ux_requirements",
            "accessibility_expectations",
            "technical_constraints",
            "existing_visual_conventions",
            "acceptance_criteria",
            "assumptions",
            "research_queries",
            "research_datasets",
            "research_findings",
            "research_provenance",
            "applicable_recommendations",
            "rejected_recommendations",
            "research_conflicts",
            "research_artifact_refs",
            "research_limitations",
            "design_artifact_refs",
            "design_rationale",
            "color_tokens",
            "typography_tokens",
            "spacing_rules",
            "layout_rules",
            "component_rules",
            "interaction_states",
            "responsive_behavior",
            "accessibility_rules",
            "motion_rules",
            "anti_patterns",
            "design_review_findings",
            "stack_evidence",
            "token_mappings",
            "component_strategy",
            "responsive_strategy",
            "state_handling_conventions",
            "accessibility_strategy",
            "test_tools",
            "performance_constraints",
            "implementation_anti_patterns",
            "validation_commands",
            "stack_limitations",
            "screen_inventory",
            "interaction_state_matrix",
            "component_dependency_graph",
            "page_component_specs",
            "acceptance_mapping",
            "implementation_plan",
            "changed_files",
            "implemented_states",
            "design_deviations",
            "remaining_implementation_work",
            "audit_findings",
            "visual_evidence_refs",
            "audit_command_results",
            "audit_limitations",
            "resolved_findings",
            "unresolved_findings",
            "remediation_verification",
            "criterion_results",
            "passed_criteria",
            "failed_criteria",
            "unverified_criteria",
            "not_applicable_criteria",
        )
        for name in list_fields:
            setattr(context, name, bounded_list(payload.get(name, [])))
        for name in ("phase_iteration", "design_review_attempts", "remediation_cycles"):
            value = payload.get(name, 0)
            count = value if isinstance(value, int) and not isinstance(value, bool) else 0
            setattr(context, name, min(max(count, 0), 100_000))
        skill_available = payload.get("skill_available")
        context.skill_available = skill_available if isinstance(skill_available, bool) else None
        resume_reconciled = payload.get("resume_reconciled")
        context.resume_reconciled = (
            resume_reconciled if isinstance(resume_reconciled, bool) else False
        )
        attempts = payload.get("phase_attempts")
        if isinstance(attempts, dict):
            context.phase_attempts = {
                key[:64]: min(value, 100_000)
                for key, value in attempts.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
        boundary = payload.get("last_boundary")
        if isinstance(boundary, dict):
            context.last_boundary = _bounded_mapping(boundary)
        history = payload.get("phase_history")
        if isinstance(history, list):
            context.phase_history = [
                _bounded_mapping(item)
                for item in history[-MAX_RESUME_HISTORY:]
                if isinstance(item, dict)
            ]
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, dict):
            for key, raw in list(artifacts.items())[-MAX_LIST_ITEMS:]:
                if not isinstance(key, str) or not isinstance(raw, dict):
                    continue
                context.artifacts[key[:64]] = PhaseArtifact(
                    phase=bounded_text(raw.get("phase", key), 64),
                    kind=bounded_text(raw.get("kind", "artifact"), 64),
                    summary=bounded_text(raw.get("summary", "")),
                    references=bounded_list(raw.get("references", [])),
                    metadata=_bounded_mapping(raw.get("metadata", {})),
                    created_at=_finite_timestamp(raw.get("created_at")),
                )
        return context


def bounded_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    """Normalize and bound text supplied by tools or checkpoint payloads."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    printable = "".join(char for char in text if char in "\n\t" or char.isprintable())
    return printable.strip()[:limit]


def bounded_list(value: object) -> list[str]:
    """Return only bounded string items from a tool or checkpoint list."""
    if not isinstance(value, list):
        return []
    return [
        bounded_text(item, MAX_ITEM_CHARS)
        for item in value[:MAX_LIST_ITEMS]
        if isinstance(item, str)
    ]


def _bounded_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key, item in list(value.items())[:MAX_LIST_ITEMS]:
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            result[key[:128]] = bounded_text(item, MAX_ITEM_CHARS)
        elif isinstance(item, list):
            result[key[:128]] = bounded_list(item)
        elif isinstance(item, float) and math.isfinite(item):
            result[key[:128]] = item
        elif isinstance(item, (bool, int)) or item is None:
            result[key[:128]] = item
    return result


def _finite_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return time.time()
