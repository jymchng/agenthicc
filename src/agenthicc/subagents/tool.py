"""spawn_subagents @tool() factory (PRD-124).

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
    """Structured task input accepted by ``spawn_subagents``."""

    type: BuiltinSubagentType
    task: str


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
) -> Callable[..., object]:
    """Return a ``spawn_subagents`` @tool()-decorated function.

    Closes over *parent_runner* and *parent_model* so the tool can build
    isolated subagent workers that share the parent's LLM transport.

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
        ``AppState`` used to build a ``ToolCapabilityGate`` per worker.
        ``None`` in headless / test contexts.
    processor:
        Kernel ``EventProcessor`` for emitting ``SubagentPool*`` events.
        ``None`` disables kernel event emission.
    conv_store:
        ``ConversationStore`` for appending a simple scroll-buffer summary.
        ``None`` disables scroll-buffer output.
    registry:
        ``SubagentTypeRegistry`` to look up type specs.  Defaults to
        ``DEFAULT_REGISTRY`` when ``None``.
    timeout_s:
        Default wall-clock timeout in seconds for each worker in this
        invocation. Defaults to one hour and may be overridden per call.
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

        # PRD-124 Phase 4: check resume cache before spawning.
        # The fingerprint is a hash of the sorted (type, task) pairs so the
        # same logical set of tasks — even if re-ordered — hits the cache.
        fp = _tasks_fingerprint(subagent_tasks)
        cached = _find_cached_result(conv_store, fp)
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
        )
        result = await pool.run()

        # Only a completely successful pool is a safe resume cache entry.
        # Caching a partial result would make a resumed call silently reuse a
        # failed/timeout outcome and report it as successful.
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
    """Return a short hash of the (type, task_description) pairs, order-insensitive."""
    pairs = sorted((t.agent_type, t.task_description) for t in tasks)
    return hashlib.md5(json.dumps(pairs, ensure_ascii=False).encode()).hexdigest()[:16]  # noqa: S324


def _find_cached_result(conv_store: "ConversationStore | None", fingerprint: str) -> str | None:
    """Scan conv_store turn events for a matching subagent_pool_result.

    Returns the cached ``text`` string when found, or ``None``.
    This enables the resume path: when a session is restored and ``spawn_subagents``
    is called again with the same tasks, the previous result is reused instead of
    re-executing all workers.
    """
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
