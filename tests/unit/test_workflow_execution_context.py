"""Workflow provenance is scoped to one workflow runner invocation."""

from __future__ import annotations

import pytest

from agenthicc.runners.workflow_execution_context import (
    bind_workflow_execution,
    consume_workflow_resume_plan,
    current_workflow_run_id,
)

pytestmark = pytest.mark.unit


def test_workflow_context_binds_run_id_and_consumes_resume_plan_once() -> None:
    plan = object()
    assert current_workflow_run_id() is None
    with bind_workflow_execution("run-1", resume_plan=plan):  # type: ignore[arg-type]
        assert current_workflow_run_id() == "run-1"
        assert consume_workflow_resume_plan() is plan
        assert consume_workflow_resume_plan() is None
    assert current_workflow_run_id() is None


def test_nested_workflow_context_restores_outer_identity() -> None:
    with bind_workflow_execution("outer"):
        assert current_workflow_run_id() == "outer"
        with bind_workflow_execution("inner"):
            assert current_workflow_run_id() == "inner"
        assert current_workflow_run_id() == "outer"


def test_empty_workflow_run_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        with bind_workflow_execution(" "):
            pass
