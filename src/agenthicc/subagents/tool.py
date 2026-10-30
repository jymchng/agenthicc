"""Build the session-bound ``spawn_subagents`` tool (PRD-124).

The tool is deliberately a factory rather than a process-global callable. A
parent turn supplies the factory with the effective model, already-filtered
visible tools, ConversationStore, retry/usage services, approval policy, and
the session's durable conversation journal. The resulting callable validates a
list of typed task requests, checks the complete-pool resume cache, runs a
:class:`~agenthicc.subagents.pool.SubagentPool` when necessary, and returns a
labelled aggregate suitable for a provider tool result. The aggregate retains
complete worker output; only scroll/kernel presentation fields are bounded.

The provider schema is part of this module's contract.  ``context`` is an
optional task field, but it is included in the generated decorated metadata
because it becomes part of the worker prompt and resume fingerprint.  Keep
the schema repair in :func:`_augment_type_schema` when changing the input
shape; it accommodates the lauren-ai TypedDict schema walker used by the
supported runtime.

NOTE: intentionally no ``from __future__ import annotations`` — @tool()
inspects type annotations at decoration time using ``get_type_hints()``.
Postponed evaluation (PEP 563) breaks that inspection.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, TypedDict, cast

from agenthicc.config import SubagentSettings
from agenthicc.result_contracts import project_result_value, validate_result_value
from agenthicc.subagents.communication import (
    PoolHandle,
    make_parent_communication_tools,
    registry_for_session,
)
from agenthicc.subagents.policy import SubagentExecutionPolicy
from agenthicc.subagents.pool import SubagentTask, _redact_result_value
from agenthicc.subagents.types import SubagentTypeRegistry, SubagentTypeSpec
from agenthicc.tools.base import tool_name

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from agenthicc.config import ExecutionSettings
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.plugins.registry import ToolRegistry
    from agenthicc.runners.retry import RetryConfig
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.tools.approval import ApprovalBroker
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy
    from agenthicc.tui.conversation_store import AppState, ConversationStore

__all__ = ["make_spawn_subagents_tool"]

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


def _model_allowed(model: str, provider: str, allowed_models: frozenset[str] | None) -> bool:
    """Check a model ID against the operator's optionally provider-qualified allow-list."""
    if allowed_models is None:
        return True
    return model in allowed_models or f"{provider}/{model}" in allowed_models


class _SpawnTaskInput(TypedDict):
    """Structured task input accepted by ``spawn_subagents``.

    Lauren-ai builds the provider JSON schema from this TypedDict.  Keeping
    ``context`` in the schema is important: the runtime has always accepted
    it, and it is part of the worker's prompt, so omitting it made the
    model-facing contract disagree with the actual validator.
    """

    type: str
    task: str
    # ``str | None`` is intentional rather than ``NotRequired[str]``.  The
    # lauren-ai schema generator treats Optional annotations as non-required,
    # while its current TypedDict walker does not yet understand PEP 655's
    # ``NotRequired`` marker.
    context: str | None


def make_spawn_subagents_tool(
    parent_runner: "AgentRunnerBase",
    parent_model: str,
    all_tools: list[object],
    max_concurrent: int | None = None,
    subagent_settings: SubagentSettings | None = None,
    app_state: "AppState | None" = None,
    processor: "EventProcessor | None" = None,
    conv_store: "ConversationStore | None" = None,
    registry: SubagentTypeRegistry | None = None,
    tool_registry: "ToolRegistry | None" = None,
    retry_config: "RetryConfig | None" = None,
    usage_ledger: "UsageLedger | None" = None,
    conversation_id: str = "",
    parent_run_id: str = "",
    provider_options: Mapping[str, object] | None = None,
    approval_svc: "ApprovalBroker | None" = None,
    workspace_access: "WorkspaceAccessPolicy | None" = None,
    conversation_journal: "ConversationJournal | None" = None,
    policy: SubagentExecutionPolicy | None = None,
    provider: str = "unknown",
    execution_settings: "ExecutionSettings | None" = None,
    default_model: str = "",
    allowed_models: frozenset[str] | None = None,
    parent_depth: int = 0,
    parent_pool_id: str = "",
    ancestry: tuple[tuple[str, str], ...] = (),
) -> Callable[..., object]:
    """Return a ``spawn_subagents`` @tool()-decorated function.

    Closes over *parent_runner* and *parent_model* so the tool can build
    isolated subagent workers that share the parent's LLM transport.

    The workers do not receive the parent's message history. They receive
    their task description as a new user message and optional task context in
    their role system prompt. Their aggregate is the only value returned to
    the parent turn. This keeps concurrent workers isolated while preserving
    one visible, resumable orchestration point. In a real session, worker and
    complete-pool records are also written to the shared conversation journal
    before this callable returns, protecting output if the parent is cancelled
    before it commits its tool exchange.

    Parameters
    ----------
    parent_runner:
        The parent turn's ``AgentRunnerBase`` — provides the transport.
    parent_model:
        The effective model ID string (e.g. ``"anthropic/deepseek-v4-flash"``).
    all_tools:
        Full list of agent tools available in this session.  The pool filters
        this list to each subagent type's ``allowed_tools`` set.
    max_concurrent:
        Default semaphore bound.  May be overridden per call via the
        ``max_concurrent`` tool parameter.
    app_state:
        ``AppState`` used to derive each worker's isolated Yolo policy state.
        The parent state is never mutated; ``None`` in headless / test
        contexts leaves capability hooks disabled.
    processor:
        Kernel ``EventProcessor`` for emitting ``SubagentPool*`` events.
        ``None`` disables kernel event emission.
    conv_store:
        ``ConversationStore`` for appending a simple scroll-buffer summary.
        ``None`` disables scroll-buffer output.
    registry:
        ``SubagentTypeRegistry`` to look up type specs.  Defaults to
        ``DEFAULT_REGISTRY`` when ``None``.
    provider_options:
        Provider sampling/request options copied into each worker call.
    conversation_id / parent_run_id:
        Correlation identifiers used for provider conversation continuity and
        usage-ledger records.  They do not merge worker message histories.
    timeout_s (returned tool argument):
        Wall-clock timeout in seconds for each worker in one invocation.
        Defaults to one hour and may be overridden by the parent model.
    approval_svc:
        The parent session's approval service.  It is retained for hook
        compatibility, but child workers run with an isolated Yolo policy, so
        they do not request foreground Safe-mode approval.  The parent TUI
        mode is never changed by spawning workers.
    workspace_access:
        The parent session's workspace policy, passed to child approval hooks
        for consistent path authorization.
    conversation_journal:
        The parent session's durable conversation journal. Worker results are
        recorded there before completion events are emitted, and complete pool
        results are used as the resume cache even when the reactive TUI store
        was not restored.
    """
    from lauren_ai._tools import tool as _tool

    from agenthicc.subagents.pool import (
        SubagentAdmissionError,
        SubagentPool,
        SubagentTask,
        _validate_timeout_s,
        _worker_context_window,
        _worker_effort,
        _worker_model,
    )
    from agenthicc.config import SubagentSettings
    from agenthicc.subagents.governance import resolve_depth_tier, validate_depth_tiers
    from agenthicc.subagents.types import DEFAULT_REGISTRY, DEFAULT_SUBAGENT_TIMEOUT_S
    from agenthicc.tools.capabilities import tool_read

    _registry = registry if registry is not None else DEFAULT_REGISTRY
    _settings = subagent_settings or SubagentSettings()
    _allowed_models = (
        allowed_models
        if allowed_models is not None
        else frozenset(_settings.allowed_models)
        if _settings.allowed_models is not None
        else None
    )
    validate_depth_tiers(_settings, _registry, _allowed_models)
    _allow_call_overrides = _settings.allow_call_overrides
    requested_default_concurrency = (
        _settings.max_concurrent if max_concurrent is None else max_concurrent
    )
    if (
        isinstance(requested_default_concurrency, bool)
        or not isinstance(requested_default_concurrency, int)
        or requested_default_concurrency < 1
    ):
        raise ValueError("max_concurrent must be a positive integer")
    default_max_concurrent = min(requested_default_concurrency, _settings.max_concurrent)
    effective_policy = policy or SubagentExecutionPolicy.from_app_state(
        app_state,
        visible_tool_names=frozenset(tool_name(item) for item in all_tools),
    )
    continuation_registry = registry_for_session(conversation_id, conv_store)

    @tool_read
    @_tool()
    async def spawn_subagents(
        tasks: list[_SpawnTaskInput],
        max_concurrent: int = default_max_concurrent,
        timeout_s: float = DEFAULT_SUBAGENT_TIMEOUT_S,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, object]:
        """Spawn multiple specialized subagents concurrently and return their aggregated results.

        Each subagent runs in isolation with its own memory and a filtered tool set.
        Results are returned as a labelled plain-text digest you can reason over.

        Args:
            tasks: List of task objects. Each must have:
                   - type (str): Agent type name.
                   - task (str): Description of what this subagent should do.
                   - context (str, optional): Additional background context.
            max_concurrent: Maximum number of subagents running at once; it must not exceed the configured pool limit.
            timeout_s: Maximum wall-clock seconds for each worker in this call
                       (default 3600, or one hour).
            model: Optional model override; available only when the operator enables it.
            effort: Optional reasoning-effort override; available only when enabled.
        """
        pool_depth = parent_depth + 1
        if pool_depth > _settings.max_depth:
            return {
                "ok": False,
                "status": "refused",
                "error": (
                    f"Subagent depth limit exceeded: current depth {pool_depth}, limit "
                    f"{_settings.max_depth}. Increase subagents.max_depth to allow nested pools."
                ),
                "depth": pool_depth,
                "parent_pool_id": parent_pool_id,
            }
        task_ceiling, concurrency_ceiling, task_limit_source, concurrency_limit_source = (
            _settings.limits_for_depth(pool_depth)
        )
        requested_concurrency = (
            max_concurrent
            if isinstance(max_concurrent, int) and not isinstance(max_concurrent, bool)
            else 1
        )
        effective_max_concurrent = min(max(1, requested_concurrency), concurrency_ceiling)
        pool_metadata: dict[str, object] = {
            "depth": pool_depth,
            "parent_pool_id": parent_pool_id,
            "max_tasks": task_ceiling,
            "task_limit_source": task_limit_source,
            "max_concurrent": effective_max_concurrent,
            "max_concurrent_used": effective_max_concurrent,
            "max_concurrent_limit": concurrency_ceiling,
            "max_concurrent_limit_source": concurrency_limit_source,
            "max_active_pools": _settings.max_active_pools,
        }
        try:
            effective_timeout_s = _validate_timeout_s(timeout_s)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        if (model is not None or effort is not None) and not _allow_call_overrides:
            return {
                "ok": False,
                "error": (
                    "Per-call model and effort overrides are disabled. PRD-197 rejects "
                    "model-supplied policy arguments; an operator must enable "
                    "subagents.allow_call_overrides first."
                ),
            }
        call_model: str | None = None
        if model is not None:
            call_model = model.strip()
            if not call_model or not _MODEL_ID.fullmatch(call_model):
                return {"ok": False, "error": "model must be a model identifier"}
            from agenthicc.config import SUPPORTED_PROVIDERS

            model_provider, separator, model_id = call_model.partition("/")
            if separator and model_provider.casefold() in SUPPORTED_PROVIDERS:
                if model_provider.casefold() != provider.casefold():
                    return {
                        "ok": False,
                        "error": (
                            f"model provider {model_provider!r} does not match session provider "
                            f"{provider!r}"
                        ),
                    }
                call_model = model_id
            if not _model_allowed(call_model, provider, _allowed_models):
                return {
                    "ok": False,
                    "error": (
                        f"model {call_model!r} is not in subagents.allowed_models: "
                        f"{', '.join(sorted(_allowed_models or ()))}"
                    ),
                }
        call_effort: str | None = None
        if effort is not None:
            call_effort = effort.strip().casefold()
            if call_effort not in _EFFORTS:
                return {
                    "ok": False,
                    "error": f"effort must be one of {', '.join(_EFFORTS)}",
                }

        if not isinstance(tasks, list):
            return {"ok": False, "error": "tasks must be a list of task objects"}
        if len(tasks) > subagent_settings.max_workers:
            return {
                "ok": False,
                "error": (f"tasks exceeds configured max_workers={subagent_settings.max_workers}"),
            }
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            return {"ok": False, "error": "max_concurrent must be an integer", **pool_metadata}
        if max_concurrent < 1:
            return {"ok": False, "error": "max_concurrent must be a positive integer"}
        if len(tasks) > task_ceiling:
            return {
                "ok": False,
                "status": "refused",
                "error": (
                    f"Subagent task limit exceeded at depth {pool_depth}: requested {len(tasks)}, "
                    f"limit {task_ceiling} from {task_limit_source}. Raise that setting to admit "
                    "more tasks."
                ),
                "tasks_requested": len(tasks),
                "task_limit_source": task_limit_source,
                **pool_metadata,
            }

        # Validate and convert task dicts into SubagentTask dataclasses.
        subagent_tasks: list[SubagentTask] = []
        for i, raw in enumerate(tasks):
            if not isinstance(raw, dict):
                return {
                    "ok": False,
                    "error": f"tasks[{i}] must be a dict with 'type' and 'task' keys",
                }
            raw_mapping: Mapping[str, object] = raw
            agent_type = str(raw_mapping.get("type") or raw_mapping.get("agent_type") or "").strip()
            task_desc = str(raw_mapping.get("task") or raw_mapping.get("task_description") or "")
            context = str(raw_mapping.get("context") or "")
            if not agent_type:
                return {"ok": False, "error": f"tasks[{i}] missing 'type' field"}
            if not task_desc:
                return {"ok": False, "error": f"tasks[{i}] missing 'task' field"}
            if agent_type not in _registry:
                known = ", ".join(_known_type_names(_registry))
                return {
                    "ok": False,
                    "error": f"Unknown agent type {agent_type!r}. Known types: {known}",
                }
            subagent_tasks.append(
                SubagentTask(
                    task_id=f"task-{i}",
                    agent_type=agent_type,
                    task_description=task_desc,
                    context=context,
                )
            )

        if not subagent_tasks:
            return {"ok": False, "error": "tasks list is empty", **pool_metadata}

        for task in subagent_tasks:
            signature = (task.agent_type, task.task_description)
            if signature in ancestry:
                chain = (*ancestry, signature)
                rendered_chain = " -> ".join(
                    f"{agent_type}: {description[:120]}" for agent_type, description in chain
                )
                return {
                    "ok": False,
                    "status": "refused",
                    "error": f"Repeated subagent task in ancestry: {rendered_chain}",
                    "ancestry": rendered_chain,
                    **pool_metadata,
                }

        pool_metadata["tasks_used"] = len(subagent_tasks)

        # PRD-124 Phase 4: check resume cache before spawning. The fingerprint
        # includes type, task, and context, while remaining order-insensitive.
        # Routing and permission changes invalidate cached worker results.
        effective_routing: dict[str, dict[str, object]] = {}
        type_policy_revisions: dict[str, str] = {}
        for agent_type in sorted({task.agent_type for task in subagent_tasks}):
            spec = _registry.get(agent_type)
            if spec is None:
                continue
            type_policy_revisions[agent_type] = _type_policy_revision(spec)
            worker_model = _worker_model(
                spec,
                parent_model=parent_model,
                default_model=default_model,
                call_model=call_model,
            )
            context_window = _worker_context_window(worker_model, execution_settings)
            requested_context = (
                spec.context
                if spec.context is not None
                else _settings.default_context
                if _settings.default_context is not None
                else 8_000
            )
            tier = resolve_depth_tier(_settings, pool_depth, agent_type)
            effective_routing[agent_type] = {
                "model": worker_model,
                "effort": _worker_effort(
                    spec,
                    default_effort=_settings.default_effort,
                    call_effort=call_effort,
                    provider_options=provider_options or {},
                ),
                "context": min(max(1, requested_context), context_window),
                "context_window": context_window,
                "output_schema": spec.output_schema,
                "allowed_tools": sorted(tier.allowed_tools or spec.allowed_tools),
                "allowed_models": sorted(tier.allowed_models or _allowed_models or ()),
            }
        routing_policy: dict[str, object] = {
            "provider": provider,
            "parent_model": parent_model,
            "default_model": default_model,
            "default_effort": _settings.default_effort,
            "default_context": _settings.default_context,
            "allowed_models": sorted(_allowed_models) if _allowed_models is not None else None,
            "allow_call_overrides": _allow_call_overrides,
            "call_model": call_model,
            "call_effort": call_effort,
            "depth": pool_depth,
            "max_depth": _settings.max_depth,
            "max_tasks": task_ceiling,
            "max_concurrent": effective_max_concurrent,
            "ancestry": ancestry,
            "types": effective_routing,
        }
        fp = _tasks_fingerprint(
            subagent_tasks,
            policy_revision=(effective_policy.policy_revision if effective_policy else ""),
            routing_policy=routing_policy,
            type_policy_revisions=type_policy_revisions,
        )
        expected_values = sum(
            1
            for task in subagent_tasks
            if (spec := _registry.get(task.agent_type)) is not None
            and spec.output_schema is not None
        )
        if conversation_journal is not None and parent_run_id:
            previous = conversation_journal.find_subagent_pool(parent_run_id, fp)
            if previous is not None:
                status = previous.get("status")
                if status in {"completed", "interrupted", "cancelled"}:
                    total_value = previous.get("total", len(subagent_tasks))
                    succeeded_value = previous.get("succeeded", 0)
                    failed_value = previous.get("failed", len(subagent_tasks))
                    total = total_value if isinstance(total_value, int) else len(subagent_tasks)
                    succeeded = succeeded_value if isinstance(succeeded_value, int) else 0
                    failed = failed_value if isinstance(failed_value, int) else len(subagent_tasks)
                    text = previous.get("text", "")
                    values = _validated_cached_values(previous.get("values"), _registry)
                    reason_value = previous.get("reason")
                    reason = reason_value if isinstance(reason_value, str) else ""
                    budget_exceeded_reason = reason if reason.startswith("max_") else ""
                    if status != "completed" or failed != 0 or len(values) == expected_values:
                        return {
                            "ok": status == "completed" and failed == 0,
                            "status": str(status),
                            "pool_id": str(previous.get("pool_id", "")),
                            "total": total,
                            "succeeded": succeeded,
                            "failed": failed,
                            "error": (
                                reason
                                if reason
                                else ""
                                if failed == 0
                                else f"{failed} subagent(s) failed"
                            ),
                            "budget_exceeded_reason": budget_exceeded_reason,
                            "results": text if isinstance(text, str) else "",
                            "values": values,
                            **pool_metadata,
                        }
                stored_receipt = previous.get("tool_result")
                if isinstance(stored_receipt, dict):
                    return dict(stored_receipt)
                return {
                    "ok": False,
                    "status": "in_progress",
                    "pool_id": str(previous.get("pool_id", "")),
                    "total": len(subagent_tasks),
                    "succeeded": 0,
                    "failed": 0,
                    "results": "This logical turn already started this subagent pool.",
                }

        cached = _find_cached_result(conv_store, fp, journal=conversation_journal)
        if cached is not None:
            cached_values = _find_cached_values(
                conv_store,
                fp,
                _registry,
                journal=conversation_journal,
            )
            if len(cached_values) != expected_values:
                cached = None
        if cached is not None:
            if conv_store is not None:
                conv_store.append_event(
                    "system",
                    {
                        "text": f"◈ Resumed: using cached subagent results ({len(subagent_tasks)} tasks)"
                    },
                )
            return {
                "ok": True,
                "pool_id": "cached",
                "total": len(subagent_tasks),
                "succeeded": len(subagent_tasks),
                "failed": 0,
                "error": "",
                "results": cached,
                "values": cached_values,
                **pool_metadata,
            }

        pool = SubagentPool(
            tasks=subagent_tasks,
            parent_runner=parent_runner,
            parent_model=parent_model,
            all_tools=all_tools,
            max_concurrent=effective_max_concurrent,
            app_state=app_state,
            processor=processor,
            conv_store=conv_store,
            registry=_registry,
            tool_registry=tool_registry,
            retry_config=retry_config,
            usage_ledger=usage_ledger,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            provider_options=provider_options,
            timeout_s=effective_timeout_s,
            approval_svc=approval_svc,
            workspace_access=workspace_access,
            conversation_journal=conversation_journal,
            task_fingerprint=fp,
            policy=effective_policy,
            provider=provider,
            default_model=default_model,
            default_effort=_settings.default_effort,
            default_context=_settings.default_context,
            call_model=call_model,
            call_effort=call_effort,
            execution_settings=execution_settings,
            allowed_models=_allowed_models,
            depth=pool_depth,
            parent_pool_id=parent_pool_id,
            max_depth=_settings.max_depth,
            max_tasks=task_ceiling,
            max_tasks_source=task_limit_source,
            max_active_pools=_settings.max_active_pools,
            ancestry=ancestry,
            subagent_settings=_settings,
            max_workers=_settings.max_workers,
            max_tool_calls=_settings.max_tool_calls,
            max_iterations=_settings.max_iterations,
            max_tokens=_settings.max_tokens,
            max_total_time_s=_settings.max_total_time_s,
            on_budget_exceeded=_settings.on_budget_exceeded,
            stop_grace_s=_settings.stop_grace_s,
        )
        from agenthicc.runners.agent_turn_context import current_background_task_owner

        task_owner = current_background_task_owner()
        if task_owner is None:
            pool_task = asyncio.create_task(pool.run(), name=f"subagent-pool-{pool.pool_id}")
        else:
            pool_task = task_owner.start(pool.run(), name=f"subagent-pool-{pool.pool_id}")
        pending_task: asyncio.Task[list[dict[str, object]]] | None = None
        background_task = asyncio.create_task(
            pool.wait_for_background_request(), name=f"subagent-background-{pool.pool_id}"
        )
        communication = pool.communication
        if communication is not None:
            pending_task = asyncio.create_task(communication.wait_for_pending())
        waiters: set[asyncio.Task[object]] = {
            cast(asyncio.Task[object], pool_task),
            cast(asyncio.Task[object], background_task),
        }
        if pending_task is not None:
            waiters.add(cast(asyncio.Task[object], pending_task))
        backgrounded = False
        try:
            done, _pending = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pool_task in done or pool_task.done():
                result = await pool_task
            elif background_task in done:
                background_receipt: dict[str, object] = {
                    "ok": True,
                    "status": "backgrounded",
                    "pool_id": pool.pool_id,
                    "total": len(subagent_tasks),
                    "succeeded": 0,
                    "failed": 0,
                    "error": "",
                    "results": (
                        f"Subagent pool {pool.pool_id} is running in the background. "
                        "Its result will be delivered as a user message on your next turn."
                    ),
                    **pool_metadata,
                }
                if conversation_journal is None:
                    pool_task.cancel()
                    await asyncio.gather(pool_task, return_exceptions=True)
                    return {
                        "ok": False,
                        "status": "background_unavailable",
                        "pool_id": pool.pool_id,
                        "total": len(subagent_tasks),
                        "succeeded": 0,
                        "failed": 0,
                        "error": "Background subagents require a durable conversation journal.",
                        "results": "",
                        **pool_metadata,
                    }
                conversation_journal.subagent_pool_backgrounded(
                    pool_id=pool.pool_id,
                    tool_result=background_receipt,
                )
                backgrounded = True
                if communication is not None:
                    continuation_registry.register(
                        PoolHandle(
                            pool_id=pool.pool_id,
                            conversation_id=conversation_id,
                            broker=communication,
                            task=pool_task,
                        )
                    )
                return background_receipt
            elif pending_task is not None and communication is not None and pending_task in done:
                questions = communication.pending_questions(recipient="main")
                continuation_registry.register(
                    PoolHandle(
                        pool_id=pool.pool_id,
                        conversation_id=conversation_id,
                        broker=communication,
                        task=pool_task,
                    )
                )
                return {
                    "ok": False,
                    "status": "awaiting_clarification",
                    "pool_id": pool.pool_id,
                    "pending_questions": questions,
                    "total": len(subagent_tasks),
                    "succeeded": 0,
                    "failed": 0,
                    "results": "A subagent needs clarification. Answer it with answer_subagent, then collect_subagent_results.",
                    **pool_metadata,
                }
            result = await pool_task
        except asyncio.CancelledError:
            if not backgrounded and not pool_task.done():
                pool_task.cancel()
                await asyncio.gather(pool_task, return_exceptions=True)
            raise
        except SubagentAdmissionError as exc:
            if not backgrounded and not pool_task.done():
                pool_task.cancel()
                await asyncio.gather(pool_task, return_exceptions=True)
            return {
                "ok": False,
                "status": "refused",
                "pool_id": pool.pool_id,
                "error": str(exc),
                "results": "",
                **pool_metadata,
            }
        except Exception:
            if not backgrounded and not pool_task.done():
                pool_task.cancel()
                await asyncio.gather(pool_task, return_exceptions=True)
            raise
        finally:
            for watcher in (background_task, pending_task):
                if watcher is not None and not watcher.done():
                    watcher.cancel()
            await asyncio.gather(
                *(watcher for watcher in (background_task, pending_task) if watcher is not None),
                return_exceptions=True,
            )

        structured_values: list[Mapping[str, object]] = [value for value in result.values]
        # Only a completely successful pool is a safe resume cache entry.
        # Caching a partial result would make a resumed call silently reuse a
        # failed/timeout outcome and report it as successful.
        if (
            conversation_journal is not None
            and result.failed == 0
            and not result.budget_exceeded_reason
        ):
            # This write is deliberately separate from the parent tool
            # exchange. The parent runner commits that exchange after this
            # callable returns; persisting here closes the cancellation window
            # and makes the result available to a resumed session.
            conversation_journal.subagent_pool_result(
                pool_id=result.pool_id,
                fingerprint=fp,
                total=result.total,
                succeeded=result.succeeded,
                failed=result.failed,
                text=result.text,
                values=structured_values,
            )
        if conv_store is not None and result.failed == 0 and not result.budget_exceeded_reason:
            conv_store.append_event(
                "subagent_pool_result",
                {
                    "fingerprint": fp,
                    "text": result.text,
                    "total": result.total,
                    "succeeded": result.succeeded,
                    "failed": result.failed,
                    "values": structured_values,
                },
            )

        budget_exceeded_reason = result.budget_exceeded_reason
        return {
            "ok": result.failed == 0 and not budget_exceeded_reason,
            "status": (
                "budget_exceeded"
                if budget_exceeded_reason
                else "completed"
                if result.failed == 0
                else "failed"
            ),
            "pool_id": result.pool_id,
            "total": result.total,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "error": (
                budget_exceeded_reason
                if budget_exceeded_reason
                else ""
                if result.failed == 0
                else f"{result.failed} subagent(s) failed"
            ),
            "budget_exceeded_reason": budget_exceeded_reason,
            "results": result.text,
            "values": structured_values,
            **pool_metadata,
        }

    _augment_type_schema(spawn_subagents, _registry, allow_call_overrides=_allow_call_overrides)
    # The turn runner registers these alongside spawn_subagents.  Attaching
    # them keeps this factory backwards compatible for lightweight callers that
    # only need the original callable.
    communication_tools = make_parent_communication_tools(continuation_registry, conversation_id)
    # The shared agent-turn runner already injects this factory into every
    # workflow. Attach the durable worktree worker alongside the existing
    # parent/subagent communication tools so custom workflows receive it too,
    # without creating a second injection path in the runner.
    if workspace_access is not None:
        from agenthicc.worktrees.agent_tool import (
            make_spawn_worker_agents_tool,
        )

        # A goal run has a product-level ID that is distinct from the current
        # provider turn ID. Resolve it from the durable main-session link so
        # every workflow-wide worker manifest is correlated with the same run,
        # including workers summoned by custom workflows. Ordinary sessions
        # intentionally receive an empty run ID and remain PRD-203 compatible.
        goal_run_id = ""
        if conversation_id:
            try:
                from agenthicc.runs import RunStore

                goal_run_id = next(
                    (
                        run.run_id
                        for run in RunStore().list()
                        if run.main_session_id == conversation_id
                    ),
                    "",
                )
            except (OSError, ValueError):
                goal_run_id = ""

        communication_tools.append(
            make_spawn_worker_agents_tool(
                repository=workspace_access.scope.primary_root,
                parent_session_id=conversation_id or parent_run_id,
                run_id=goal_run_id,
                max_parallel_tasks=max(1, default_max_concurrent),
                workspace_access=workspace_access,
            )
        )
    # Metadata on a tool function object; callables have no typed attributes.
    setattr(spawn_subagents, "__agenthicc_subagent_communication_tools__", communication_tools)
    return spawn_subagents


# ── resume helpers ────────────────────────────────────────────────────────────


def _type_policy_revision(spec: SubagentTypeSpec) -> str:
    """Fingerprint a registered type's effective and declared permissions."""
    policy_revision = spec.policy.policy_revision if spec.policy is not None else ""
    permissions = spec.permissions
    if permissions is None:
        return policy_revision
    permission_data: dict[str, object] = {
        "deny_tools": sorted(permissions.deny_tools),
        "deny_capabilities": sorted(permissions.deny_capabilities),
        "require_approval": sorted(permissions.require_approval),
        "workspace_paths": (
            sorted(permissions.workspace_paths) if permissions.workspace_paths is not None else None
        ),
    }
    payload = {"policy_revision": policy_revision, "permissions": permission_data}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode()
    ).hexdigest()[:24]


def _tasks_fingerprint(
    tasks: list[SubagentTask],
    policy_revision: str = "",
    routing_policy: Mapping[str, object] | None = None,
    type_policy_revisions: Mapping[str, str] | None = None,
) -> str:
    """Hash complete task inputs in an order-insensitive form.

    Context is part of a worker's effective prompt.  Ignoring it caused a
    resumed call with the same short task but different parent findings to
    reuse stale output.  The explicit object shape also makes future key
    additions less error-prone than positional tuples.
    """
    entries: list[dict[str, object]] = [
        {
            "type": agent_type,
            "task": task,
            "context": context,
            "type_policy_revision": (type_policy_revisions or {}).get(agent_type, ""),
        }
        for agent_type, task, context in sorted(
            (item.agent_type, item.task_description, item.context) for item in tasks
        )
    ]
    entries.append({"policy_revision": policy_revision})
    if routing_policy is not None:
        entries.append({"routing_policy": dict(routing_policy)})
    return hashlib.md5(
        json.dumps(entries, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


def _cached_pool_count(value: object) -> int | None:
    """Parse a persisted pool count without assuming its dynamic JSON type."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _find_cached_result(
    conv_store: "ConversationStore | None",
    fingerprint: str,
    *,
    journal: "ConversationJournal | None" = None,
) -> str | None:
    """Find a matching complete result in durable journal or UI projection.

    Returns the cached ``text`` string when found, or ``None``.
    This enables the resume path: when a session is restored and ``spawn_subagents``
    is called again with the same tasks, the previous result is reused instead of
    re-executing all workers.
    """
    if journal is not None:
        try:
            for record in reversed(journal.fold_subagent_pool_results()):
                if record.get("fingerprint") == fingerprint:
                    return str(record.get("text", ""))
        except (AttributeError, OSError, TypeError, ValueError):
            # A legacy/lightweight journal double may not expose the auxiliary
            # projection. Fall through to the reactive compatibility path.
            pass
    if conv_store is None:
        return None
    try:
        turn_list = conv_store.turns()
    except Exception:  # noqa: BLE001
        return None
    for turn in reversed(turn_list):
        for ev in reversed(turn.events):
            if ev.kind == "subagent_pool_result":
                payload = ev.payload
                if payload.get("fingerprint") == fingerprint:
                    # Pre-PRD-124 records did not include ``failed``.  For
                    # those records, the legacy succeeded/total pair is the
                    # next-best signal; records with an explicit failure are
                    # never eligible for replay.
                    if payload.get("failed", 0):
                        continue
                    total = payload.get("total")
                    succeeded = payload.get("succeeded")
                    if total is not None and succeeded is not None:
                        total_count = _cached_pool_count(total)
                        succeeded_count = _cached_pool_count(succeeded)
                        if (
                            total_count is None
                            or succeeded_count is None
                            or succeeded_count < total_count
                        ):
                            continue
                    return str(payload.get("text", ""))
    return None


def _validated_cached_values(
    raw_values: object,
    registry: SubagentTypeRegistry,
) -> list[dict[str, object]]:
    """Project and revalidate structured values against the active registry."""
    if not isinstance(raw_values, list):
        return []
    projected_values: list[dict[str, object]] = []
    for raw in raw_values:
        if not isinstance(raw, Mapping):
            return []
        agent_type = raw.get("agent_type")
        raw_value = raw.get("value")
        spec = registry.get(agent_type) if isinstance(agent_type, str) else None
        if spec is None or spec.output_schema is None or not isinstance(raw_value, Mapping):
            return []
        try:
            value = _redact_result_value(project_result_value(raw_value))
            validate_result_value(spec.output_schema, value)
        except (TypeError, ValueError):
            return []
        projected_values.append({**raw, "value": value})
    return projected_values


def _find_cached_values(
    conv_store: "ConversationStore | None",
    fingerprint: str,
    registry: SubagentTypeRegistry,
    *,
    journal: "ConversationJournal | None" = None,
) -> list[dict[str, object]]:
    """Load and revalidate structured values paired with a cached pool result."""
    candidates: list[object] = []
    if journal is not None:
        try:
            candidates.extend(reversed(journal.fold_subagent_pool_results()))
        except (AttributeError, OSError, TypeError, ValueError):
            pass
    if conv_store is not None:
        try:
            for turn in reversed(conv_store.turns()):
                for event in reversed(turn.events):
                    if event.kind == "subagent_pool_result":
                        candidates.append(event.payload)
        except Exception:  # noqa: BLE001
            pass

    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("fingerprint") != fingerprint:
            continue
        if candidate.get("failed", 0):
            continue
        values = _validated_cached_values(candidate.get("values", []), registry)
        if values:
            return values
        raw_values = candidate.get("values", [])
        if raw_values == []:
            return []
    return []


def _known_type_names(registry: SubagentTypeRegistry) -> list[str]:
    """Return the canonical names exposed by this session's registry."""
    return registry.names()


def _augment_type_schema(
    tool: object,
    registry: SubagentTypeRegistry,
    *,
    allow_call_overrides: bool = False,
) -> None:
    """Add this session's types and operator-enabled routing inputs to metadata."""

    try:
        metadata = object.__getattribute__(tool, "__lauren_ai_tool__")
    except AttributeError:
        return
    try:
        parameters = object.__getattribute__(metadata, "parameters")
    except AttributeError:
        return
    if not isinstance(parameters, dict):
        return
    input_schema = parameters.get("input_schema")
    if not isinstance(input_schema, dict):
        return
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return
    required = input_schema.get("required")
    if isinstance(required, list):
        input_schema["required"] = [name for name in required if name not in {"model", "effort"}]
    if not allow_call_overrides:
        properties.pop("model", None)
        properties.pop("effort", None)
    else:
        model_schema = properties.get("model")
        if isinstance(model_schema, dict):
            model_schema["description"] = (
                "Operator-enabled model override; must match the active provider and allow-list."
            )
        effort_schema = properties.get("effort")
        if isinstance(effort_schema, dict):
            effort_schema["enum"] = list(_EFFORTS)
            effort_schema["description"] = "Operator-enabled reasoning-effort override."
    tasks = properties.get("tasks")
    if not isinstance(tasks, dict):
        return
    item_schema = tasks.get("items")
    if not isinstance(item_schema, dict):
        return
    item_properties = item_schema.get("properties")
    if not isinstance(item_properties, dict):
        return
    # Lauren-ai's current TypedDict schema walker marks every annotated key as
    # required, even when the annotation is Optional.  ``context`` is a
    # supported optional input, so repair that generated boundary here rather
    # than forcing providers to send an empty context string.
    required = item_schema.get("required")
    if isinstance(required, list):
        item_schema["required"] = [name for name in required if name != "context"]
    type_schema = item_properties.get("type")
    if isinstance(type_schema, dict):
        type_schema["enum"] = _known_type_names(registry)

    descriptions = []
    for name in registry.names():
        spec = registry.get(name)
        if spec is not None:
            text = spec.description or "No description provided."
            descriptions.append(f"- {name}: {text}")
    try:
        raw_description = object.__getattribute__(metadata, "description")
    except AttributeError:
        raw_description = ""
    base_description = raw_description if isinstance(raw_description, str) else ""
    description = base_description
    if descriptions:
        description += "\n\nAvailable subagent types:\n" + "\n".join(descriptions)
    metadata.description = description
    parameters["description"] = description
