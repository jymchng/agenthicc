"""Behavior coverage for the parallel workflow runner boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agenthicc.workflows.parallel_code_plan.runner import (
    ParallelCodePlanContext,
    ParallelCodePlanRunner,
)


class _AppState:
    def __init__(self) -> None:
        self.phases: list[tuple[object, ...]] = []

    def update_workflow_phase(self, **kwargs: object) -> None:
        self.phases.append(tuple(kwargs.values()))


class _Handle:
    run_id = "parallel-run"
    checkpoint_supported = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def attach_context(self, _context: object) -> None:
        self.calls.append("attach")

    def update_phase(self, name: str, _index: int, _iteration: int) -> None:
        self.calls.append(f"phase:{name}")

    def persist_checkpoint(self, *, reason: str) -> None:
        self.calls.append(f"checkpoint:{reason}")

    def save_checkpoint(self, *, reason: str) -> None:
        self.calls.append(f"save:{reason}")

    def mark_terminal(self, status: str) -> None:
        self.calls.append(f"terminal:{status}")

    def finalize_failure(self, reason: str) -> None:
        self.calls.append(f"failure:{reason}")


class _Delegate:
    _model_id = "model"

    def _phase_model(self, phase: str) -> str:
        return f"{phase}-model"

    async def _run_turn(self, *_args: object, **_kwargs: object) -> None:
        return None


class _Coordinator:
    def create(self) -> SimpleNamespace:
        return SimpleNamespace(orchestration_id="orchestration-1")


def _runner(*, handle: _Handle | None = None) -> ParallelCodePlanRunner:
    runner = object.__new__(ParallelCodePlanRunner)
    runner._cfg = SimpleNamespace(
        approval_svc=None,
        app_state=_AppState(),
        workflow_handle=handle,
        conversation_id="conversation",
        session_memory=object(),
    )
    runner._delegate = _Delegate()
    return runner


@pytest.mark.asyncio
async def test_parallel_runner_executes_each_phase_and_persists_boundaries() -> None:
    handle = _Handle()
    runner = _runner(handle=handle)
    coordinator = _Coordinator()
    runner._coordinator = lambda _ctx: coordinator  # type: ignore[method-assign]

    async def turn(
        _ctx: object,
        *,
        phase: str,
        index: int,
        text: str,
        prompt: str,
        tools: object,
        event: object,
        max_turns: int = 12,
    ) -> None:
        assert phase
        assert index >= 0
        assert text
        assert prompt
        assert tools
        assert max_turns > 0
        event.set()  # type: ignore[union-attr]

    runner._turn = turn  # type: ignore[method-assign]
    context = await runner.run("Implement the feature")

    assert context.orchestration_id == "orchestration-1"
    assert context.phase == "complete"
    assert context.summary.startswith("Parallel orchestration")
    assert "terminal:complete" in handle.calls
    assert any(item.startswith("checkpoint:") for item in handle.calls)


@pytest.mark.asyncio
async def test_parallel_runner_resume_dispatches_from_saved_phase() -> None:
    runner = _runner()
    runner._coordinator = lambda _ctx: _Coordinator()  # type: ignore[method-assign]

    async def turn(*_args: object, event: object, **_kwargs: object) -> None:
        event.set()  # type: ignore[union-attr]

    runner._turn = turn  # type: ignore[method-assign]
    context = ParallelCodePlanContext(
        intent="resume",
        run_id="parallel-run",
        orchestration_id="orchestration-1",
        phase="dispatch",
    )
    resumed = await runner.resume(context)
    assert resumed.phase == "complete"
    assert await runner.resume(resumed) is resumed
    with pytest.raises(TypeError, match="ParallelCodePlanContext"):
        await runner.resume(object())


@pytest.mark.asyncio
async def test_parallel_runner_turn_requires_transition_and_handles_failure() -> None:
    runner = _runner()
    ctx = ParallelCodePlanContext(intent="intent", run_id="run")
    event = __import__("asyncio").Event()
    with pytest.raises(RuntimeError, match="ended without"):
        await runner._turn(
            ctx,
            phase="plan",
            index=0,
            text="text",
            prompt="prompt",
            tools=[],
            event=event,
        )

    failing = _runner(handle=_Handle())

    async def fail(_ctx: object, _index: int) -> None:
        raise RuntimeError("phase failed")

    failing._plan = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="phase failed"):
        await failing.run("intent")
    assert any(
        item.startswith("failure:RuntimeError") for item in failing._cfg.workflow_handle.calls
    )
