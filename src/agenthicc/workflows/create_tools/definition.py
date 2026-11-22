"""The built-in workflow that stages a validated set of project tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from agenthicc.workflows.authoring.budget import RepairBudget
from agenthicc.workflows.authoring.verdict import ValidationReport
from agenthicc.workflows.create_tools.plan import decode_plan
from agenthicc.workflows.create_tools.runner import (
    CreateToolsContext,
    CreateToolsRunner,
    CreateToolsState,
)
from agenthicc.workflows.payload import payload_int
from agenthicc.workflows.plugin import PhaseSpec, WorkflowInvocation, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lauren_ai._memory import ShortTermMemory

    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig


@dataclass
class CreateToolsParams(WorkflowParams):
    """Provider model overrides for the phases that use an agent."""

    analyze_model: str = ""
    generate_model: str = ""
    validate_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        return {
            "analyze": self.analyze_model,
            "generate": self.generate_model,
            "validate": self.validate_model,
        }


class CreateTools(WorkflowPlugin):
    """Stage one or several tools for the current session to publish."""

    name = "create_tools"
    description = "Plan, generate, validate, and stage project tools for publication."
    invocation = WorkflowInvocation(
        when_to_use="Create one or several agenthicc tools the session can call after publication.",
        returns="Names and publication states of the created tools.",
        agent_callable=True,
        subagent_eligible=True,
        publishes=("tool",),
    )
    phases = [
        PhaseSpec("analyze", next="generate"),
        PhaseSpec("generate", next="validate"),
        PhaseSpec("validate", next="finalize", on_reject="generate"),
        PhaseSpec("finalize"),
    ]

    @classmethod
    def build_runner(
        cls, config: WorkflowConfig, mode_manager: ModeManager | None
    ) -> CreateToolsRunner:
        return CreateToolsRunner(config)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> CreateToolsParams:
        return CreateToolsParams(
            analyze_model=str(source.get("analyze_model") or ""),
            generate_model=str(source.get("generate_model") or ""),
            validate_model=str(source.get("validate_model") or ""),
        )

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        if not isinstance(context, CreateToolsContext):
            raise TypeError("create_tools requires a CreateToolsContext.")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.value,
            "repair": asdict(context.repair),
            "phase_iteration": context.phase_iteration,
            "plans": [plan.to_dict() for plan in context.plans],
            "predecessors": dict(context.predecessors),
            "reports": {module: report.to_dict() for module, report in context.reports.items()},
            "static_only": context.static_only,
            "fail_reason": context.fail_reason,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls, payload: dict[str, object], memory: ShortTermMemory | None = None
    ) -> CreateToolsContext:
        raw_plans = payload.get("plans")
        raw_prior = payload.get("predecessors")
        raw_reports = payload.get("reports")
        if (
            not isinstance(raw_plans, list)
            or not isinstance(raw_prior, dict)
            or not isinstance(raw_reports, dict)
        ):
            raise ValueError("Invalid create_tools checkpoint.")
        plans = tuple(decode_plan(item) for item in raw_plans if isinstance(item, dict))
        if len(plans) != len(raw_plans):
            raise ValueError("Invalid create_tools plan in checkpoint.")
        return CreateToolsContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            repair=RepairBudget.from_payload(payload.get("repair")),
            state=CreateToolsState(str(payload.get("state"))),
            phase_iteration=payload_int(payload.get("phase_iteration"), 0),
            plans=plans,
            predecessors={str(name): str(digest) for name, digest in raw_prior.items()},
            reports={
                str(module): ValidationReport.from_payload(raw)
                for module, raw in raw_reports.items()
            },
            static_only=payload.get("static_only") is True,
            fail_reason=str(payload.get("fail_reason", "")),
            shared_memory=memory,
        )
