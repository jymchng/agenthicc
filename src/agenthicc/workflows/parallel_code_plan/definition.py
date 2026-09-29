"""Workflow plugin definition for parallel coding."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from .runner import ParallelCodePlanRunner


@dataclasses.dataclass
class ParallelCodePlanParams(WorkflowParams):
    """Optional per-phase model overrides for parallel planning."""

    plan_model: str = ""
    decompose_model: str = ""
    dispatch_model: str = ""
    collect_model: str = ""
    integrate_model: str = ""
    verify_model: str = ""
    summarize_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        return dataclasses.asdict(self)


class ParallelCodePlan(WorkflowPlugin):
    """Plan once, execute independent tasks in isolated worktrees, integrate safely."""

    name = "parallel_code_plan"
    description = "Plan → decompose → isolated workers → review → integrate → verify"
    mode_bindings = ["Plan"]
    phases = [
        PhaseSpec(name="plan", agent_type="planner", next="decompose", max_turns=20),
        PhaseSpec(name="decompose", agent_type="planner", next="dispatch", max_turns=20),
        PhaseSpec(name="dispatch", agent_type="coordinator", next="collect", max_turns=12),
        PhaseSpec(name="collect", agent_type="reviewer", next="integrate", max_turns=8),
        PhaseSpec(name="integrate", agent_type="integrator", next="verify", max_turns=20),
        PhaseSpec(name="verify", agent_type="verifier", next="summarize", max_turns=12),
        PhaseSpec(name="summarize", agent_type="reviewer", next=None, max_turns=2),
    ]

    @classmethod
    def build_runner(
        cls, config: WorkflowConfig, mode_manager: ModeManager | None
    ) -> ParallelCodePlanRunner:
        from .runner import ParallelCodePlanRunner  # noqa: PLC0415

        return ParallelCodePlanRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: Mapping[str, object]) -> WorkflowParams:
        fields = {
            field.name: value
            for field in dataclasses.fields(ParallelCodePlanParams)
            if isinstance(value := source.get(field.name), str)
        }
        return ParallelCodePlanParams(**fields)

    @classmethod
    def create_initial_context(
        cls, intent: str, run_id: str, memory: object | None = None
    ) -> object:
        from .runner import ParallelCodePlanContext  # noqa: PLC0415

        return ParallelCodePlanContext(
            intent=intent,
            run_id=run_id,
            shared_memory=cast("ShortTermMemory | None", memory),
        )

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object] | None:
        from .runner import ParallelCodePlanContext  # noqa: PLC0415

        if not isinstance(context, ParallelCodePlanContext):
            return None
        return {
            "intent": context.intent[:20_000],
            "run_id": context.run_id,
            "plan": context.plan[:20_000],
            "orchestration_id": context.orchestration_id,
            "phase": context.phase,
            "summary": context.summary[:4_000],
            "fail_reason": context.fail_reason[:2_000],
            "phase_iteration": context.phase_iteration,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> object | None:
        from .runner import ParallelCodePlanContext  # noqa: PLC0415

        raw_iteration = payload.get("phase_iteration", 0)
        iteration = raw_iteration if isinstance(raw_iteration, int) else 0
        return ParallelCodePlanContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            plan=str(payload.get("plan", "")),
            orchestration_id=str(payload.get("orchestration_id", "")),
            phase=str(payload.get("phase", "plan")),
            summary=str(payload.get("summary", "")),
            fail_reason=str(payload.get("fail_reason", "")),
            phase_iteration=iteration,
            shared_memory=cast("ShortTermMemory | None", memory),
        )
