"""Built-in workflow definitions — Python-only, no TOML (PRD-87)."""
from __future__ import annotations

from agenthicc.workflows.plugin import PhaseRole, PhaseSpec, WorkflowPlugin


class PlanOnly(WorkflowPlugin):
    name          = "plan_only"
    description   = "Read-only planning pass — produces a plan, does not execute."
    mode_bindings = ["Review"]
    phases        = [
        PhaseSpec(
            name="plan",
            agent_type=PhaseRole.PLANNER,
            max_turns=8,
            output_schema="plan",
        ),
    ]


class CodePlan(WorkflowPlugin):
    """Single-agent Plan mode: Plan → Execute → Review → Summary.

    One agent runs all four phases sharing the same ShortTermMemory, so the
    executor already has full context from the planning phase without any
    re-exploration.

    The plan phase uses two injected approval tools:
      request_plan_approval(plan)  — shows PlanApprovalOverlay; returns
                                     {approved, feedback}.
      finalize_plan(plan)          — writes the approved plan and signals
                                     transition to execute.

    Phase focus is governed by system_prompt_override; no specialised agent
    class is needed.  All phases use agent_type="auto".
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
            mode_override="Auto",           # switches to Auto → write/exec tools available
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
            mode_override=None,         # stays in Plan mode → read-only review
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
            mode_override=None,     # stays in Plan mode
            system_prompt_override=(
                "You are in the SUMMARY phase. Write a concise summary of what "
                "was planned, implemented, and verified in this session."
            ),
        ),
    ]


class ReviewOnly(WorkflowPlugin):
    name          = "review_only"
    description   = "Read-only review pass — inspect and provide structured feedback."
    mode_bindings = []
    phases        = [
        PhaseSpec(
            name="review",
            agent_type=PhaseRole.REVIEWER,
            max_turns=8,
            output_schema="review_result",
        ),
    ]


class Supervised(WorkflowPlugin):
    name          = "supervised"
    description   = "Plan → Human Review → Execute."
    mode_bindings = []
    phases        = [
        PhaseSpec(
            name="plan",
            agent_type=PhaseRole.PLANNER,
            max_turns=8,
            output_schema="plan",
            next="human_review",
        ),
        PhaseSpec(
            name="human_review",
            agent_type=PhaseRole.HUMAN,
            max_turns=1,
            next="execute",
            on_reject="plan",
            max_iterations=5,
        ),
        PhaseSpec(
            name="execute",
            agent_type=PhaseRole.EXECUTOR,
            max_turns=30,
        ),
    ]


class Architect(WorkflowPlugin):
    name          = "architect"
    description   = "Explore → Plan → Execute → Verify."
    mode_bindings = []
    phases        = [
        PhaseSpec(
            name="explore",
            agent_type=PhaseRole.EXPLORER,
            max_turns=10,
            next="plan",
        ),
        PhaseSpec(
            name="plan",
            agent_type=PhaseRole.PLANNER,
            max_turns=8,
            output_schema="plan",
            next="execute",
        ),
        PhaseSpec(
            name="execute",
            agent_type=PhaseRole.EXECUTOR,
            max_turns=40,
            next="verify",
        ),
        PhaseSpec(
            name="verify",
            agent_type=PhaseRole.VERIFIER,
            max_turns=8,
            output_schema="review_result",
            on_reject="execute",
            max_iterations=2,
        ),
    ]
