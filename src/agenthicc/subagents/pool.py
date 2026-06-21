"""SubagentPool — concurrent worker execution (PRD-124)."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agenthicc.subagents.types import SubagentTypeSpec, SubagentTypeRegistry, DEFAULT_REGISTRY

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase

__all__ = [
    "WorkerState",
    "SubagentPoolState",
    "SubagentTask",
    "SubagentResult",
    "AggregatedResult",
    "SubagentWorker",
    "SubagentPool",
    "run_pool",
]

_MAX_RESULT_CHARS = 2_000


# ── TUI state models ──────────────────────────────────────────────────────────

@dataclass
class WorkerState:
    """Live status of one worker — used to drive the footer worker grid."""

    label:      str                                          # "explorer #1"
    agent_type: str
    status:     str = "pending"                              # pending | running | done | failed


@dataclass
class SubagentPoolState:
    """Live summary of an active SubagentPool — stored on ConversationStore signal."""

    pool_id:  str
    total:    int
    workers:  list[WorkerState] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for w in self.workers if w.status in ("done", "failed"))


# ── task / result data models ─────────────────────────────────────────────────

@dataclass
class SubagentTask:
    """One unit of work assigned to a subagent worker."""

    task_id:          str
    agent_type:       str
    task_description: str
    context:          str = ""


@dataclass
class SubagentResult:
    """Outcome of one subagent worker execution."""

    task_id:     str
    agent_type:  str
    label:       str    # "explorer #1", "tester #2", …
    ok:          bool
    text:        str    # AgentResponse.content verbatim (plain text)
    error:       str    = ""
    duration_ms: float  = 0.0


@dataclass
class AggregatedResult:
    """Concatenated result from all workers in one pool run."""

    pool_id:   str
    total:     int
    succeeded: int
    failed:    int
    text:      str    # labelled concatenation delivered to parent as tool result


# ── worker ────────────────────────────────────────────────────────────────────

class SubagentWorker:
    """Executes one SubagentTask using an isolated AgentRunnerBase instance."""

    def __init__(
        self,
        task:          SubagentTask,
        spec:          SubagentTypeSpec,
        index:         int,
        parent_runner: AgentRunnerBase,
        parent_model:  str,
        all_tools:     list[Any],
        app_state:     Any | None = None,
    ) -> None:
        self._task          = task
        self._spec          = spec
        self._index         = index
        self._parent_runner = parent_runner
        self._parent_model  = parent_model
        self._all_tools     = all_tools
        self._app_state     = app_state
        self.label          = f"{spec.name} #{index}"

    async def run(self) -> SubagentResult:
        """Execute the task; return SubagentResult regardless of success/failure."""
        t0 = time.monotonic()
        try:
            text = await asyncio.wait_for(
                self._execute(),
                timeout=self._spec.max_turn_time_s,
            )
            duration_ms = (time.monotonic() - t0) * 1_000
            return SubagentResult(
                task_id=self._task.task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=True,
                text=text,
                duration_ms=duration_ms,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - t0) * 1_000
            return SubagentResult(
                task_id=self._task.task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=False,
                text="",
                error=f"timed out after {self._spec.max_turn_time_s:.0f}s",
                duration_ms=duration_ms,
            )
        except asyncio.CancelledError:
            duration_ms = (time.monotonic() - t0) * 1_000
            return SubagentResult(
                task_id=self._task.task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=False,
                text="",
                error="cancelled",
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001
            duration_ms = (time.monotonic() - t0) * 1_000
            return SubagentResult(
                task_id=self._task.task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=False,
                text="",
                error=str(exc),
                duration_ms=duration_ms,
            )

    async def _execute(self) -> str:
        """Build an isolated agent and run the task. Returns response content."""
        from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
        from lauren_ai._agents._runner import AgentRunnerBase as _RunnerBase  # noqa: PLC0415
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from agenthicc.runners.tool_populator import populate_agent_tools  # noqa: PLC0415

        # Filter the full tool list to only what this type is allowed to use.
        filtered = [
            t for t in self._all_tools
            if getattr(t, "__name__", "") in self._spec.allowed_tools
        ]

        # Build the system prompt: type prompt + optional context.
        system = self._spec.system_prompt
        if self._task.context:
            system = f"{system}\n\n[ADDITIONAL CONTEXT]\n{self._task.context}"

        # Construct the @agent class and runner.
        @agent_decorator(model=self._parent_model, system=system)
        @use_tools(*filtered)
        class _SubAgent: ...  # noqa: N801

        agent_instance = _SubAgent()
        populate_agent_tools(agent_instance, filtered)

        hooks: list[Any] = []
        if self._app_state is not None:
            from agenthicc.tools.capability_gate import ToolCapabilityGate  # noqa: PLC0415
            hooks.append(ToolCapabilityGate(self._app_state))

        runner = _RunnerBase(
            transport=self._parent_runner._transport,
            global_hooks=hooks or None,
        )

        from lauren_ai._config import AgentConfig  # noqa: PLC0415

        memory = ShortTermMemory(max_tokens=8_000)
        response = await runner.run(
            agent_instance,
            self._task.task_description,
            memory=memory,
            config_override=AgentConfig(
                max_turns=self._spec.max_turns,
                parallel_tool_calls=True,
            ),
        )
        return response.content or ""


# ── pool ──────────────────────────────────────────────────────────────────────

class SubagentPool:
    """Runs a set of SubagentTasks concurrently bounded by *max_concurrent*.

    Creates one SubagentWorker per task, schedules them under an asyncio
    Semaphore, and aggregates results into a labelled plain-text digest.
    """

    def __init__(
        self,
        tasks:          list[SubagentTask],
        parent_runner:  AgentRunnerBase,
        parent_model:   str,
        all_tools:      list[Any],
        max_concurrent: int = 4,
        app_state:      Any | None = None,
        processor:      Any | None = None,
        conv_store:     Any | None = None,
        registry:       SubagentTypeRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self.pool_id        = uuid.uuid4().hex
        self._tasks         = tasks
        self._parent_runner = parent_runner
        self._parent_model  = parent_model
        self._all_tools     = all_tools
        self._max_concurrent = max_concurrent
        self._app_state     = app_state
        self._processor     = processor
        self._conv_store    = conv_store
        self._registry      = registry

    async def run(self) -> AggregatedResult:
        """Execute all tasks concurrently; return aggregated plain-text result."""
        # Type-index counter for labelling: {type_name → count_so_far}
        type_indices: dict[str, int] = {}

        workers: list[SubagentWorker] = []
        worker_states: list[WorkerState] = []
        for task in self._tasks:
            spec = self._registry.get(task.agent_type)
            if spec is None:
                workers.append(_UnknownTypeWorker(task))  # type: ignore[arg-type]
                worker_states.append(WorkerState(f"{task.agent_type} #?", task.agent_type, "pending"))
                continue
            type_indices[task.agent_type] = type_indices.get(task.agent_type, 0) + 1
            idx = type_indices[task.agent_type]
            w = SubagentWorker(
                task=task,
                spec=spec,
                index=idx,
                parent_runner=self._parent_runner,
                parent_model=self._parent_model,
                all_tools=self._all_tools,
                app_state=self._app_state,
            )
            workers.append(w)
            worker_states.append(WorkerState(w.label, task.agent_type, "pending"))

        # Initialise TUI pool-state signal.
        pool_state = SubagentPoolState(
            pool_id=self.pool_id,
            total=len(workers),
            workers=worker_states,
        )
        self._set_pool_state(pool_state)

        # Emit pool-started kernel + scroll-buffer events.
        await self._emit_pool_started()
        self._append_scroll_event("subagent_pool_started", {
            "total": len(workers),
            "workers": [{"label": ws.label, "type": ws.agent_type} for ws in worker_states],
        })

        # Run workers with a semaphore bounding concurrency.
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def _bounded(worker: SubagentWorker, ws: WorkerState) -> SubagentResult:
            async with semaphore:
                ws.status = "running"
                self._set_pool_state(pool_state)
                await self._emit_worker_started(worker)
                result = await worker.run()
                ws.status = "done" if result.ok else "failed"
                self._set_pool_state(pool_state)
                await self._emit_worker_done(result)
                self._append_scroll_event(
                    "subagent_worker_done" if result.ok else "subagent_worker_done",
                    {
                        "label":       result.label,
                        "ok":          result.ok,
                        "error":       result.error,
                        "duration_ms": result.duration_ms,
                        "done":        pool_state.done,
                        "total":       pool_state.total,
                    },
                )
                return result

        raw = await asyncio.gather(
            *[_bounded(w, ws) for w, ws in zip(workers, worker_states)],
            return_exceptions=True,
        )

        # Normalise: SubagentWorker.run() swallows exceptions, but guard anyway.
        results: list[SubagentResult] = []
        for i, item in enumerate(raw):
            if isinstance(item, SubagentResult):
                results.append(item)
            else:
                task = self._tasks[i] if i < len(self._tasks) else SubagentTask("?", "?", "?")
                results.append(SubagentResult(
                    task_id=task.task_id,
                    agent_type=task.agent_type,
                    label=f"{task.agent_type} #{i + 1}",
                    ok=False,
                    text="",
                    error=str(item),
                ))

        aggregated = _aggregate(self.pool_id, results, self._registry)
        await self._emit_pool_completed(aggregated)
        self._append_scroll_event("subagent_pool_done", {
            "succeeded": aggregated.succeeded,
            "total":     aggregated.total,
            "failed":    aggregated.failed,
        })
        # Clear the TUI pool-state so the footer hides.
        self._set_pool_state(None)
        return aggregated

    # ── TUI helpers ──────────────────────────────────────────────────────────

    def _set_pool_state(self, state: SubagentPoolState | None) -> None:
        if self._conv_store is not None and hasattr(self._conv_store, "subagent_pool_state"):
            self._conv_store.subagent_pool_state.set(state)

    def _append_scroll_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self._conv_store is not None and hasattr(self._conv_store, "append_event"):
            self._conv_store.append_event(kind, payload)

    # ── kernel event helpers ─────────────────────────────────────────────────

    async def _emit_pool_started(self) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415
        await self._processor.emit(Event.create("SubagentPoolStarted", {
            "pool_id":       self.pool_id,
            "tasks":         [{"task_id": t.task_id, "type": t.agent_type,
                               "description": t.task_description} for t in self._tasks],
            "max_concurrent": self._max_concurrent,
        }))

    async def _emit_worker_started(self, worker: SubagentWorker) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415
        await self._processor.emit(Event.create("SubagentStarted", {
            "pool_id": self.pool_id,
            "task_id": worker._task.task_id,
            "type":    worker._task.agent_type,
            "label":   worker.label,
            "task":    worker._task.task_description,
        }))

    async def _emit_worker_done(self, result: SubagentResult) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415
        event_type = "SubagentCompleted" if result.ok else "SubagentFailed"
        await self._processor.emit(Event.create(event_type, {
            "pool_id":     self.pool_id,
            "task_id":     result.task_id,
            "type":        result.agent_type,
            "label":       result.label,
            "text":        result.text[:_MAX_RESULT_CHARS],
            "error":       result.error,
            "duration_ms": result.duration_ms,
        }))

    async def _emit_pool_completed(self, agg: AggregatedResult) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415
        await self._processor.emit(Event.create("SubagentPoolCompleted", {
            "pool_id":   self.pool_id,
            "total":     agg.total,
            "succeeded": agg.succeeded,
            "failed":    agg.failed,
            "text":      agg.text,
        }))


# ── unknown-type sentinel worker ──────────────────────────────────────────────

class _UnknownTypeWorker:
    """Placeholder that immediately returns a failed result for unknown types."""

    def __init__(self, task: SubagentTask) -> None:
        self._task = task
        self.label = f"{task.agent_type} #?"

    async def run(self) -> SubagentResult:
        return SubagentResult(
            task_id=self._task.task_id,
            agent_type=self._task.agent_type,
            label=self.label,
            ok=False,
            text="",
            error=f"unknown subagent type: {self._task.agent_type!r}",
        )


# ── aggregation ───────────────────────────────────────────────────────────────

def _aggregate(
    pool_id:  str,
    results:  list[SubagentResult],
    registry: SubagentTypeRegistry = DEFAULT_REGISTRY,
) -> AggregatedResult:
    """Produce labelled-concatenation text from a list of results."""
    succeeded = sum(1 for r in results if r.ok)
    failed    = len(results) - succeeded

    # Group results by type for custom aggregators.
    by_type: dict[str, list[SubagentResult]] = {}
    for r in results:
        by_type.setdefault(r.agent_type, []).append(r)

    sections: list[str] = []
    for r in results:
        # Check for a custom aggregator for this type (applied once per type).
        agg = registry.get_aggregator(r.agent_type)
        if agg is not None and r.agent_type in by_type:
            # Custom aggregator: produce one section for all results of this type.
            custom_text = agg.aggregate(by_type.pop(r.agent_type))
            sections.append(f"=== {r.agent_type} (custom aggregator) ===\n{custom_text}")
            continue
        if r.agent_type not in by_type:
            # Already consumed by custom aggregator above.
            continue
        dur = f"{r.duration_ms / 1_000:.1f}s"
        status = f"✓ {dur}" if r.ok else f"✗ {r.error or 'failed'}"
        header = f"=== {r.label} ({status}) ==="
        body   = r.text[:_MAX_RESULT_CHARS] if r.ok else f"[failed: {r.error}]"
        sections.append(f"{header}\n{body}")

    text = "\n\n".join(sections)
    return AggregatedResult(
        pool_id=pool_id,
        total=len(results),
        succeeded=succeeded,
        failed=failed,
        text=text,
    )


# ── convenience coroutine ─────────────────────────────────────────────────────

async def run_pool(
    tasks:          list[SubagentTask],
    parent_runner:  AgentRunnerBase,
    parent_model:   str,
    all_tools:      list[Any],
    max_concurrent: int = 4,
    app_state:      Any | None = None,
    processor:      Any | None = None,
    conv_store:     Any | None = None,
    registry:       SubagentTypeRegistry = DEFAULT_REGISTRY,
) -> AggregatedResult:
    """Create a SubagentPool and run it.  Convenience wrapper."""
    pool = SubagentPool(
        tasks=tasks,
        parent_runner=parent_runner,
        parent_model=parent_model,
        all_tools=all_tools,
        max_concurrent=max_concurrent,
        app_state=app_state,
        processor=processor,
        conv_store=conv_store,
        registry=registry,
    )
    return await pool.run()
