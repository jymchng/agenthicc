"""Backward-compatibility shim — WorkflowRunner now lives in workflows/default/runner.py.

Import from ``agenthicc.workflows.default`` or ``agenthicc.workflows`` instead.
This module is retained so external code that imports from the old path continues
to work (PRD-112).
"""
from __future__ import annotations

from agenthicc.workflows.default.runner import WorkflowRunner, build_workflow_runner

__all__ = ["WorkflowRunner", "build_workflow_runner"]
