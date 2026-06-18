"""Communication tool catalog for agents (PRD-03).

Every method on :class:`CommunicationTools` is a plain async callable —
deliberately *not* decorated with any tool framework — so the same
implementations can later be wrapped by lauren-ai's ``@tool()`` decorator
(or any other adapter) without changes.

All side-effects are expressed as events emitted on the kernel
:class:`~agenthicc.kernel.EventProcessor`. Methods never mutate
``AppState`` directly.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from agenthicc.kernel import AppState, Event, EventProcessor, NodeStatus

from .pool import AgentPool, AgentRecord

__all__ = ["CommunicationTools"]

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_TRANSPORTS = frozenset({"stdio", "ws", "websocket", "streamable", "http", "streamable_http"})


def _auto_name(url: str) -> str:
    """Generate a slug from a URL for use as MCP server name."""
    import re
    import urllib.parse  # noqa: PLC0415
    try:
        parsed = urllib.parse.urlparse(url)
        base = parsed.netloc or (url.split()[-1] if url.split() else "server")
        return re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:32] or "mcp-server"
    except Exception:  # noqa: BLE001
        return "mcp-server"


def _has_cycle(graph: dict[str, set[str]]) -> bool:
    """Iterative three-color DFS cycle check over ``node -> deps`` edges."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    for root in graph:
        if color[root] != WHITE:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                color[node] = BLACK
                continue
            if color.get(node, BLACK) == BLACK:
                continue
            if color.get(node) == GRAY:
                continue
            color[node] = GRAY
            stack.append((node, True))
            for dep in graph.get(node, ()):
                state = color.get(dep)
                if state == GRAY:
                    return True
                if state == WHITE:
                    stack.append((dep, False))
    return False


def _detect_cycle(graph: dict[str, set[str]]) -> bool:
    return _has_cycle(graph)


class CommunicationTools:
    """Built-in communication tool implementations.

    :param processor: The kernel event processor (sole write path).
    :param pool: Runtime agent pool; spawned agents are registered here.
    :param message_bus: Optional lauren-ai ``AgentMessageBus`` used for
        actual point-to-point delivery. When absent, messages are still
        recorded as ``AgentMessageSent`` events on the log.
    """

    def __init__(
        self,
        processor: EventProcessor,
        pool: AgentPool,
        message_bus: Any | None = None,
        mcp_registry: Any | None = None,
    ) -> None:
        self._processor = processor
        self._pool = pool
        self._bus = message_bus
        self._mcp_registry = mcp_registry

    # ── helpers ──────────────────────────────────────────────────────────

    async def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        source_agent_id: str | None = None,
    ) -> Event:
        event = Event.create(event_type, payload, source_agent_id=source_agent_id)
        await self._processor.emit(event)
        return event

    async def _fresh_state(self) -> AppState:
        """Settle the event queue so reads observe prior emissions."""
        await self._processor.drain()
        return self._processor.get_state()

    # ── lifecycle ────────────────────────────────────────────────────────

    async def agent_spawn(
        self,
        agent_type: str,
        config: dict[str, Any] | None = None,
        parent_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Spawn a new agent; returns the new ``agent_id``."""
        agent_id = uuid4().hex
        await self._emit(
            "AgentSpawnRequest",
            {
                "agent_id": agent_id,
                "agent_type": agent_type,
                "parent_agent_id": parent_agent_id,
                "config": dict(config or {}),
                "metadata": {},
            },
            source_agent_id=parent_agent_id,
        )
        self._pool.add(AgentRecord(agent_id=agent_id, agent_type=agent_type))
        return {"agent_id": agent_id, "agent_type": agent_type}

    # ── messaging ────────────────────────────────────────────────────────

    async def agent_send_message(
        self,
        to_agent_id: str,
        message: Any,
        from_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Send *message* to another agent.

        Delivery uses the message bus when one is configured; the
        ``AgentMessageSent`` event is always appended to the log for
        auditability.
        """
        payload = message if isinstance(message, dict) else {"text": str(message)}
        delivered = False
        message_id: str | None = None
        if self._bus is not None:
            from lauren_ai._messaging import (  # noqa: PLC0415
                AgentMessage,
                AgentMessageType,
            )

            envelope = AgentMessage(
                from_agent=from_agent_id or "system",
                to=to_agent_id,
                message_type=AgentMessageType.NOTIFICATION,
                payload=payload,
            )
            result = await self._bus.send(envelope)
            delivered = result.receiver_count > 0
            message_id = str(envelope.id)
        event = await self._emit(
            "AgentMessageSent",
            {
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
                "message": payload,
                "delivered": delivered,
            },
            source_agent_id=from_agent_id,
        )
        return {
            "message_id": message_id or event.event_id,
            "to_agent_id": to_agent_id,
            "delivered": delivered,
        }

    # ── tasks ────────────────────────────────────────────────────────────

    async def task_create(
        self,
        description: str,
        workflow_id: str,
        dependencies: list[str] | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a pending task plus its workflow node."""
        task_id = uuid4().hex
        node_id = node_id or uuid4().hex
        await self._emit(
            "WorkflowNodeAdded",
            {
                "workflow_id": workflow_id,
                "node_id": node_id,
                "task_id": task_id,
                "label": description,
                "dependencies": list(dependencies or []),
            },
        )
        await self._emit(
            "TaskCreated",
            {
                "task_id": task_id,
                "workflow_id": workflow_id,
                "node_id": node_id,
                "description": description,
            },
        )
        return {
            "task_id": task_id,
            "node_id": node_id,
            "workflow_id": workflow_id,
            "status": "pending",
        }

    async def task_assign(self, task_id: str, agent_id: str) -> dict[str, Any]:
        """Explicitly assign an existing task to an agent."""
        await self._emit(
            "TaskAssigned",
            {"task_id": task_id, "agent_id": agent_id},
        )
        return {"task_id": task_id, "agent_id": agent_id, "assigned": True}

    # ── workflow ─────────────────────────────────────────────────────────

    async def workflow_modify(
        self,
        workflow_id: str,
        action: str,
        node_id: str,
        label: str | None = None,
        dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add or remove a node in a workflow DAG.

        ``add_node`` is rejected if it would introduce a cycle;
        ``remove_node`` is rejected for running/complete nodes.
        """
        state = await self._fresh_state()
        workflow = state.workflows.get(workflow_id)
        if workflow is None:
            raise ValueError(f"unknown workflow {workflow_id!r}")

        if action == "add_node":
            deps = set(dependencies or [])
            graph: dict[str, set[str]] = {
                n.node_id: set(n.dependencies) for n in workflow.nodes.values()
            }
            graph[node_id] = deps
            if _detect_cycle(graph):
                raise ValueError(
                    f"adding node {node_id!r} would create a cycle in "
                    f"workflow {workflow_id!r}"
                )
            await self._emit(
                "WorkflowNodeAdded",
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "task_id": uuid4().hex,
                    "label": label or node_id,
                    "dependencies": sorted(deps),
                },
            )
        elif action == "remove_node":
            node = workflow.nodes.get(node_id)
            if node is None:
                raise ValueError(
                    f"node {node_id!r} not found in workflow {workflow_id!r}"
                )
            if node.status in (NodeStatus.running, NodeStatus.complete):
                raise ValueError(
                    f"cannot remove node {node_id!r}: status is {node.status.value}"
                )
            await self._emit(
                "WorkflowNodeRemoved",
                {"workflow_id": workflow_id, "node_id": node_id},
            )
        else:
            raise ValueError(f"unsupported workflow action {action!r}")

        return {
            "workflow_id": workflow_id,
            "action": action,
            "node_id": node_id,
            "applied": True,
        }

    # ── observability / UI ───────────────────────────────────────────────

    async def application_log(
        self,
        level: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a structured log entry to the event log."""
        level = level.upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"invalid log level {level!r}")
        event = await self._emit(
            "ApplicationLog",
            {"level": level, "message": message, "data": data or {}},
        )
        return {"log_id": event.event_id, "level": level, "accepted": True}

    async def application_ui_update(
        self,
        content: Any,
        ui_type: str = "message",
    ) -> dict[str, Any]:
        """Push a UI update onto the event log for the TUI to render."""
        event = await self._emit(
            "UIUpdate",
            {"ui_type": ui_type, "content": content},
        )
        return {"update_id": event.event_id, "ui_type": ui_type, "queued": True}

    # ── meta ─────────────────────────────────────────────────────────────

    async def tool_define(
        self,
        name: str,
        description: str,
        source_code: str,
        parameters_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Register a dynamically defined tool after a compile check."""
        if not name.isidentifier():
            raise ValueError(f"tool name {name!r} is not a valid identifier")
        try:
            compile(source_code, f"<tool:{name}>", "exec")
        except SyntaxError as exc:
            raise ValueError(
                f"tool {name!r} source code does not compile: {exc.msg} "
                f"(line {exc.lineno})"
            ) from exc
        tool_id = uuid4().hex
        await self._emit(
            "ToolRegistered",
            {
                "tool_id": tool_id,
                "name": name,
                "description": description,
                "parameters_schema": parameters_schema,
                "source_code": source_code,
                "is_builtin": False,
            },
        )
        return {"tool_id": tool_id, "name": name, "registered": True}

    async def mcp_connect(
        self,
        url: str,
        transport: str = "stdio",
        name: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Connect to an MCP server at runtime and register its tools.

        Args:
            url: Command (stdio) or server URL (ws/streamable).
            transport: One of stdio, ws, streamable.
            name: Optional server slug; auto-generated from URL if omitted.
            token: Bearer token for authenticated servers.
        """
        transport_key = transport.lower()
        if transport_key not in _VALID_TRANSPORTS:
            return {"ok": False, "error": f"Unknown transport {transport!r}. Use: {sorted(_VALID_TRANSPORTS)}"}
        if self._mcp_registry is None:
            return {"ok": False, "error": "McpToolRegistry not available in this session"}
        server_name = name or _auto_name(url)
        try:
            from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415
            cfg = McpServerConfig(name=server_name, url=url, transport=transport_key, token=token or "", auto_connect=True)
            self._mcp_registry.register_server(cfg)
            tools = await self._mcp_registry.connect_server(server_name)
            return {"ok": True, "server_name": server_name, "tool_count": len(tools), "tools": [t.name for t in tools]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    async def hook_register(
        self,
        entity_type: str,
        stage: str,
        handler_dotpath: str,
    ) -> dict[str, Any]:
        """Register a lifecycle hook handler by dotted path."""
        hook_id = uuid4().hex
        await self._emit(
            "HookRegistered",
            {
                "hook_id": hook_id,
                "entity_type": entity_type,
                "stage": stage,
                "handler_dotpath": handler_dotpath,
            },
        )
        return {
            "hook_id": hook_id,
            "entity_type": entity_type,
            "stage": stage,
            "registered": True,
        }
