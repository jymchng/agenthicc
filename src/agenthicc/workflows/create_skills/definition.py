"""Registry declaration and checkpoint codec for the skill authoring workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from agenthicc.workflows.authoring.budget import RepairBudget
from agenthicc.workflows.authoring.verdict import ValidationReport
from agenthicc.workflows.create_skills.plan import decode_skill_plan
from agenthicc.workflows.create_skills.runner import (
    CreateSkillsContext,
    CreateSkillsRunner,
    CreateSkillsState,
)
from agenthicc.workflows.payload import payload_int
from agenthicc.workflows.plugin import PhaseSpec, WorkflowInvocation, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lauren_ai._memory import ShortTermMemory

    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig


@dataclass
class CreateSkillsParams(WorkflowParams):
    """Per-phase provider model overrides for skill authoring."""

    design_model: str = ""
    write_model: str = ""
    validate_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        return {
            "design": self.design_model,
            "write": self.write_model,
            "validate": self.validate_model,
        }


class CreateSkills(WorkflowPlugin):
    """Design and stage one or more skills for session publication."""

    name = "create_skills"
    description = "Author project skills with deterministic validation and live publication."
    invocation = WorkflowInvocation(
        when_to_use="Create reusable guidance the agent can load on demand in this project.",
        returns="Skill names, slugs, commands, and publication states.",
        agent_callable=True,
        subagent_eligible=True,
        publishes=("skill",),
    )
    phases = [
        PhaseSpec("design", next="write"),
        PhaseSpec("write", next="validate"),
        PhaseSpec("validate", next="finalize", on_reject="write"),
        PhaseSpec("finalize"),
    ]

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CreateSkillsRunner:
        return CreateSkillsRunner(config)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> CreateSkillsParams:
        return CreateSkillsParams(
            design_model=str(source.get("design_model") or ""),
            write_model=str(source.get("write_model") or ""),
            validate_model=str(source.get("validate_model") or ""),
        )

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        if not isinstance(context, CreateSkillsContext):
            raise TypeError("create_skills requires a CreateSkillsContext.")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.value,
            "repair": asdict(context.repair),
            "phase_iteration": context.phase_iteration,
            "plans": [plan.to_dict() for plan in context.plans],
            "predecessors": dict(context.predecessors),
            "reports": {slug: report.to_dict() for slug, report in context.reports.items()},
            "files": {slug: list(files) for slug, files in context.files.items()},
            "fail_reason": context.fail_reason,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: ShortTermMemory | None = None,
    ) -> CreateSkillsContext:
        raw_plans, raw_prior = payload.get("plans"), payload.get("predecessors")
        raw_reports, raw_files = payload.get("reports"), payload.get("files")
        if (
            not isinstance(raw_plans, list)
            or not isinstance(raw_prior, dict)
            or not isinstance(raw_reports, dict)
            or not isinstance(raw_files, dict)
        ):
            raise ValueError("Invalid create_skills checkpoint.")
        plans = tuple(decode_skill_plan(item) for item in raw_plans if isinstance(item, dict))
        if len(plans) != len(raw_plans):
            raise ValueError("Invalid create_skills plan in checkpoint.")
        files: dict[str, tuple[str, ...]] = {}
        for slug, paths in raw_files.items():
            if (
                not isinstance(slug, str)
                or not isinstance(paths, list)
                or any(not isinstance(path, str) for path in paths)
            ):
                raise ValueError("Invalid create_skills inventory in checkpoint.")
            files[slug] = tuple(paths)
        return CreateSkillsContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            repair=RepairBudget.from_payload(payload.get("repair")),
            state=CreateSkillsState(str(payload.get("state"))),
            phase_iteration=payload_int(payload.get("phase_iteration"), 0),
            plans=plans,
            predecessors={str(name): str(digest) for name, digest in raw_prior.items()},
            reports={
                str(slug): ValidationReport.from_payload(raw) for slug, raw in raw_reports.items()
            },
            files=files,
            fail_reason=str(payload.get("fail_reason", "")),
            shared_memory=memory,
        )
