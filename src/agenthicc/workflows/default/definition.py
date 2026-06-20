"""Generic built-in workflow definitions (PRD-87, PRD-112).

Simple declarative WorkflowPlugin subclasses that use the standard
WorkflowRunner — no custom state machine required.
"""
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
