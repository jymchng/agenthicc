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

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal, TypedDict

from agenthicc.subagents.pool import SubagentTask
from agenthicc.subagents.types import SubagentTypeRegistry

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.tui.conversation_store import ConversationStore
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.plugins.registry import ToolRegistry
    from agenthicc.runners.retry import RetryConfig
    from agenthicc.runners.usage_ledger import UsageLedger
    from agenthicc.memory.journal import ConversationJournal
    from agenthicc.tools.approval import ApprovalService
    from agenthicc.tools.workspace_access import WorkspaceAccessPolicy

__all__ = ["make_spawn_subagents_tool"]


BuiltinSubagentType = Literal[
    "explorer",
    "planner",
    "implementer",
    "executor",
    "tester",
    "reviewer",
    "documenter",
    "verifier",
    "researcher",
]

# Models frequently shorten ``researcher`` to ``research``.  Keep the
# registry's canonical names stable while accepting this unambiguous,
# backwards-compatible spelling at the tool boundary.
_SUBAGENT_TYPE_ALIASES: dict[str, str] = {"research": "researcher"}


class _SpawnTaskInput(TypedDict):
    """Structured task input accepted by ``spawn_subagents``.

    Lauren-ai builds the provider JSON schema from this TypedDict.  Keeping
    ``context`` in the schema is important: the runtime has always accepted
    it, and it is part of the worker's prompt, so omitting it made the
    model-facing contract disagree with the actual validator.
    """

    type: BuiltinSubagentType
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
    max_concurrent: int = 4,
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
    approval_svc: "ApprovalService | None" = None,
    workspace_access: "WorkspaceAccessPolicy | None" = None,
    conversation_journal: "ConversationJournal | None" = None,
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
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.subagents.pool import (  # noqa: PLC0415
        SubagentPool,
        SubagentTask,
        _validate_timeout_s,
    )
    from agenthicc.subagents.types import DEFAULT_REGISTRY, DEFAULT_SUBAGENT_TIMEOUT_S  # noqa: PLC0415

    _registry = registry if registry is not None else DEFAULT_REGISTRY

    @_tool()
    async def spawn_subagents(
        tasks: list[_SpawnTaskInput],
        max_concurrent: int = max_concurrent,
        timeout_s: float = DEFAULT_SUBAGENT_TIMEOUT_S,
    ) -> dict[str, object]:
        """Spawn multiple specialized subagents concurrently and return their aggregated results.

        Each subagent runs in isolation with its own memory and a filtered tool set.
        Results are returned as a labelled plain-text digest you can reason over.

        Available agent types: explorer, planner, implementer, executor, tester,
        reviewer, documenter, verifier, researcher.

        Args:
            tasks: List of task objects. Each must have:
                   - type (str): Agent type name.
                   - task (str): Description of what this subagent should do.
                   - context (str, optional): Additional background context.
            max_concurrent: Maximum number of subagents running at once (default 4).
            timeout_s: Maximum wall-clock seconds for each worker in this call
                       (default 3600, or one hour).
        """
        try:
            effective_timeout_s = _validate_timeout_s(timeout_s)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        if not isinstance(tasks, list):
            return {"ok": False, "error": "tasks must be a list of task objects"}
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            return {"ok": False, "error": "max_concurrent must be an integer"}

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
            agent_type = _SUBAGENT_TYPE_ALIASES.get(agent_type, agent_type)
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
            return {"ok": False, "error": "tasks list is empty"}

        # PRD-124 Phase 4: check resume cache before spawning.  The fingerprint
        # includes type, task, and context, while remaining order-insensitive.
        # The same logical set of tasks — even if re-ordered — hits the cache.
        fp = _tasks_fingerprint(subagent_tasks)
        cached = _find_cached_result(conv_store, fp, journal=conversation_journal)
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
            }

        pool = SubagentPool(
            tasks=subagent_tasks,
            parent_runner=parent_runner,
            parent_model=parent_model,
            all_tools=all_tools,
            max_concurrent=max(1, max_concurrent),
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
        )
        result = await pool.run()

        # Only a completely successful pool is a safe resume cache entry.
        # Caching a partial result would make a resumed call silently reuse a
        # failed/timeout outcome and report it as successful.
        if conversation_journal is not None and result.failed == 0:
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
            )
        if conv_store is not None and result.failed == 0:
            conv_store.append_event(
                "subagent_pool_result",
                {
                    "fingerprint": fp,
                    "text": result.text,
                    "total": result.total,
                    "succeeded": result.succeeded,
                    "failed": result.failed,
                },
            )

        return {
            "ok": result.failed == 0,
            "pool_id": result.pool_id,
            "total": result.total,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "error": "" if result.failed == 0 else f"{result.failed} subagent(s) failed",
            "results": result.text,
        }

    _augment_type_schema(spawn_subagents, _registry)
    return spawn_subagents


# ── resume helpers ────────────────────────────────────────────────────────────


def _tasks_fingerprint(tasks: list[SubagentTask]) -> str:
    """Hash complete task inputs in an order-insensitive form.

    Context is part of a worker's effective prompt.  Ignoring it caused a
    resumed call with the same short task but different parent findings to
    reuse stale output.  The explicit object shape also makes future key
    additions less error-prone than positional tuples.
    """
    entries = [
        {"type": agent_type, "task": task, "context": context}
        for agent_type, task, context in sorted(
            (item.agent_type, item.task_description, item.context) for item in tasks
        )
    ]
    return hashlib.md5(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()[:16]  # noqa: S324


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
    turns = getattr(conv_store, "turns", None)
    if turns is None:
        return None
    try:
        turn_list = turns()
    except Exception:  # noqa: BLE001
        return None
    for turn in reversed(turn_list):
        events = getattr(turn, "events", [])
        for ev in reversed(events):
            if getattr(ev, "kind", "") == "subagent_pool_result":
                payload = getattr(ev, "payload", {})
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
                        try:
                            if int(succeeded) < int(total):
                                continue
                        except (TypeError, ValueError):
                            continue
                    return str(payload.get("text", ""))
    return None


def _known_type_names(registry: SubagentTypeRegistry) -> list[str]:
    """Return canonical registry names plus aliases that resolve in it."""

    names = registry.names()
    return [
        *names,
        *[
            alias
            for alias, canonical in _SUBAGENT_TYPE_ALIASES.items()
            if canonical in registry and alias not in names
        ],
    ]


def _augment_type_schema(tool: object, registry: SubagentTypeRegistry) -> None:
    """Add dynamic/plugin names to the schema generated from ``Literal``.

    ``Literal`` gives the static built-in contract to lauren-ai's schema
    generator.  Plugin registries are runtime data, so their names cannot be
    expressed in a static annotation; merge them into the generated enum
    after decoration without replacing the annotation-based schema.
    """

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
    if not isinstance(type_schema, dict):
        return
    enum = type_schema.get("enum")
    if not isinstance(enum, list):
        return
    known = _known_type_names(registry)
    type_schema["enum"] = [
        *[name for name in enum if name in known],
        *[name for name in known if name not in enum],
    ]
