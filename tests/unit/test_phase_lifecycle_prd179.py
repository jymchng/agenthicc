"""Unit coverage for PRD-179's shared phase lifecycle primitives."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.memory.journal import ConversationJournal
from agenthicc.workflows.phase_lifecycle import (
    PhaseAnnotation,
    PhaseBoundaryError,
    checkpoint_phase_boundary,
    publish_phase_annotation,
    reconcile_phase_cursor,
)
from agenthicc.workflows.create_workflow.runner import CreateWorkflowRunner
from agenthicc.workflows.create_workflow.state import CreateWorkflowContext, CreateWorkflowState

pytestmark = pytest.mark.unit


class _Handle:
    def __init__(self, *, fail: bool = False, checkpoint_supported: bool = True) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail = fail
        self.checkpoint_supported = checkpoint_supported

    def attach_context(self, context: object) -> None:
        self.calls.append(("attach", context))

    def update_phase(
        self,
        phase: str | None,
        index: int,
        iteration: int,
        *,
        persist: bool = True,
    ) -> None:
        self.calls.append(("update", (phase, index, iteration, persist)))

    def save_checkpoint(self, *, reason: str = "") -> object:
        self.calls.append(("checkpoint", reason))
        if self.fail:
            raise OSError("disk unavailable")
        return {"reason": reason}


def _annotation() -> PhaseAnnotation:
    return PhaseAnnotation(
        workflow_name="demo",
        phase_name="build",
        phase_index=1,
        total_phases=3,
        run_id="run-1",
        intent="build a demo",
        model_id="test-model",
        phase_iteration=2,
        phase_attempt=1,
        plan_version="demo.v1",
    )


def test_annotation_rejects_invalid_plan_position_and_identity() -> None:
    with pytest.raises(ValueError, match="phase_index"):
        PhaseAnnotation(
            workflow_name="demo",
            phase_name="build",
            phase_index=3,
            total_phases=3,
            run_id="run-1",
            intent="",
            model_id="model",
            phase_iteration=0,
        )

    with pytest.raises(ValueError, match="run_id"):
        PhaseAnnotation(
            workflow_name="demo",
            phase_name="build",
            phase_index=0,
            total_phases=1,
            run_id="",
            intent="",
            model_id="model",
            phase_iteration=0,
        )


def test_publish_annotation_updates_handle_before_ui_projection() -> None:
    handle = _Handle()
    ui_calls: list[dict[str, object]] = []
    config = SimpleNamespace(
        workflow_handle=handle,
        app_state=SimpleNamespace(update_workflow_phase=lambda **kwargs: ui_calls.append(kwargs)),
    )
    context = object()

    publish_phase_annotation(config, _annotation(), context, display_name="build 2/3")

    assert [name for name, _value in handle.calls] == ["attach", "update"]
    assert handle.calls[1][1] == ("build", 1, 2, True)
    assert ui_calls == [
        {
            "workflow_name": "demo",
            "phase_name": "build 2/3",
            "phase_index": 1,
            "total_phases": 3,
            "run_id": "run-1",
            "intent": "build a demo",
            "model_id": "test-model",
        }
    ]


def test_boundary_checkpoint_is_explicit_and_fail_closed() -> None:
    handle = _Handle()
    config = SimpleNamespace(workflow_handle=handle)
    result = checkpoint_phase_boundary(
        config,
        object(),
        completed_phase="build",
        next_phase="review",
        phase_index=2,
        phase_iteration=3,
        outcome="completed",
    )

    assert result == {"reason": "phase_boundary:build:completed"}
    assert handle.calls[-2][1] == ("review", 2, 3, False)
    assert handle.calls[-1][1] == "phase_boundary:build:completed"

    failing = _Handle(fail=True)
    with pytest.raises(PhaseBoundaryError, match="build"):
        checkpoint_phase_boundary(
            SimpleNamespace(workflow_handle=failing),
            object(),
            completed_phase="build",
            next_phase=None,
            phase_index=1,
            phase_iteration=3,
        )

    unsupported = _Handle(checkpoint_supported=False)
    with pytest.raises(PhaseBoundaryError, match="unavailable"):
        checkpoint_phase_boundary(
            SimpleNamespace(workflow_handle=unsupported),
            object(),
            completed_phase="build",
            next_phase="review",
            phase_index=2,
            phase_iteration=3,
        )


def test_boundary_checkpoint_is_idempotent_for_the_same_context_boundary() -> None:
    handle = _Handle()
    context = SimpleNamespace(last_boundary={})
    config = SimpleNamespace(workflow_handle=handle)

    checkpoint_phase_boundary(
        config,
        context,
        completed_phase="build",
        next_phase="review",
        phase_index=2,
        phase_iteration=3,
    )
    checkpoint_phase_boundary(
        config,
        context,
        completed_phase="build",
        next_phase="review",
        phase_index=2,
        phase_iteration=3,
    )

    assert [name for name, _value in handle.calls].count("checkpoint") == 1
    assert context.last_boundary["durable"] is True


def test_create_workflow_resume_reconciles_completed_prefix_before_prompt() -> None:
    runner = CreateWorkflowRunner.__new__(CreateWorkflowRunner)
    runner._cfg = SimpleNamespace(workflow_handle=None)
    context = CreateWorkflowContext(
        intent="author a workflow",
        run_id="run-1",
        state=CreateWorkflowState.DESIGN,
        completed_phases=["design", "generate"],
    )

    runner._reconcile_resume_cursor(context)

    assert context.state is CreateWorkflowState.VALIDATE
    assert context.resume_resolution_source == "durable_phase_state"
    assert context.resume_reconciled is True


def test_create_workflow_resume_preserves_validation_repair_cursor() -> None:
    runner = CreateWorkflowRunner.__new__(CreateWorkflowRunner)
    runner._cfg = SimpleNamespace(workflow_handle=None)
    context = CreateWorkflowContext(
        intent="author a workflow",
        run_id="run-1",
        state=CreateWorkflowState.GENERATE,
        completed_phases=["design", "generate", "validate"],
        rejection_reason="missing transition tool",
        last_boundary={
            "next_phase": "generate",
            "outcome": "rejected",
        },
    )

    runner._reconcile_resume_cursor(context)

    assert context.state is CreateWorkflowState.GENERATE


def test_create_workflow_resume_uses_same_session_journal_as_durable_evidence() -> None:
    runner = CreateWorkflowRunner.__new__(CreateWorkflowRunner)
    runner._cfg = SimpleNamespace(
        workflow_handle=SimpleNamespace(
            attach_context=lambda _context: None,
            update_phase=lambda *_args, **_kwargs: None,
            save_checkpoint=lambda **_kwargs: None,
            conversation=SimpleNamespace(
                journal=SimpleNamespace(
                    fold_workflow_phase_boundaries=lambda _run_id, _workflow: [
                        {"completed_phase": "design"},
                        {"completed_phase": "generate"},
                    ]
                )
            ),
        )
    )
    context = CreateWorkflowContext(
        intent="author a workflow",
        run_id="run-1",
        state=CreateWorkflowState.DESIGN,
    )

    runner._reconcile_resume_cursor(context)

    assert context.state is CreateWorkflowState.VALIDATE
    assert context.resume_resolution_source == "workflow_journal"
    assert context.resume_reconciled is True


def test_resume_resolution_can_use_same_session_journal_as_auxiliary_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversation.jsonl"
    journal = ConversationJournal(path)
    try:
        journal.workflow_phase_boundary(
            "run-1",
            "demo",
            completed_phase="first",
            next_phase="second",
            phase_index=1,
            phase_iteration=1,
            outcome="completed",
        )
        records = journal.fold_workflow_phase_boundaries("run-1", "demo")
    finally:
        journal.close()

    resolution = reconcile_phase_cursor(
        ("first", "second"),
        "first",
        journal_phases=tuple(str(item["completed_phase"]) for item in records),
    )
    assert resolution.source == "workflow_journal"
    assert resolution.phase_name == "second"


def test_reconcile_advances_stale_cursor_only_through_contiguous_receipts() -> None:
    resolution = reconcile_phase_cursor(
        ("init", "recon", "design_system", "bootstrap", "build"),
        "init",
        receipt_phases=("init", "recon", "design_system"),
        terminal_phase="complete",
    )

    assert resolution.phase_name == "bootstrap"
    assert resolution.phase_index == 3
    assert resolution.completed_phases == ("init", "recon", "design_system")
    assert resolution.source == "phase_receipts"
    assert resolution.reconciled is True

    gap = reconcile_phase_cursor(
        ("init", "recon", "design_system", "bootstrap"),
        "init",
        receipt_phases=("init", "design_system"),
    )
    assert gap.phase_name == "recon"
    assert gap.completed_phases == ("init",)


def test_reconcile_is_summary_independent_and_preserves_reentry_cursor() -> None:
    first = reconcile_phase_cursor(
        ("init", "build", "review"),
        "build",
        receipt_phases=("init",),
    )
    second = reconcile_phase_cursor(
        ("init", "build", "review"),
        "build",
        receipt_phases=("init",),
    )
    assert first == second
    assert first.phase_name == "build"

    reentry = reconcile_phase_cursor(
        ("init", "build", "review"),
        "build",
        receipt_phases=("init", "build", "review"),
        preserve_current=True,
    )
    assert reentry.phase_name == "build"
    assert reentry.source == "reentry_cursor"
