"""Integration coverage for PRD-179 durable phase boundaries and recovery."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.workflow_checkpoint_store import WorkflowCheckpointStore
from agenthicc.runners.workflow_handle import WorkflowRunHandle
from agenthicc.tools.sandbox import WorkspaceScope
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin
from agenthicc.workflows.reconstruct_site import (
    ReconstructContext,
    ReconstructSiteParams,
    ReconstructSiteRunner,
    ReconstructState,
)
from agenthicc.workflows.reconstruct_site.evidence_plan import RECONSTRUCT_PHASE_PLAN

pytestmark = pytest.mark.integration


@dataclasses.dataclass
class BoundaryContext:
    intent: str
    run_id: str
    state: str
    phase_iteration: int
    shared_memory: object | None = None


class BoundaryWorkflow(WorkflowPlugin):
    name = "boundary_integration"
    description = "phase boundary integration fixture"
    phases = [PhaseSpec(name="first", next="second"), PhaseSpec(name="second")]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        assert isinstance(context, BoundaryContext)
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state,
            "phase_iteration": context.phase_iteration,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls, payload: dict[str, object], memory: object | None = None
    ) -> BoundaryContext:
        return BoundaryContext(
            intent=str(payload["intent"]),
            run_id=str(payload["run_id"]),
            state=str(payload["state"]),
            phase_iteration=int(payload["phase_iteration"]),
            shared_memory=memory,
        )


def test_real_handle_persists_post_phase_boundary(tmp_path: Path) -> None:
    conversation = SessionConversation.open(
        "boundary-session",
        max_tokens=10_000,
        journal_path=tmp_path / "conversation.jsonl",
    )
    try:
        store = WorkflowCheckpointStore("boundary-session", root=tmp_path / "sessions")
        handle = WorkflowRunHandle.create(
            run_id="boundary-run",
            workflow=BoundaryWorkflow,
            conversation=conversation,
            intent="run boundary fixture",
            checkpoint_store=store,
        )
        context = BoundaryContext(
            intent="run boundary fixture",
            run_id="boundary-run",
            state="second",
            phase_iteration=1,
            shared_memory=conversation.memory,
        )
        handle.attach_context(context)
        from agenthicc.workflows.phase_lifecycle import checkpoint_phase_boundary

        checkpoint_phase_boundary(
            SimpleNamespace(workflow_handle=handle),
            context,
            completed_phase="first",
            next_phase="second",
            phase_index=1,
            phase_iteration=1,
        )
        saved = store.load("boundary-run")
        assert saved is not None
        assert saved.current_phase == "second"
        assert saved.reason == "phase_boundary:first:completed"
        assert saved.context["fields"]["state"] == "second"  # type: ignore[index]
        journal_boundaries = conversation.journal.fold_workflow_phase_boundaries(
            "boundary-run", "boundary_integration"
        )
        assert [item["completed_phase"] for item in journal_boundaries] == ["first"]
    finally:
        conversation.close()


def _reconstruct_config(tmp_path: Path) -> SimpleNamespace:
    execution = SimpleNamespace(
        effective_model=lambda: "test-model",
        effective_usable_budget=lambda: 10_000,
        provider="openai",
        model="test-model",
        profile="",
        base_url="",
    )
    return SimpleNamespace(
        app_state=SimpleNamespace(update_workflow_phase=lambda **_kwargs: None),
        agent_runner=SimpleNamespace(),
        cfg=SimpleNamespace(execution=execution),
        params=ReconstructSiteParams(profile="production"),
        session_memory=object(),
        workflow_handle=None,
        workspace_scope=WorkspaceScope.create(tmp_path),
        browser_manager=object(),
        browser_tools=(),
        plugin_tools=[],
        mcp_registry=None,
        memory_router=None,
        semantic_index=None,
        approval_svc=None,
        terminal_wait_policies={},
    )


def test_reconstruct_resume_reconciles_receipts_before_prompt_state(tmp_path: Path) -> None:
    plan = RECONSTRUCT_PHASE_PLAN.active("production")
    context = ReconstructContext(
        intent="reconstruct fixture",
        run_id="reconstruct-reconcile",
        state=ReconstructState.INIT,
        profile="production",
    )
    runner = ReconstructSiteRunner(_reconstruct_config(tmp_path), None)
    store = runner._ensure_evidence(context)
    # Complete every phase before bootstrap, including optional research gate
    # entries in the production plan.  The checkpoint cursor intentionally
    # remains INIT to model a crash between boundary and checkpoint refresh.
    before_bootstrap = plan.names[: plan.names.index("bootstrap")]
    for attempt, phase in enumerate(before_bootstrap, start=1):
        store.write_phase_receipt(
            phase,
            attempt,
            f"{phase} complete",
            transition="next",
        )

    resumed = ReconstructSiteRunner(_reconstruct_config(tmp_path), None)
    resumed._active_plan = plan
    resumed._rehydrate_evidence(context)
    resumed._reconcile_resume_cursor(context, plan)

    assert context.state is ReconstructState.BOOTSTRAP
    assert context.resume_reconciled is True
    assert context.resume_resolution_source == "phase_receipts"
    assert "INIT" not in context.resume_resolution_reason
