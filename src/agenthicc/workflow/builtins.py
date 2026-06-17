"""Built-in workflow definitions — graph-based, Python-only (PRD-101)."""
from __future__ import annotations

from agenthicc.workflow.plugin import (
    EdgeGate, EdgeSpec, PhaseNode, PhaseRole,
    WorkflowGraph, WorkflowPlugin,
)


class PlanOnly(WorkflowPlugin):
    """Single read-only planning pass."""
    name          = "plan_only"
    description   = "Read-only planning pass — produces a plan, does not execute."
    mode_bindings = ["Review"]

    graph = WorkflowGraph(
        name  = "plan_only",
        entry = "plan",
        nodes = {
            "plan": PhaseNode(
                name       = "plan",
                agent_type = PhaseRole.PLANNER,
                edges      = (),   # terminal
            ),
        },
    )


class CodePlan(WorkflowPlugin):
    """Single-agent Plan mode: plan → execute → review → summarize."""
    name          = "code_plan"
    description   = "Plan → Execute → Review → Summary (single agent, shared memory)"
    mode_bindings = ["Plan"]

    graph = WorkflowGraph(
        name  = "code_plan",
        entry = "plan",
        nodes = {
            "plan": PhaseNode(
                name              = "plan",
                agent_type        = "auto",
                max_continuations = 5,
                edges             = (
                    EdgeSpec("execute", "approve",
                             gate=EdgeGate(kind="plan_review",
                                          title="Review Implementation Plan")),
                    EdgeSpec("plan", "revise"),
                ),
            ),
            "execute": PhaseNode(
                name              = "execute",
                agent_type        = "auto",
                mode_override     = "Auto",
                max_continuations = 10,
                edges             = (
                    EdgeSpec("review", "complete"),
                ),
            ),
            "review": PhaseNode(
                name              = "review",
                agent_type        = "auto",
                max_continuations = 3,
                edges             = (
                    EdgeSpec("summarize", "approve"),
                    EdgeSpec("execute",   "reject"),
                ),
            ),
            "summarize": PhaseNode(
                name              = "summarize",
                agent_type        = "auto",
                max_continuations = 1,
                edges             = (),   # terminal
            ),
        },
    )


class ReviewOnly(WorkflowPlugin):
    """Single read-only review pass."""
    name          = "review_only"
    description   = "Read-only review pass — inspect and provide structured feedback."
    mode_bindings: list[str] = []

    graph = WorkflowGraph(
        name  = "review_only",
        entry = "review",
        nodes = {
            "review": PhaseNode(
                name       = "review",
                agent_type = PhaseRole.REVIEWER,
                edges      = (),   # terminal
            ),
        },
    )


class Supervised(WorkflowPlugin):
    """Plan → human review → execute."""
    name          = "supervised"
    description   = "Plan → Human Review → Execute."
    mode_bindings: list[str] = []

    graph = WorkflowGraph(
        name  = "supervised",
        entry = "plan",
        nodes = {
            "plan": PhaseNode(
                name       = "plan",
                agent_type = PhaseRole.PLANNER,
                edges      = (
                    EdgeSpec("human_review", "complete"),
                ),
            ),
            "human_review": PhaseNode(
                name       = "human_review",
                agent_type = PhaseRole.HUMAN,
                edges      = (
                    EdgeSpec("execute", "approve"),
                    EdgeSpec("plan",    "reject"),
                ),
            ),
            "execute": PhaseNode(
                name       = "execute",
                agent_type = PhaseRole.EXECUTOR,
                edges      = (),   # terminal
            ),
        },
    )


class Architect(WorkflowPlugin):
    """Explore → plan → execute → verify."""
    name          = "architect"
    description   = "Explore → Plan → Execute → Verify."
    mode_bindings: list[str] = []

    graph = WorkflowGraph(
        name  = "architect",
        entry = "explore",
        nodes = {
            "explore": PhaseNode(
                name       = "explore",
                agent_type = PhaseRole.EXPLORER,
                edges      = (
                    EdgeSpec("plan", "complete"),
                ),
            ),
            "plan": PhaseNode(
                name       = "plan",
                agent_type = PhaseRole.PLANNER,
                edges      = (
                    EdgeSpec("execute", "complete"),
                ),
            ),
            "execute": PhaseNode(
                name       = "execute",
                agent_type = PhaseRole.EXECUTOR,
                edges      = (
                    EdgeSpec("verify", "complete"),
                ),
            ),
            "verify": PhaseNode(
                name              = "verify",
                agent_type        = PhaseRole.VERIFIER,
                max_continuations = 2,
                edges             = (
                    EdgeSpec(None,      "approve"),   # terminal on approve
                    EdgeSpec("execute", "reject"),
                ),
            ),
        },
    )
