"""spawn_subagents @tool() factory (PRD-124).

NOTE: intentionally no ``from __future__ import annotations`` — @tool()
inspects type annotations at decoration time using ``get_type_hints()``.
Postponed evaluation (PEP 563) breaks that inspection.
"""
import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from agenthicc.subagents.pool import SubagentTask
from agenthicc.subagents.types import SubagentTypeRegistry

if TYPE_CHECKING:
    from lauren_ai._agents._runner import AgentRunnerBase
    from agenthicc.kernel.processor import EventProcessor
    from agenthicc.tui.conversation_store import ConversationStore
    from agenthicc.tui.conversation_store import AppState

__all__ = ["make_spawn_subagents_tool"]


def make_spawn_subagents_tool(
    parent_runner:  "AgentRunnerBase",
    parent_model:   str,
    all_tools:      list[object],
    max_concurrent: int = 4,
    app_state:      "AppState | None" = None,
    processor:      "EventProcessor | None" = None,
    conv_store:     "ConversationStore | None" = None,
    registry:       SubagentTypeRegistry | None = None,
    tool_registry:  object = None,
) -> object:
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
    """
    from lauren_ai._tools import tool as _tool  # noqa: PLC0415
    from agenthicc.subagents.pool import SubagentPool, SubagentTask  # noqa: PLC0415
    from agenthicc.subagents.types import DEFAULT_REGISTRY  # noqa: PLC0415

    _registry = registry if registry is not None else DEFAULT_REGISTRY

    @_tool()
    async def spawn_subagents(tasks: list[dict], max_concurrent: int = max_concurrent) -> dict:
        """Spawn multiple specialized subagents concurrently and return their aggregated results.

        Each subagent runs in isolation with its own memory and a filtered tool set.
        Results are returned as a labelled plain-text digest you can reason over.

        Available agent types: explorer, planner, implementer, tester, reviewer,
        documenter, verifier, researcher.

        Args:
            tasks: List of task objects. Each must have:
                   - type (str): Agent type name.
                   - task (str): Description of what this subagent should do.
                   - context (str, optional): Additional background context.
            max_concurrent: Maximum number of subagents running at once (default 4).
        """
        # Validate and convert task dicts into SubagentTask dataclasses.
        subagent_tasks: list[SubagentTask] = []
        for i, raw in enumerate(tasks):
            if not isinstance(raw, dict):
                return {
                    "ok": False,
                    "error": f"tasks[{i}] must be a dict with 'type' and 'task' keys",
                }
            agent_type = str(raw.get("type") or raw.get("agent_type") or "")
            task_desc  = str(raw.get("task") or raw.get("task_description") or "")
            context    = str(raw.get("context") or "")
            if not agent_type:
                return {"ok": False, "error": f"tasks[{i}] missing 'type' field"}
            if not task_desc:
                return {"ok": False, "error": f"tasks[{i}] missing 'task' field"}
            if agent_type not in _registry:
                known = ", ".join(_registry.names())
                return {
                    "ok": False,
                    "error": f"Unknown agent type {agent_type!r}. Known types: {known}",
                }
            subagent_tasks.append(SubagentTask(
                task_id=f"task-{i}",
                agent_type=agent_type,
                task_description=task_desc,
                context=context,
            ))

        if not subagent_tasks:
            return {"ok": False, "error": "tasks list is empty"}

        # PRD-124 Phase 4: check resume cache before spawning.
        # The fingerprint is a hash of the sorted (type, task) pairs so the
        # same logical set of tasks — even if re-ordered — hits the cache.
        fp = _tasks_fingerprint(subagent_tasks)
        cached = _find_cached_result(conv_store, fp)
        if cached is not None:
            if conv_store is not None:
                conv_store.append_event("system", {
                    "text": f"◈ Resumed: using cached subagent results ({len(subagent_tasks)} tasks)"
                })
            return {
                "ok":        True,
                "pool_id":   "cached",
                "total":     len(subagent_tasks),
                "succeeded": len(subagent_tasks),
                "failed":    0,
                "results":   cached,
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
        )
        result = await pool.run()

        # Persist result for resume.
        if conv_store is not None:
            conv_store.append_event("subagent_pool_result", {
                "fingerprint": fp,
                "text":        result.text,
                "total":       result.total,
                "succeeded":   result.succeeded,
            })

        return {
            "ok":        True,
            "pool_id":   result.pool_id,
            "total":     result.total,
            "succeeded": result.succeeded,
            "failed":    result.failed,
            "results":   result.text,
        }

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
                    return str(payload.get("text", ""))
    return None
