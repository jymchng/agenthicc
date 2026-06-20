"""Backward-compatibility shim — builtin workflow classes now live in subpackages.

- Generic workflows  → agenthicc.workflows.default.definition
- code_plan plugin   → agenthicc.workflows.code_plan.definition

Import from those canonical locations instead.  This module is retained so
external code that imports from the old path continues to work (PRD-112).
"""
from __future__ import annotations

from agenthicc.workflows.code_plan.definition import CodePlan, CodePlanParams
from agenthicc.workflows.default.definition import Architect, PlanOnly, ReviewOnly, Supervised

__all__ = [
    "CodePlan", "CodePlanParams",
    "Architect", "PlanOnly", "ReviewOnly", "Supervised",
]
