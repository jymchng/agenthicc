"""Workflow system — phase-based agentic pipelines (PRD-81, PRD-87, PRD-101)."""
from __future__ import annotations

# ── Legacy types (PRD-87) ─────────────────────────────────────────────────────
from agenthicc.workflow.plugin import (
    PhaseOutput,
    PhaseRole,
    PhaseRunRecord,
    PhaseSpec,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowPlugin,
    WorkflowRun,
    _parse_output_schema,
)

# ── Graph types (PRD-101) ─────────────────────────────────────────────────────
from agenthicc.workflow.plugin import (
    DataBus,
    EdgeGate,
    EdgeSpec,
    NodeResult,
    PhaseNode,
    WorkflowGraph,
)

from agenthicc.workflow.registry import WorkflowRegistry, build_workflow_registry
from agenthicc.workflow.loader import load_builtin_workflows, load_python_workflows
from agenthicc.workflow.runner import WorkflowRunner, build_workflow_runner
from agenthicc.workflow.config import WorkflowConfig

__all__ = [
    # Legacy
    "PhaseRole", "PhaseSpec", "WorkflowContext", "WorkflowDefinition",
    "WorkflowPlugin", "WorkflowRun", "PhaseOutput", "PhaseRunRecord",
    "_parse_output_schema",
    # Graph (PRD-101)
    "EdgeGate", "EdgeSpec", "PhaseNode", "WorkflowGraph",
    "DataBus", "NodeResult",
    # Infrastructure
    "WorkflowRegistry", "build_workflow_registry",
    "load_builtin_workflows", "load_python_workflows",
    "WorkflowRunner", "build_workflow_runner",
    "WorkflowConfig",
]
