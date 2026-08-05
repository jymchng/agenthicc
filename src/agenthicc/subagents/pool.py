"""SubagentPool — concurrent worker execution (PRD-124)."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, cast

from agenthicc.subagents.types import (
    DEFAULT_REGISTRY,
    DEFAULT_SUBAGENT_TIMEOUT_S,
    SubagentTypeRegistry,
    SubagentTypeSpec,
)

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._transport import TokenUsage
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.plugins.registry import ToolRegistry
    from agenthicc.tui.conversation_store import AppState, ConversationStore
    from agenthicc.runners.retry import RetryConfig
    from agenthicc.runners.usage_ledger import UsageLedger

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


def _validate_timeout_s(value: object) -> float:
    """Validate a worker timeout expressed in seconds."""
    if isinstance(value, bool):
        raise ValueError("timeout_s must be a finite number greater than zero")
    if not isinstance(value, (int, float, str)):
        raise ValueError("timeout_s must be a finite number greater than zero")
    try:
        timeout_s = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_s must be a finite number greater than zero") from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be a finite number greater than zero")
    return timeout_s


class _TransportLike(Protocol):
    """Minimal transport surface used by the usage-normalising proxy."""

    async def complete(self, messages: list[object], **kwargs: object) -> object: ...


def _token_count(value: object) -> int:
    """Return a safe integer for provider usage fields.

    OpenAI-compatible gateways are allowed to return ``null`` for usage
    fields, especially when usage accounting is disabled.  Lauren-ai's
    ``TokenUsage.__add__`` assumes those fields are integers and otherwise
    raises ``TypeError: int + NoneType`` while a subagent is finishing.
    Missing or malformed counts mean *unknown*, not a failed worker; use zero
    and preserve the worker's actual response.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _usage_field(usage: object | None, name: str, default: object = 0) -> object:
    """Read a possibly absent usage attribute without assuming a provider type."""
    if usage is None:
        return default
    try:
        return object.__getattribute__(usage, name)
    except AttributeError:
        return default


def _normalise_usage(usage: object | None) -> TokenUsage:
    """Convert a provider's nullable usage object to Lauren's numeric type."""
    from lauren_ai._transport import TokenUsage  # noqa: PLC0415

    metadata = _usage_field(usage, "provider_metadata", {})
    return TokenUsage(
        input_tokens=_token_count(_usage_field(usage, "input_tokens")),
        output_tokens=_token_count(_usage_field(usage, "output_tokens")),
        cache_read_tokens=_token_count(_usage_field(usage, "cache_read_tokens")),
        cache_write_tokens=_token_count(_usage_field(usage, "cache_write_tokens")),
        reasoning_tokens=_token_count(_usage_field(usage, "reasoning_tokens")),
        audio_input_tokens=_token_count(_usage_field(usage, "audio_input_tokens")),
        audio_output_tokens=_token_count(_usage_field(usage, "audio_output_tokens")),
        provider_metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _normalise_completion(value: object) -> object:
    """Replace nullable usage on a completion or stream chunk."""
    from lauren_ai._transport import Completion, CompletionChunk  # noqa: PLC0415

    if isinstance(value, Completion):
        return replace(value, usage=_normalise_usage(value.usage))
    if isinstance(value, CompletionChunk) and value.usage is not None:
        return replace(value, usage=_normalise_usage(value.usage))
    return value


async def _normalised_stream(stream: AsyncIterator[object]) -> AsyncIterator[object]:
    """Yield stream chunks with nullable usage fields converted to zero."""
    async for chunk in stream:
        yield _normalise_completion(chunk)


class _UsageNormalisingTransport:
    """Transparent transport proxy that protects Lauren's usage arithmetic."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def complete(self, messages: list[object], **kwargs: object) -> object:
        transport = cast(_TransportLike, self._inner)
        result = await transport.complete(messages, **kwargs)
        if kwargs.get("stream") is True:
            return _normalised_stream(cast(AsyncIterator[object], result))
        return _normalise_completion(result)

    def __getattr__(self, name: str) -> object:
        """Delegate optional transport methods such as ``count_tokens``."""
        return object.__getattribute__(self._inner, name)


# ── TUI state models ──────────────────────────────────────────────────────────


@dataclass
class WorkerState:
    """Live status of one worker — used to drive the footer worker grid."""

    label: str  # "explorer #1"
    agent_type: str
    status: str = "pending"  # pending | running | done | failed


@dataclass
class SubagentPoolState:
    """Live summary of an active SubagentPool — stored on ConversationStore signal."""

    pool_id: str
    total: int
    workers: list[WorkerState] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for w in self.workers if w.status in ("done", "failed"))


# ── task / result data models ─────────────────────────────────────────────────


@dataclass
class SubagentTask:
    """One unit of work assigned to a subagent worker."""

    task_id: str
    agent_type: str
    task_description: str
    context: str = ""


@dataclass
class SubagentResult:
    """Outcome of one subagent worker execution."""

    task_id: str
    agent_type: str
    label: str  # "explorer #1", "tester #2", …
    ok: bool
    text: str  # AgentResponse.content or a tool-evidence fallback (plain text)
    error: str = ""
    duration_ms: float = 0.0
    tool_calls: tuple[str, ...] = ()  # Provider tools actually dispatched
    changed_paths: tuple[str, ...] = ()  # Paths supplied to mutation tools


@dataclass
class AggregatedResult:
    """Concatenated result from all workers in one pool run."""

    pool_id: str
    total: int
    succeeded: int
    failed: int
    text: str  # labelled concatenation delivered to parent as tool result


_MUTATING_TOOL_NAMES = frozenset(
    {
        "append_file",
        "apply_diff",
        "batch_copy",
        "batch_delete",
        "batch_move",
        "batch_write",
        "copy_file",
        "delete_file",
        "make_directory",
        "move_file",
        "patch_file",
        "touch_file",
        "truncate_file",
        "write_file",
    }
)


def _tool_name(tool: object) -> str:
    """Return a callable or Agenthicc tool object's provider name."""
    name = getattr(tool, "__name__", "")
    if isinstance(name, str) and name:
        return name
    name = type(tool).__dict__.get("name", "")
    return name if isinstance(name, str) else ""


def _is_mutating_tool(tool: object) -> bool:
    """Return whether *tool* is known to change workspace state.

    Capability metadata is authoritative for decorated tools.  The name
    fallback preserves the contract for legacy Tool objects and for callers
    that pass a compatible callable without Agenthicc metadata.
    """
    if _tool_name(tool) in _MUTATING_TOOL_NAMES:
        return True
    try:
        from agenthicc.tools.capabilities import ToolCapability, get_tool_capabilities

        return ToolCapability.WRITE in get_tool_capabilities(tool)
    except (ImportError, AttributeError):
        return False


def _paths_from_tool_input(tool_name: str, value: Mapping[str, object]) -> list[str]:
    """Extract bounded, human-readable path hints from a write call."""
    paths: list[str] = []
    for key in ("path", "source", "destination"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            paths.append(candidate)
    if tool_name == "batch_write":
        entries = value.get("files")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, Mapping):
                    candidate = entry.get("path")
                    if isinstance(candidate, str) and candidate:
                        paths.append(candidate)
    return paths


def _tool_call_summary(
    tool_calls: tuple[str, ...],
    changed_paths: tuple[str, ...],
) -> str:
    """Build a useful fallback when a provider returns no final prose."""
    if not tool_calls:
        return ""
    calls = ", ".join(tool_calls)
    summary = f"Executed tool call(s): {calls}."
    if changed_paths:
        summary += " Affected path(s): " + ", ".join(changed_paths) + "."
    return summary


# ── worker ────────────────────────────────────────────────────────────────────


def _expand_allowed(
    allowed_tools: frozenset[str],
    registry: ToolRegistry | None,
) -> frozenset[str]:
    """Expand glob patterns in *allowed_tools* using *registry*.

    Patterns ending in ``".*"`` (e.g. ``"fs.*"``) are expanded to all tool
    names registered under that group.  Literal names pass through unchanged.
    When no registry is provided, all patterns are treated as literal names
    (backward-compatible behaviour).

    :param allowed_tools: Raw frozenset from ``SubagentTypeSpec.allowed_tools``.
    :param registry: ``ToolRegistry`` used for group-name lookups.
    :return: Expanded frozenset of concrete provider tool names.
    """
    if registry is None:
        return allowed_tools
    result: frozenset[str] = frozenset()
    for pattern in allowed_tools:
        result |= registry.glob_expand(pattern)
    return result


class SubagentWorker:
    """Executes one SubagentTask using an isolated AgentRunnerBase instance."""

    def __init__(
        self,
        task: SubagentTask,
        spec: SubagentTypeSpec,
        index: int,
        parent_runner: AgentRunnerBase,
        parent_model: str,
        all_tools: list[object],
        app_state: AppState | None = None,
        registry: ToolRegistry | None = None,
        retry_config: "RetryConfig | None" = None,
        usage_ledger: "UsageLedger | None" = None,
        conversation_id: str = "",
        parent_run_id: str = "",
        provider_options: Mapping[str, object] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._task = task
        self._spec = spec
        self._index = index
        self._parent_runner = parent_runner
        self._parent_model = parent_model
        self._all_tools = all_tools
        self._app_state = app_state
        self._registry = registry
        self._retry_config: RetryConfig | None = retry_config
        self._usage_ledger = usage_ledger
        self._conversation_id = conversation_id
        self._parent_run_id = parent_run_id
        self._provider_options = dict(provider_options or {})
        self._timeout_s = None if timeout_s is None else _validate_timeout_s(timeout_s)
        self.label = f"{spec.name} #{index}"
        self._tool_calls: list[str] = []
        self._successful_tool_calls: list[str] = []
        self._changed_paths: list[str] = []
        # Expand glob patterns once at construction time.
        self._effective_allowed: frozenset[str] = _expand_allowed(spec.allowed_tools, registry)

    async def run(self) -> SubagentResult:
        """Execute the task; return SubagentResult regardless of success/failure."""
        t0 = time.monotonic()
        self._tool_calls.clear()
        self._successful_tool_calls.clear()
        self._changed_paths.clear()
        try:
            text = await asyncio.wait_for(
                self._execute(),
                timeout=(
                    self._timeout_s if self._timeout_s is not None else self._spec.max_turn_time_s
                ),
            )
            duration_ms = (time.monotonic() - t0) * 1_000
            tool_calls = tuple(self._tool_calls)
            successful_tool_calls = tuple(self._successful_tool_calls)
            changed_paths = tuple(dict.fromkeys(self._changed_paths))

            # An implementer is a mutation-capable role, not a planning role.
            # A provider can return a valid prose completion without ever
            # calling a write tool; treating that as success is what made pools
            # report completed work while files remained unchanged.
            if self._spec.name == "implementer" and not successful_tool_calls:
                return SubagentResult(
                    task_id=self._task.task_id,
                    agent_type=self._task.agent_type,
                    label=self.label,
                    ok=False,
                    text=text,
                    error=(
                        "implementer completed without a successful mutating tool call; "
                        "the task was reported but no mutation was recorded"
                    ),
                    duration_ms=duration_ms,
                    tool_calls=tool_calls,
                    changed_paths=changed_paths,
                )

            if not text.strip():
                text = _tool_call_summary(tool_calls, changed_paths)
            if not text.strip():
                return SubagentResult(
                    task_id=self._task.task_id,
                    agent_type=self._task.agent_type,
                    label=self.label,
                    ok=False,
                    text="",
                    error="agent returned no final summary and executed no tools",
                    duration_ms=duration_ms,
                    tool_calls=tool_calls,
                    changed_paths=changed_paths,
                )
            return SubagentResult(
                task_id=self._task.task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=True,
                text=text,
                duration_ms=duration_ms,
                tool_calls=tool_calls,
                changed_paths=changed_paths,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.monotonic() - t0) * 1_000
            return SubagentResult(
                task_id=self._task.task_id,
                agent_type=self._task.agent_type,
                label=self.label,
                ok=False,
                text="",
                error=(
                    "timed out after "
                    f"{self._timeout_s if self._timeout_s is not None else self._spec.max_turn_time_s:.0f}s"
                ),
                duration_ms=duration_ms,
                tool_calls=tuple(self._tool_calls),
                changed_paths=tuple(dict.fromkeys(self._changed_paths)),
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
                tool_calls=tuple(self._tool_calls),
                changed_paths=tuple(dict.fromkeys(self._changed_paths)),
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
                tool_calls=tuple(self._tool_calls),
                changed_paths=tuple(dict.fromkeys(self._changed_paths)),
            )

    async def _execute(self) -> str:
        """Build an isolated agent and run the task. Returns response content."""
        from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
        from lauren_ai._agents._runner import AgentRunnerBase as _RunnerBase  # noqa: PLC0415
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from lauren_ai import AfterToolHookDecision, ToolCallContext, ToolHook  # noqa: PLC0415
        from lauren_ai._signals import SignalBus, ToolCallStarted  # noqa: PLC0415
        from agenthicc.runners.tool_populator import populate_agent_tools  # noqa: PLC0415

        # Filter the full tool list to the expanded allowed set.
        # _effective_allowed already has glob patterns resolved to concrete names.
        filtered = [t for t in self._all_tools if _tool_name(t) in self._effective_allowed]
        mutating_tools = frozenset(_tool_name(tool) for tool in filtered if _is_mutating_tool(tool))
        if self._spec.name == "implementer" and not mutating_tools:
            raise RuntimeError(
                "implementer has no write-capable tools after session filtering; "
                "the parent mode or phase did not expose a file mutation tool"
            )

        # Build the system prompt: type prompt + optional context.
        system = self._spec.system_prompt
        if self._task.context:
            system = f"{system}\n\n[ADDITIONAL CONTEXT]\n{self._task.context}"

        # Construct the @agent class and runner.
        @agent_decorator(model=self._parent_model, system=system)
        @use_tools(*filtered)
        class _SubAgent: ...  # type: ignore[type-var]  # lauren-ai dynamic subagent class

        agent_instance = _SubAgent()
        populate_agent_tools(agent_instance, filtered)

        hooks: list[object] = []
        if self._app_state is not None:
            from agenthicc.tools.capability_gate import ToolCapabilityGate  # noqa: PLC0415

            hooks.append(ToolCapabilityGate(self._app_state))

        worker = self

        class _MutationEvidenceHook(ToolHook):
            """Record only mutation calls whose callable returned success."""

            async def after_tool_call(
                self,
                result: object,
                ctx: ToolCallContext,
            ) -> AfterToolHookDecision:
                if ctx.tool_name not in mutating_tools:
                    return AfterToolHookDecision.proceed()
                if isinstance(result, Mapping) and result.get("ok") is False:
                    return AfterToolHookDecision.proceed()
                worker._successful_tool_calls.append(ctx.tool_name)
                return AfterToolHookDecision.proceed()

        hooks.append(_MutationEvidenceHook())

        signals = SignalBus()

        @signals.on(ToolCallStarted)
        async def _record_tool_started(event: ToolCallStarted) -> None:
            name = str(event.tool_name)
            self._tool_calls.append(name)
            if name in mutating_tools:
                self._changed_paths.extend(_paths_from_tool_input(name, event.input))

        runner = _RunnerBase(
            # OpenAI-compatible endpoints may return nullable usage fields.
            # Lauren's non-streaming runner adds those fields directly, so
            # isolate each worker behind the normalising proxy.
            transport=_UsageNormalisingTransport(self._parent_runner._transport),
            signals=signals,
            global_hooks=hooks or None,
        )

        usage_tracker = None
        if self._usage_ledger is not None:
            from agenthicc.runners.usage_ledger import UsageRunTracker  # noqa: PLC0415

            usage_tracker = UsageRunTracker(
                self._usage_ledger,
                run_id=f"{self._parent_run_id}:subagent:{self._task.task_id}",
                provider="unknown",
                model=self._parent_model,
                category="subagent",
                agent_name=self.label,
            )

        from lauren_ai._config import AgentConfig  # noqa: PLC0415

        memory = ShortTermMemory(max_tokens=8_000)
        result: dict[str, str] = {"text": ""}
        from dataclasses import fields as dataclass_fields  # noqa: PLC0415

        agent_fields = {field.name for field in dataclass_fields(AgentConfig)}
        config_options: dict[str, object] = {
            "temperature": self._provider_options.get("temperature"),
            "top_p": self._provider_options.get("top_p"),
            "max_completion_tokens": self._provider_options.get("max_completion_tokens"),
            "request_options": self._provider_options.get("request_options"),
        }
        if config_options["request_options"] is not None:
            from agenthicc.config import RequestOptionSettings  # noqa: PLC0415

            if isinstance(config_options["request_options"], RequestOptionSettings):
                config_options["request_options"] = config_options["request_options"].resolve()
        config_options = {
            name: value
            for name, value in config_options.items()
            if name in agent_fields and value is not None
        }

        def _agent_config() -> AgentConfig:
            agent_constructor = cast(Callable[..., AgentConfig], AgentConfig)
            return agent_constructor(
                max_turns=self._spec.max_turns,
                parallel_tool_calls=True,
                **config_options,
            )

        async def _do_run() -> None:
            # Keep lightweight runner doubles and legacy integrations working
            # when accounting is disabled; production sessions always pass
            # the scoped correlation fields with the ledger sink.
            if usage_tracker is not None:
                response = await runner.run(
                    agent_instance,
                    self._task.task_description,
                    conversation_id=self._conversation_id or None,
                    run_id=f"{self._parent_run_id}:subagent:{self._task.task_id}",
                    memory=memory,
                    config_override=_agent_config(),
                    event_sinks=[usage_tracker.sink],
                )
            else:
                response = await runner.run(
                    agent_instance,
                    self._task.task_description,
                    memory=memory,
                    config_override=_agent_config(),
                )
            result["text"] = response.content or ""

        # PRD-126 gap 3: subagents call runner.run() directly (not via _stream),
        # so they wrap it in the same snapshot-rollback retry.  The fresh per-call
        # memory is snapshotted (empty) before each attempt and restored on a
        # transient error so runner.run() re-adds the user message cleanly.
        try:
            if self._retry_config is not None:
                from agenthicc.runners.retry import run_with_transport_retry  # noqa: PLC0415

                await run_with_transport_retry(
                    _do_run,
                    config=self._retry_config,
                    memory=memory,
                )
            else:
                await _do_run()
        finally:
            if usage_tracker is not None:
                usage_tracker.finalize("completed" if result["text"] else "failed")
        return result["text"]


# ── pool ──────────────────────────────────────────────────────────────────────


class SubagentPool:
    """Runs a set of SubagentTasks concurrently bounded by *max_concurrent*.

    Creates one SubagentWorker per task, schedules them under an asyncio
    Semaphore, and aggregates results into a labelled plain-text digest.
    """

    def __init__(
        self,
        tasks: list[SubagentTask],
        parent_runner: AgentRunnerBase,
        parent_model: str,
        all_tools: list[object],
        max_concurrent: int = 4,
        app_state: AppState | None = None,
        processor: EventProcessor | None = None,
        conv_store: ConversationStore | None = None,
        registry: SubagentTypeRegistry = DEFAULT_REGISTRY,
        tool_registry: ToolRegistry | None = None,
        retry_config: "RetryConfig | None" = None,
        usage_ledger: "UsageLedger | None" = None,
        conversation_id: str = "",
        parent_run_id: str = "",
        provider_options: Mapping[str, object] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.pool_id = uuid.uuid4().hex
        self._tasks = tasks
        self._parent_runner = parent_runner
        self._parent_model = parent_model
        self._all_tools = all_tools
        self._max_concurrent = max_concurrent
        self._app_state = app_state
        self._processor = processor
        self._conv_store = conv_store
        self._registry = registry
        self._tool_registry = tool_registry
        self._retry_config: RetryConfig | None = retry_config
        self._usage_ledger = usage_ledger
        self._conversation_id = conversation_id
        self._parent_run_id = parent_run_id
        self._provider_options = dict(provider_options or {})
        self._timeout_s = None if timeout_s is None else _validate_timeout_s(timeout_s)

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
                worker_states.append(
                    WorkerState(f"{task.agent_type} #?", task.agent_type, "pending")
                )
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
                registry=self._tool_registry,
                retry_config=self._retry_config,
                usage_ledger=self._usage_ledger,
                conversation_id=self._conversation_id,
                parent_run_id=self._parent_run_id,
                provider_options=self._provider_options,
                timeout_s=self._timeout_s,
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
        self._append_scroll_event(
            "subagent_pool_started",
            {
                "total": len(workers),
                "workers": [{"label": ws.label, "type": ws.agent_type} for ws in worker_states],
                "timeout_s": self._timeout_s or DEFAULT_SUBAGENT_TIMEOUT_S,
            },
        )

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
                        "label": result.label,
                        "ok": result.ok,
                        "error": result.error,
                        "duration_ms": result.duration_ms,
                        "summary": result.text[:_MAX_RESULT_CHARS],
                        "tool_calls": list(result.tool_calls),
                        "changed_paths": list(result.changed_paths),
                        "done": pool_state.done,
                        "total": pool_state.total,
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
                results.append(
                    SubagentResult(
                        task_id=task.task_id,
                        agent_type=task.agent_type,
                        label=f"{task.agent_type} #{i + 1}",
                        ok=False,
                        text="",
                        error=str(item),
                    )
                )

        aggregated = _aggregate(self.pool_id, results, self._registry)
        await self._emit_pool_completed(aggregated)
        self._append_scroll_event(
            "subagent_pool_done",
            {
                "succeeded": aggregated.succeeded,
                "total": aggregated.total,
                "failed": aggregated.failed,
            },
        )
        # Clear the TUI pool-state so the footer hides.
        self._set_pool_state(None)
        return aggregated

    # ── TUI helpers ──────────────────────────────────────────────────────────

    def _set_pool_state(self, state: SubagentPoolState | None) -> None:
        if self._conv_store is not None and hasattr(self._conv_store, "subagent_pool_state"):
            self._conv_store.subagent_pool_state.set(state)

    def _append_scroll_event(self, kind: str, payload: dict[str, object]) -> None:
        if self._conv_store is not None and hasattr(self._conv_store, "append_event"):
            self._conv_store.append_event(kind, payload)

    # ── kernel event helpers ─────────────────────────────────────────────────

    async def _emit_pool_started(self) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415

        await self._processor.emit(
            Event.create(
                "SubagentPoolStarted",
                {
                    "pool_id": self.pool_id,
                    "tasks": [
                        {
                            "task_id": t.task_id,
                            "type": t.agent_type,
                            "description": t.task_description,
                        }
                        for t in self._tasks
                    ],
                    "max_concurrent": self._max_concurrent,
                    "timeout_s": self._timeout_s or DEFAULT_SUBAGENT_TIMEOUT_S,
                },
            )
        )

    async def _emit_worker_started(self, worker: SubagentWorker) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415

        await self._processor.emit(
            Event.create(
                "SubagentStarted",
                {
                    "pool_id": self.pool_id,
                    "task_id": worker._task.task_id,
                    "type": worker._task.agent_type,
                    "label": worker.label,
                    "task": worker._task.task_description,
                },
            )
        )

    async def _emit_worker_done(self, result: SubagentResult) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415

        event_type = "SubagentCompleted" if result.ok else "SubagentFailed"
        await self._processor.emit(
            Event.create(
                event_type,
                {
                    "pool_id": self.pool_id,
                    "task_id": result.task_id,
                    "type": result.agent_type,
                    "label": result.label,
                    "text": result.text[:_MAX_RESULT_CHARS],
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                    "tool_calls": list(result.tool_calls),
                    "changed_paths": list(result.changed_paths),
                },
            )
        )

    async def _emit_pool_completed(self, agg: AggregatedResult) -> None:
        if self._processor is None:
            return
        from agenthicc.kernel import Event  # noqa: PLC0415

        await self._processor.emit(
            Event.create(
                "SubagentPoolCompleted",
                {
                    "pool_id": self.pool_id,
                    "total": agg.total,
                    "succeeded": agg.succeeded,
                    "failed": agg.failed,
                    "text": agg.text,
                },
            )
        )


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
    pool_id: str,
    results: list[SubagentResult],
    registry: SubagentTypeRegistry = DEFAULT_REGISTRY,
) -> AggregatedResult:
    """Produce labelled-concatenation text from a list of results."""
    succeeded = sum(1 for r in results if r.ok)
    failed = len(results) - succeeded

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
        if r.ok:
            body = r.text[:_MAX_RESULT_CHARS]
        elif r.text:
            body = f"[failed: {r.error}]\n{r.text[:_MAX_RESULT_CHARS]}"
        else:
            body = f"[failed: {r.error}]"
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
    tasks: list[SubagentTask],
    parent_runner: AgentRunnerBase,
    parent_model: str,
    all_tools: list[object],
    max_concurrent: int = 4,
    app_state: AppState | None = None,
    processor: EventProcessor | None = None,
    conv_store: ConversationStore | None = None,
    registry: SubagentTypeRegistry = DEFAULT_REGISTRY,
    tool_registry: ToolRegistry | None = None,
    usage_ledger: "UsageLedger | None" = None,
    conversation_id: str = "",
    parent_run_id: str = "",
    provider_options: Mapping[str, object] | None = None,
    timeout_s: float | None = None,
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
        tool_registry=tool_registry,
        usage_ledger=usage_ledger,
        conversation_id=conversation_id,
        parent_run_id=parent_run_id,
        provider_options=provider_options,
        timeout_s=timeout_s,
    )
    return await pool.run()
