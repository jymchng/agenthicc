"""Integration coverage for discovery and checkpoint-safe workflow metadata."""

from __future__ import annotations

from agenthicc.workflows.loader import builtin_workflow_descriptors
from agenthicc.workflows.registry import build_workflow_registry


def test_parallel_code_plan_is_discoverable_without_eager_import() -> None:
    descriptor = next(
        item for item in builtin_workflow_descriptors() if item.name == "parallel_code_plan"
    )
    assert descriptor.module.endswith("parallel_code_plan.definition")
    registry = build_workflow_registry()
    plugin = registry.get("parallel_code_plan")
    assert plugin is not None
    assert plugin.phase_names() == [
        "plan",
        "decompose",
        "dispatch",
        "collect",
        "integrate",
        "verify",
        "summarize",
    ]


def test_parallel_context_checkpoint_contains_identity_not_live_resources() -> None:
    from agenthicc.workflows.parallel_code_plan import ParallelCodePlan, ParallelCodePlanContext

    context = ParallelCodePlanContext(
        intent="Implement a feature",
        run_id="run-1",
        plan="split it",
        orchestration_id="orchestration-1",
        phase="integrate",
        shared_memory=object(),
    )
    payload = ParallelCodePlan.checkpoint_context_to_payload(context)
    assert payload is not None
    assert payload["orchestration_id"] == "orchestration-1"
    assert "shared_memory" not in payload
    restored = ParallelCodePlan.checkpoint_context_from_payload(payload, memory="session-memory")
    assert isinstance(restored, ParallelCodePlanContext)
    assert restored.phase == "integrate"
    assert restored.shared_memory == "session-memory"
