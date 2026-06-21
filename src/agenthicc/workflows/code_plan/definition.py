"""CodePlan workflow plugin definition and tunable parameters (PRD-112).

Co-located with the runner and state machine that back it.
"""
from __future__ import annotations

import dataclasses
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.workflows.code_plan.runner import CodePlanRunner
    from agenthicc.workflows.config import WorkflowConfig
    from agenthicc.tui.runtime.mode_manager import ModeManager

from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin


@dataclasses.dataclass
class CodePlanParams(WorkflowParams):
    """Tunable parameters for the code_plan workflow (PRD-111).

    Each field maps to a TOML key under ``[workflows.code_plan]``.

    Example agenthicc.toml::

        [workflows.code_plan]
        execute_model = "claude-haiku-4-5"   # cheaper model for implementation
        plan_model    = ""                   # empty → use execution.model
    """
    plan_model:    str = field(default="")
    execute_model: str = field(default="")
    review_model:  str = field(default="")
    summary_model: str = field(default="")

    def get_phase_models(self) -> dict[str, str]:
        return {
            "plan":      self.plan_model,
            "execute":   self.execute_model,
            "review":    self.review_model,
            "summarize": self.summary_model,
        }


class CodePlan(WorkflowPlugin):
    """Single-agent Plan mode: Plan → Execute → Review → Summary.

    One agent runs all four phases sharing the same ShortTermMemory, so the
    executor already has full context from the planning phase without any
    re-exploration.
    """
    name          = "code_plan"
    description   = "Plan → Execute → Review → Summary  (single agent, shared memory)"
    mode_bindings = ["Plan"]
    phases        = [
        PhaseSpec(
            name="plan",
            agent_type="auto",
            max_turns=20,
            next="execute",
            on_reject="plan",
            max_iterations=10,
            require_plan_finalization=True,
            mode_override=None,
            system_prompt_override=(
                "You are in the PLANNING phase. First explore the repository to "
                "understand the codebase. Then produce a detailed implementation "
                "plan. Use request_plan_approval() to present the plan for human "
                "review, and finalize_plan() once it is approved."
            ),
        ),
        PhaseSpec(
            name="execute",
            agent_type="auto",
            max_turns=40,
            next="review",
            max_iterations=10,
            require_explicit_completion=True,
            mode_override="Auto",
            system_prompt_override=(
                "You are in the EXECUTION phase. You already explored and planned "
                "in the previous phase — do NOT re-explore. Implement the approved "
                "plan step by step using tools. "
                "When ALL tasks are complete, call mark_execute_complete() with a "
                "brief summary. Do not stop without calling it."
            ),
        ),
        PhaseSpec(
            name="review",
            agent_type="auto",
            max_turns=8,
            on_reject="execute",
            max_iterations=10,
            next="summarize",
            require_explicit_review=True,
            mode_override=None,
            system_prompt_override=(
                "You are in the REVIEW phase. Inspect the changes you just made "
                "and run the tests. "
                "Call approve_review(summary) if all tests pass and the code is correct. "
                "Call reject_review(reason) if there are issues that need fixing. "
                "You MUST call one of these two tools — do not output any other signal."
            ),
        ),
        PhaseSpec(
            name="summarize",
            agent_type="auto",
            max_turns=4,
            output_schema="free_text",
            mode_override=None,
            system_prompt_override=(
                "You are in the SUMMARY phase. Write a concise summary of what "
                "was planned, implemented, and verified in this session."
            ),
        ),
    ]

    @classmethod
    def build_runner(
        cls,
        config:       WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> CodePlanRunner:
        """Return a CodePlanRunner — uses its own state machine (PRD-116)."""
        from agenthicc.workflows.code_plan.runner import CodePlanRunner  # noqa: PLC0415
        return CodePlanRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build ``CodePlanParams`` from *source* (PRD-111, PRD-116)."""
        known = {f.name for f in dataclasses.fields(CodePlanParams)}
        return CodePlanParams(**{k: v for k, v in source.items() if k in known})
