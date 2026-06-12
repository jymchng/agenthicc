---
title: "MCP Integration"
status: draft
version: 0.1.0
created: 2025-01-01
authors:
  - platform-ai-team
reviewers:
  - backend-lead
  - infra-lead
related_prds:
  - PRD-01  # Application State and Event Bus
  - PRD-03  # Agent Runtime and Communication Tools
  - PRD-04  # Tool Execution and Hooks
  - PRD-07  # Configuration and Security
supersedes: []
tags:
  - mcp
  - tools
  - integration
  - external-services
---

# PRD-12: MCP Integration

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Architecture and Design](#3-architecture-and-design)
4. [Data Structures and Interfaces](#4-data-structures-and-interfaces)
5. [McpToolRegistry Lifecycle](#5-mcptoolregistry-lifecycle)
6. [mcp_connect Comm Tool](#6-mcp_connect-comm-tool)
7. [Permission Enforcement](#7-permission-enforcement)
8. [Configuration Reference](#8-configuration-reference)
9. [Implementation Plan](#9-implementation-plan)
10. [Tests](#10-tests)
11. [Open Questions](#11-open-questions)

---

## 1. Executive Summary

PRD-12 specifies the **MCP (Model Context Protocol) Integration Layer** for agenthicc. Agents currently have access only to the built-in tool catalog (the twelve comm tools defined in PRD-03 and any dynamically compiled tools from `tool_define`). MCP servers expose rich external capabilities — databases, code-analysis engines, search engines, web APIs, file systems — as typed, schema-documented tool endpoints. This PRD defines how agenthicc discovers, bridges, and invokes those tools as first-class citizens of the `AppState.tools` map.

The design is grounded in three existing contracts:

- The `Tool` ABC (`tools/base.py`) — every invocable tool is a subclass with `name`, `description`, `parameters`, and an async `execute()`.
- The `ToolRegistration` dataclass (`kernel/state.py`) — the immutable record that `AppState.tools[name]` holds.
- The `AgenthiccToolExecutor` (`tools/executor.py`) — the single execution pipeline through which every tool call flows (permission check, before-hooks, timeout, after-hooks, error-hooks, event emission).

MCP tools enter the system through a new `McpToolBridge` that wraps a `lauren_ai.mcp` client connection and converts each discovered remote tool into an `AgenthiccMcpTool`. Once registered, MCP tools are indistinguishable from built-in tools at the executor layer. They go through `PermissionChecker`, `HookRunner`, and the full event bus pipeline.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal |
|---|------|
| G-01 | Let agents invoke tools from any MCP server as if they were built-in agenthicc tools |
| G-02 | Discover MCP servers from TOML config at startup; register tools into `AppState` before the first agent run |
| G-03 | Support stdio, WebSocket, and Streamable HTTP transports |
| G-04 | Pass MCP tool JSON schemas verbatim as `Tool.parameters` without translation |
| G-05 | Enforce the existing `PermissionChecker` pattern for MCP tools using the `mcp:{server}:{tool}` pattern |
| G-06 | Thread `tool_call_id` through to MCP client calls so traces are end-to-end correlated |
| G-07 | Allow agents to connect to new MCP servers at runtime via a `mcp_connect` comm tool |
| G-08 | Run MCP tool calls through `LifecycleHook` (on_before / on_after / on_error) exactly as local tools do |
| G-09 | Emit `ToolRegistered` events for each discovered tool so reducers can update `AppState.tools` |
| G-10 | Reconnect to MCP servers automatically on transient connection failures |

### 2.2 Non-Goals

| # | Non-Goal |
|---|----------|
| NG-01 | Exposing agenthicc tools as an MCP server (agent-as-server direction; separate PRD) |
| NG-02 | MCP resource or prompt primitives (tools only in v1) |
| NG-03 | Distributed MCP server registry or service mesh (single-process asyncio model) |
| NG-04 | LLM provider abstraction or prompt engineering changes |
| NG-05 | Sandboxing the network calls made by MCP tools (MCP server is trusted; firewall at the infra layer) |

---

## 3. Architecture and Design

### 3.1 High-Level Component Diagram

```
+-----------------------------------------------------------------------+
|                          AGENTHICC KERNEL                             |
|                                                                       |
|  AppState.tools: dict[str, ToolRegistration]                          |
|    "mcp:filesystem:read_file" -> ToolRegistration(...)                |
|    "mcp:github:search_code"   -> ToolRegistration(...)                |
|    "agent_spawn"              -> ToolRegistration(is_builtin=True)    |
|                                                                       |
|  EventProcessor                                                       |
|    ToolRegistered -> root_reducer -> AppState.with_tool(...)          |
|    ToolCallStarted / ToolCallComplete  (from AgenthiccToolExecutor)   |
+-----------------------------------------------------------------------+
          ^                              |
          |  ToolRegistered events       | execute(AgenthiccMcpTool, args, ctx)
          |                              v
+-----------------------------------------------------------------------+
|                        MCP INTEGRATION LAYER                          |
|                                                                       |
|  McpToolRegistry (singleton)                                          |
|    register_server(McpServerConfig)                                   |
|    discover_all()  -> emits ToolRegistered for each tool              |
|    get_tool(name)  -> AgenthiccMcpTool | None                         |
|    all_tools()     -> list[AgenthiccMcpTool]                          |
|    reconnect(server_name)                                             |
|                                                                       |
|  McpToolBridge (one per MCP server)                                   |
|    connect()  -> establishes transport (stdio / ws / streamable)      |
|    disconnect()                                                       |
|    list_tools() -> list[McpToolSchema]                                |
|    call_tool(name, args, tool_call_id) -> dict                        |
|                                                                       |
|  AgenthiccMcpTool(Tool)  (one per remote MCP tool)                    |
|    name: "mcp:{server_name}:{tool_name}"                              |
|    description: forwarded verbatim                                    |
|    parameters: MCP inputSchema forwarded verbatim                     |
|    execute(args, ctx) -> calls bridge.call_tool(...)                  |
+-----------------------------------------------------------------------+
          |
          | JSON-RPC / stdio / WebSocket / Streamable HTTP
          v
+-----------------------------------------------------------------------+
|                       EXTERNAL MCP SERVERS                            |
|  filesystem  (npx @modelcontextprotocol/server-filesystem /workspace) |
|  github      (wss://github-mcp.example.com)                           |
|  custom      (https://mcp.internal/tools)                             |
+-----------------------------------------------------------------------+
```

### 3.2 Tool Name Namespace

MCP tools are registered under the compound key `mcp:{server_name}:{tool_name}`. This:

- Avoids collisions with built-in tools (which use plain snake_case names).
- Makes the source server explicit at a glance in logs and permission rules.
- Mirrors the `{alias}__{name}` pattern used by `lauren-ai`'s `DynamicMcpBridge`.

The `ToolRegistration.name` and `AgenthiccMcpTool.name` both carry the full compound key.

### 3.3 Execution Path for an MCP Tool Call

```
Agent reasoning loop selects tool "mcp:filesystem:read_file"
          |
          v
AgenthiccToolExecutor.execute(tool, args, ctx)
          |
          +-- 1. PermissionChecker("mcp:filesystem:read_file", args, ctx)
          |       pattern matching against SecurityPolicy.permission_rules
          |
          +-- 2. EventProcessor.emit(ToolCallStarted{tool_call_id=...})
          |
          +-- 3. HookRunner.run_before("tool", tool, ctx)
          |       -> first Rejection aborts the call
          |
          +-- 4. asyncio.wait_for(
          |         AgenthiccMcpTool.execute(args, ctx),
          |         timeout=30.0
          |      )
          |         |
          |         v
          |      McpToolBridge.call_tool(
          |         "read_file", args,
          |         tool_call_id=ctx["tool_call_id"]
          |      )
          |         |
          |         v
          |      lauren_ai.mcp.McpServer.call_tool(name, args)
          |         |
          |         v
          |      MCP server process / remote endpoint
          |
          +-- 5. HookRunner.run_after("tool", tool, result, ctx)
          |
          +-- 6. EventProcessor.emit(ToolCallComplete{...})
          |
          v
     ToolResultEnvelope(tool_call_id, tool_name, ok, value, duration_ms)
```

### 3.4 Startup Sequence

```
AgenthiccApp.__init__
  |
  +-> load_config() -> [McpServerConfig, ...]
  |
  +-> McpToolRegistry.register_server(config) for each server
  |
  +-> McpToolRegistry.discover_all()
        |
        for each server in _servers:
          |
          +-> McpToolBridge.connect()
          |     (transport-specific: subprocess for stdio,
          |      asyncio WebSocket for ws, aiohttp for streamable)
          |
          +-> McpToolBridge.list_tools()
          |     -> [McpToolSchema(name, description, inputSchema), ...]
          |
          +-> for each tool_schema:
                AgenthiccMcpTool(bridge, tool_schema) constructed
                EventProcessor.emit(ToolRegistered{
                    name="mcp:{server}:{tool}",
                    description=tool_schema.description,
                    parameters_schema=tool_schema.inputSchema,
                    is_builtin=False,
                    source_agent_id=None,
                })
                -> root_reducer -> AppState.with_tool(ToolRegistration(...))
```

---

## 4. Data Structures and Interfaces

### 4.1 McpServerConfig

```python
# src/agenthicc/tools/mcp.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class McpServerConfig:
    """Static configuration for one MCP server connection.

    Mirrors the ``[[tools.mcp_servers]]`` TOML table (Section 8).

    :param name: Stable slug used as the middle segment of tool names
        (``mcp:{name}:{tool}``).  Must be a valid Python identifier.
    :param url: Transport endpoint.  For stdio, this is the full shell
        command (e.g. ``"npx -y @modelcontextprotocol/server-filesystem
        /workspace"``).  For ws/streamable, it is a URL.
    :param transport: One of ``"stdio"``, ``"ws"``, or ``"streamable"``.
    :param token: Optional bearer token for authenticated servers.
        Resolved from environment variables when the value starts with
        ``"${"`` (e.g. ``"${GITHUB_MCP_TOKEN}"``).
    :param auto_connect: When True (default), the bridge connects during
        ``McpToolRegistry.discover_all()``.  Set False to defer until the
        first explicit ``mcp_connect`` comm tool call.
    :param reconnect_attempts: Maximum number of reconnection attempts on
        connection loss before the bridge is marked as failed.
    :param reconnect_delay_seconds: Base delay between reconnection
        attempts (exponential back-off applies).
    """

    name: str
    url: str
    transport: Literal["stdio", "ws", "streamable"] = "stdio"
    token: str | None = None
    auto_connect: bool = True
    reconnect_attempts: int = 3
    reconnect_delay_seconds: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 4.2 AgenthiccMcpTool

```python
# src/agenthicc/tools/mcp.py (continued)

from agenthicc.tools.base import Tool


@dataclass
class McpToolSchema:
    """Minimal projection of the MCP tool descriptor returned by list_tools."""

    name: str
    description: str
    input_schema: dict[str, Any]  # raw MCP inputSchema, forwarded verbatim


class AgenthiccMcpTool(Tool):
    """A Tool subclass that delegates execution to a remote MCP server.

    One instance is created per tool per MCP server.  The ``name``
    attribute uses the compound key ``mcp:{server_name}:{tool_name}``
    so the tool is unambiguous across the ``AppState.tools`` map.

    :param bridge: The :class:`McpToolBridge` managing the server
        connection.  The bridge is shared across all tools on the same
        server.
    :param schema: Tool descriptor as returned by
        :meth:`McpToolBridge.list_tools`.
    """

    def __init__(self, bridge: "McpToolBridge", schema: McpToolSchema) -> None:
        self._bridge = bridge
        self._schema = schema
        self.name = f"mcp:{bridge.server_name}:{schema.name}"
        self.description = schema.description
        self.parameters = schema.input_schema  # forwarded verbatim

    async def execute(
        self, args: dict[str, Any], context: dict[str, Any]
    ) -> Any:
        """Call the remote MCP tool.

        :param args: Argument dict validated against ``self.parameters``
            by the executor before this method is called.
        :param context: Executor-injected context dict.  The key
            ``"tool_call_id"`` is extracted and threaded through to the
            bridge call for end-to-end trace correlation.
        :return: JSON-serialisable value returned by the MCP server.
        :raises McpToolCallError: When the MCP server returns an error
            response or the transport layer raises an exception.
        """
        tool_call_id: str = context.get("tool_call_id", "")
        raw_result = await self._bridge.call_tool(
            self._schema.name, args, tool_call_id=tool_call_id
        )
        return raw_result
```

### 4.3 McpToolBridge

```python
# src/agenthicc/tools/mcp.py (continued)

import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)


class McpToolCallError(RuntimeError):
    """Raised when an MCP tool call returns an error payload."""


class McpToolBridge:
    """Manages the lifecycle of a single MCP server connection.

    Wraps a ``lauren_ai.mcp.McpServer`` (or equivalent client object)
    and converts the connection into agenthicc-native data structures.

    :param config: Server configuration.
    :param event_processor: Kernel event processor used only for
        connection-state events (``McpServerConnected`` /
        ``McpServerDisconnected``).  May be None in unit tests.
    """

    def __init__(
        self,
        config: McpServerConfig,
        event_processor: Any | None = None,
    ) -> None:
        self._config = config
        self._events = event_processor
        self._client: Any | None = None  # lazy; set by connect()
        self._lock = asyncio.Lock()

    @property
    def server_name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        """Establish the transport connection to the MCP server.

        For ``stdio``: launches a subprocess and wraps it in a
        ``lauren_ai.mcp.McpServer`` using the stdio transport.
        For ``ws``: opens an asyncio WebSocket connection.
        For ``streamable``: connects via HTTP Streamable transport.

        Raises ``ConnectionError`` after ``config.reconnect_attempts``
        failed attempts.
        """
        async with self._lock:
            if self._client is not None:
                return
            resolved_url = self._resolve_env_vars(self._config.url)
            token = self._resolve_env_vars(self._config.token or "")

            for attempt in range(1, self._config.reconnect_attempts + 1):
                try:
                    self._client = await self._build_client(
                        resolved_url, self._config.transport, token or None
                    )
                    logger.info(
                        "McpToolBridge: connected to %r (transport=%s, attempt=%d)",
                        self._config.name,
                        self._config.transport,
                        attempt,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    if attempt == self._config.reconnect_attempts:
                        raise ConnectionError(
                            f"McpToolBridge: failed to connect to "
                            f"{self._config.name!r} after {attempt} attempt(s): {exc}"
                        ) from exc
                    delay = self._config.reconnect_delay_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "McpToolBridge: connection attempt %d/%d for %r failed "
                        "(%s); retrying in %.1fs",
                        attempt,
                        self._config.reconnect_attempts,
                        self._config.name,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

    async def disconnect(self) -> None:
        """Close the transport connection."""
        async with self._lock:
            if self._client is None:
                return
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._client = None
                logger.info(
                    "McpToolBridge: disconnected from %r", self._config.name
                )

    async def list_tools(self) -> list[McpToolSchema]:
        """Fetch the tool catalogue from the MCP server.

        :returns: List of :class:`McpToolSchema` objects, one per tool
            advertised by the server.
        :raises RuntimeError: If the bridge is not connected.
        """
        if self._client is None:
            raise RuntimeError(
                f"McpToolBridge: not connected to {self._config.name!r}; "
                "call connect() first"
            )
        raw = await self._client.list_tools()
        return [
            McpToolSchema(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
            )
            for t in raw
        ]

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_call_id: str = "",
    ) -> Any:
        """Invoke a single tool on the remote MCP server.

        :param tool_name: Bare tool name (no server prefix).
        :param args: Arguments dict validated by the executor.
        :param tool_call_id: Trace ID threaded from the executor; passed
            as metadata in the MCP request where the transport supports it.
        :returns: The ``content`` field of the MCP ``CallToolResult``.
        :raises McpToolCallError: On MCP-level error responses.
        :raises RuntimeError: If the bridge is not connected.
        """
        if self._client is None:
            raise RuntimeError(
                f"McpToolBridge: not connected to {self._config.name!r}"
            )
        try:
            result = await self._client.call_tool(tool_name, args)
        except Exception as exc:
            raise McpToolCallError(
                f"MCP call failed: {self._config.name}/{tool_name}: {exc}"
            ) from exc

        if getattr(result, "isError", False):
            error_text = _extract_text_content(result)
            raise McpToolCallError(
                f"MCP server returned error for "
                f"{self._config.name}/{tool_name}: {error_text}"
            )
        return _extract_tool_content(result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_env_vars(value: str) -> str:
        """Expand ``${VAR_NAME}`` references from the environment."""
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )

    @staticmethod
    async def _build_client(
        url: str, transport: str, token: str | None
    ) -> Any:
        """Construct a connected lauren_ai.mcp client."""
        from lauren_ai.mcp import McpServer  # noqa: PLC0415

        if transport == "stdio":
            return await McpServer.stdio(url)
        elif transport == "ws":
            return await McpServer.websocket(url, token=token)
        elif transport == "streamable":
            return await McpServer.streamable_http(url, token=token)
        else:
            raise ValueError(f"Unknown MCP transport: {transport!r}")


def _extract_tool_content(result: Any) -> Any:
    """Pull the usable payload out of an MCP CallToolResult."""
    content = getattr(result, "content", result)
    if isinstance(content, list):
        texts = [
            c.text for c in content
            if hasattr(c, "text") and c.text is not None
        ]
        return "\n".join(texts) if texts else content
    return content


def _extract_text_content(result: Any) -> str:
    content = getattr(result, "content", [])
    if isinstance(content, list):
        return " ".join(
            c.text for c in content if hasattr(c, "text") and c.text
        )
    return str(content)
```

### 4.4 McpToolRegistry

```python
# src/agenthicc/tools/mcp.py (continued)

from agenthicc.kernel import Event, EventProcessor


class McpToolRegistry:
    """Singleton that manages all MCP server connections and their tools.

    Usage (at application startup)::

        registry = McpToolRegistry(event_processor)
        for cfg in app_config.mcp_servers:
            registry.register_server(cfg)
        await registry.discover_all()

    After ``discover_all()``, every discovered tool is present in
    ``AppState.tools`` as a :class:`ToolRegistration` and accessible
    to agents through the normal executor path.

    :param event_processor: Kernel event processor.  Every discovered
        tool emits a ``ToolRegistered`` event so the reducer can update
        ``AppState.tools``.
    """

    def __init__(self, event_processor: EventProcessor) -> None:
        self._events = event_processor
        self._servers: dict[str, McpToolBridge] = {}
        self._tools: dict[str, AgenthiccMcpTool] = {}

    def register_server(self, config: McpServerConfig) -> None:
        """Register a server config; does NOT connect yet."""
        if config.name in self._servers:
            raise ValueError(
                f"McpToolRegistry: server {config.name!r} is already registered"
            )
        bridge = McpToolBridge(config, event_processor=self._events)
        self._servers[config.name] = bridge

    async def discover_all(self) -> None:
        """Connect to all auto_connect servers and register their tools."""
        for name, bridge in self._servers.items():
            cfg = bridge._config
            if not cfg.auto_connect:
                continue
            try:
                await bridge.connect()
                await self._register_tools_from_bridge(bridge)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "McpToolRegistry: discovery failed for %r: %s", name, exc
                )

    async def connect_server(self, server_name: str) -> list[AgenthiccMcpTool]:
        """Connect a deferred (auto_connect=False) server on demand.

        Called by the ``mcp_connect`` comm tool.  Returns the list of
        newly registered tools.
        """
        bridge = self._servers.get(server_name)
        if bridge is None:
            raise KeyError(
                f"McpToolRegistry: unknown server {server_name!r}"
            )
        await bridge.connect()
        return await self._register_tools_from_bridge(bridge)

    def get_tool(self, name: str) -> AgenthiccMcpTool | None:
        """Look up a tool by its full compound name ``mcp:{server}:{tool}``."""
        return self._tools.get(name)

    def all_tools(self) -> list[AgenthiccMcpTool]:
        return list(self._tools.values())

    async def shutdown(self) -> None:
        """Disconnect all bridges gracefully."""
        for bridge in self._servers.values():
            await bridge.disconnect()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _register_tools_from_bridge(
        self, bridge: McpToolBridge
    ) -> list[AgenthiccMcpTool]:
        schemas = await bridge.list_tools()
        registered: list[AgenthiccMcpTool] = []
        for schema in schemas:
            tool = AgenthiccMcpTool(bridge, schema)
            self._tools[tool.name] = tool
            await self._emit_tool_registered(tool)
            registered.append(tool)
        logger.info(
            "McpToolRegistry: registered %d tool(s) from %r",
            len(registered),
            bridge.server_name,
        )
        return registered

    async def _emit_tool_registered(self, tool: AgenthiccMcpTool) -> None:
        from uuid import uuid4  # noqa: PLC0415

        await self._events.emit(
            Event.create(
                "ToolRegistered",
                {
                    "tool_id": uuid4().hex,
                    "name": tool.name,
                    "description": tool.description,
                    "parameters_schema": tool.parameters,
                    "is_builtin": False,
                    "source_code": None,
                },
            )
        )
```

---

## 5. McpToolRegistry Lifecycle

### 5.1 Startup

```
1. Config loaded from agenthicc.toml [[tools.mcp_servers]] tables.
2. For each entry, AgenthiccConfig.mcp_servers yields a McpServerConfig.
3. McpToolRegistry.register_server(config) called for each.
4. McpToolRegistry.discover_all() called once before the event loop
   starts accepting agent tasks.
5. Each auto_connect=True bridge:
     a. McpToolBridge.connect() establishes transport.
     b. McpToolBridge.list_tools() fetches the catalogue.
     c. One AgenthiccMcpTool per tool is created and stored in
        McpToolRegistry._tools.
     d. ToolRegistered event emitted; root_reducer adds ToolRegistration
        to AppState.tools.
6. Agents can now invoke any registered MCP tool by name.
```

### 5.2 Reconnection

When `McpToolBridge.call_tool()` raises a transport-level exception (connection reset, timeout), the bridge attempts to reconnect before returning an error to the executor. The retry loop:

```python
# McpToolBridge.call_tool reconnect path (pseudocode):
for attempt in range(config.reconnect_attempts):
    try:
        result = await self._client.call_tool(tool_name, args)
        return _extract_tool_content(result)
    except (ConnectionResetError, TimeoutError) as exc:
        if attempt == config.reconnect_attempts - 1:
            raise McpToolCallError(f"reconnect exhausted: {exc}") from exc
        self._client = None
        await self.connect()
```

The `asyncio.Lock` in `connect()` prevents concurrent goroutines from attempting to reconnect simultaneously. A failed reconnect surfaces as a `McpToolCallError`, which the `AgenthiccToolExecutor` catches and converts into a `ToolResultEnvelope(ok=False, error=...)`.

### 5.3 Shutdown

`McpToolRegistry.shutdown()` is registered as an `atexit` handler and also called by the application shutdown hook. Each bridge calls `McpToolBridge.disconnect()`, which closes the underlying subprocess or WebSocket connection gracefully.

---

## 6. mcp_connect Comm Tool

`mcp_connect` is a new method on `CommunicationTools` (`runtime/comm_tools.py`). It allows agents to add MCP server connections at runtime without a restart.

### 6.1 Signature

```python
# Addition to CommunicationTools in src/agenthicc/runtime/comm_tools.py

async def mcp_connect(
    self,
    url: str,
    transport: str,
    name: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Connect to an MCP server, discover its tools, and register them.

    Each discovered tool is emitted as a ``ToolRegistered`` event and
    added to ``AppState.tools`` via the normal reducer path.  After this
    call returns, agents (including the caller) can invoke the new tools
    immediately.

    :param url: Transport endpoint (command string for stdio, URL for
        ws/streamable).
    :param transport: One of ``"stdio"``, ``"ws"``, ``"streamable"``.
    :param name: Optional slug for the server.  If omitted, a slug is
        derived from the URL/command.
    :param token: Optional bearer token for authenticated servers.
    :returns: Dict with ``server_name`` (str) and ``tool_count`` (int).
    :raises ValueError: For unsupported transport values.
    :raises ConnectionError: If the bridge cannot connect after all
        retry attempts.
    """
    if transport not in ("stdio", "ws", "streamable"):
        raise ValueError(
            f"mcp_connect: unsupported transport {transport!r}; "
            "expected 'stdio', 'ws', or 'streamable'"
        )
    if name is None:
        import re  # noqa: PLC0415
        slug = re.sub(r"[^a-z0-9]+", "_", url.lower().split("/")[-1])[:32]
        name = slug or "mcp_dynamic"

    from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415

    config = McpServerConfig(
        name=name,
        url=url,
        transport=transport,  # type: ignore[arg-type]
        token=token,
        auto_connect=True,
    )

    registry: McpToolRegistry = self._mcp_registry
    if registry is None:
        raise RuntimeError(
            "mcp_connect: McpToolRegistry is not configured. "
            "Add [[tools.mcp_servers]] to agenthicc.toml or inject the "
            "registry via CommunicationTools(mcp_registry=...)."
        )
    try:
        registry.register_server(config)
    except ValueError:
        # Server already registered; reconnect and re-discover.
        pass

    tools = await registry.connect_server(name)
    return {"server_name": name, "tool_count": len(tools)}
```

### 6.2 Constructor Change

`CommunicationTools.__init__` gains an optional `mcp_registry` parameter:

```python
def __init__(
    self,
    processor: EventProcessor,
    pool: AgentPool,
    message_bus: Any | None = None,
    mcp_registry: Any | None = None,   # McpToolRegistry | None
) -> None:
    self._processor = processor
    self._pool = pool
    self._bus = message_bus
    self._mcp_registry = mcp_registry
```

### 6.3 Example Agent Usage

An agent calls `mcp_connect` as a tool invocation through the standard tool dispatch pipeline:

```python
# The agent's reasoning loop issues this tool_call:
{
    "name": "mcp_connect",
    "input": {
        "url": "npx -y @modelcontextprotocol/server-brave-search",
        "transport": "stdio",
        "name": "brave_search"
    }
}

# Tool result:
{
    "server_name": "brave_search",
    "tool_count": 1
}

# Immediately after, the agent can call:
{
    "name": "mcp:brave_search:brave_web_search",
    "input": {"query": "asyncio best practices 2025"}
}
```

---

## 7. Permission Enforcement

MCP tool calls flow through the exact same `PermissionChecker` as all other tools. No special-casing is needed at the executor layer.

### 7.1 Permission Pattern

The `tool_name` passed to `PermissionChecker` is the full compound key:

```
mcp:filesystem:read_file
mcp:github:search_code
mcp:custom_server:run_query
```

`SecurityPolicy.permission_rules` entries use glob-style `tool_pattern` strings (as defined by `PermissionRule`). A `PermissionChecker` that interprets `tool_pattern` as a glob (via `fnmatch.fnmatch`) handles this naturally:

```python
import fnmatch

def make_policy_checker(policy: SecurityPolicy) -> PermissionChecker:
    def check(tool_name: str, args: dict, ctx: dict) -> bool | None:
        for rule in policy.permission_rules:
            if fnmatch.fnmatch(tool_name, rule.tool_pattern):
                if rule.action == "deny":
                    return False
                if rule.action == "allow":
                    return True
                if rule.action == "require_confirmation":
                    return True   # confirmation flow: future PRD
        return policy.default_action != "deny"
    return check
```

TOML permission examples:

```toml
[[security.permission_rules]]
tool_pattern = "mcp:filesystem:*"
action = "allow"

[[security.permission_rules]]
tool_pattern = "mcp:github:delete_*"
action = "require_confirmation"

[[security.permission_rules]]
# Deny all other MCP tools not explicitly allowed.
tool_pattern = "mcp:*"
action = "deny"
```

### 7.2 Hook Integration

`AgenthiccMcpTool.execute()` is called by `AgenthiccToolExecutor.execute()` at step 4. The `HookRunner` fires `on_before` before and `on_after` / `on_error` after. Plugin hooks registered under `entity_type="tool"` intercept MCP calls identically to local ones:

```python
# Example: audit hook that logs all outbound MCP calls
class McpAuditHook(LifecycleHook):
    async def on_after(self, entity: Tool, result: Any, ctx: Any) -> None:
        if entity.name.startswith("mcp:"):
            logger.info(
                "MCP audit: %s completed -> %r", entity.name, result
            )
```

---

## 8. Configuration Reference

```toml
# agenthicc.toml

# ── MCP server definitions ────────────────────────────────────────────

[[tools.mcp_servers]]
# Unique slug; used in tool names as mcp:{name}:{tool}.
name = "filesystem"
# Full shell command for stdio transport.
url = "npx -y @modelcontextprotocol/server-filesystem /workspace"
transport = "stdio"
auto_connect = true
reconnect_attempts = 3
reconnect_delay_seconds = 1.0

[[tools.mcp_servers]]
name = "github"
url = "wss://github-mcp.example.com"
transport = "ws"
# Environment variable reference; resolved at runtime.
token = "${GITHUB_MCP_TOKEN}"
auto_connect = true
reconnect_attempts = 5
reconnect_delay_seconds = 2.0

[[tools.mcp_servers]]
name = "internal_search"
url = "https://search.internal/mcp"
transport = "streamable"
token = "${SEARCH_API_TOKEN}"
# Deferred: only connected when mcp_connect comm tool is called.
auto_connect = false

# ── Permission rules for MCP tools ───────────────────────────────────

[[security.permission_rules]]
tool_pattern = "mcp:filesystem:*"
action = "allow"

[[security.permission_rules]]
tool_pattern = "mcp:github:*"
action = "allow"

[[security.permission_rules]]
# Deny all other MCP tools by default.
tool_pattern = "mcp:*"
action = "deny"
```

---

## 9. Implementation Plan

### 9.1 Phase 1 — Core Bridge (Week 1)

| Task | File | Notes |
|---|---|---|
| Define `McpServerConfig`, `McpToolSchema`, `AgenthiccMcpTool`, `McpToolBridge` | `src/agenthicc/tools/mcp.py` | New file; no changes to existing files required |
| Wire `lauren_ai.mcp.McpServer` transports (stdio/ws/streamable) | `src/agenthicc/tools/mcp.py` | Guard with `try/except ImportError` for tests without `lauren_ai` installed |
| Unit tests for `AgenthiccMcpTool` and `McpToolBridge` | `tests/unit/test_mcp_tool.py` | Mock MCP client; no real subprocess |

### 9.2 Phase 2 — Registry and Event Integration (Week 1-2)

| Task | File | Notes |
|---|---|---|
| Define `McpToolRegistry` | `src/agenthicc/tools/mcp.py` | Emits `ToolRegistered` events via `EventProcessor` |
| Add `ToolRegistered` reducer branch | `src/agenthicc/kernel/reducer.py` | `AppState.with_tool(ToolRegistration(...))` — check if already present |
| Wire registry into `AgenthiccApp.__init__` | `src/agenthicc/api/server.py` | Load configs, register servers, call `discover_all()` |
| Integration tests for full discovery-to-AppState path | `tests/integration/test_mcp_registry.py` | |

### 9.3 Phase 3 — mcp_connect Comm Tool (Week 2)

| Task | File | Notes |
|---|---|---|
| Add `mcp_connect` method to `CommunicationTools` | `src/agenthicc/runtime/comm_tools.py` | Inject `McpToolRegistry` via constructor |
| Update `__init__` signature of `CommunicationTools` | `src/agenthicc/runtime/comm_tools.py` | Optional `mcp_registry` param; backward-compatible |
| Unit tests for `mcp_connect` | `tests/unit/test_comm_tools.py` | Add to existing test file |

### 9.4 Phase 4 — Config Parsing and E2E (Week 2-3)

| Task | File | Notes |
|---|---|---|
| Parse `[[tools.mcp_servers]]` from TOML | `src/agenthicc/config.py` | Add `mcp_servers: list[McpServerConfig]` to config dataclass |
| E2E test: agent uses filesystem MCP tool | `tests/e2e/test_mcp_e2e.py` | Mock `lauren_ai.mcp.McpServer` |
| Shutdown hook | `src/agenthicc/api/server.py` | Call `registry.shutdown()` on app teardown |

---

## 10. Tests

### 10.1 Unit Tests

```python
# tests/unit/test_mcp_tool.py

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
import fnmatch

from agenthicc.tools.mcp import (
    AgenthiccMcpTool,
    McpToolBridge,
    McpToolCallError,
    McpToolSchema,
    McpServerConfig,
)
from agenthicc.kernel.state import PermissionRule, SecurityPolicy


# ── helpers ───────────────────────────────────────────────────────────

def make_schema(name: str = "read_file") -> McpToolSchema:
    return McpToolSchema(
        name=name,
        description=f"Reads a file ({name})",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


def make_bridge(server_name: str = "filesystem") -> McpToolBridge:
    config = McpServerConfig(name=server_name, url="echo hi", transport="stdio")
    bridge = McpToolBridge(config)
    bridge._client = AsyncMock()
    return bridge


# ── AgenthiccMcpTool ─────────────────────────────────────────────────

class TestAgenthiccMcpTool:
    def test_name_uses_compound_key(self):
        bridge = make_bridge("myserver")
        schema = make_schema("do_thing")
        tool = AgenthiccMcpTool(bridge, schema)
        assert tool.name == "mcp:myserver:do_thing"

    def test_description_forwarded_verbatim(self):
        bridge = make_bridge()
        schema = make_schema()
        schema.description = "Reads a file from the workspace"
        tool = AgenthiccMcpTool(bridge, schema)
        assert tool.description == "Reads a file from the workspace"

    def test_parameters_forwarded_verbatim(self):
        """MCP inputSchema must be forwarded without translation."""
        bridge = make_bridge()
        schema = make_schema()
        tool = AgenthiccMcpTool(bridge, schema)
        assert tool.parameters is schema.input_schema
        assert "path" in tool.parameters["properties"]

    def test_complex_schema_preserved(self):
        """Tools with nested anyOf / $ref schemas must not be altered."""
        complex_schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "options": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"limit": {"type": "integer"}},
                        },
                        {"type": "null"},
                    ]
                },
            },
            "required": ["query"],
            "$defs": {"Opts": {"type": "object"}},
        }
        bridge = make_bridge("search")
        schema = McpToolSchema(
            name="search_code",
            description="Search code",
            input_schema=complex_schema,
        )
        tool = AgenthiccMcpTool(bridge, schema)
        assert tool.parameters is complex_schema  # same object, not a copy

    @pytest.mark.asyncio
    async def test_execute_calls_bridge_call_tool(self):
        bridge = make_bridge()
        schema = make_schema("read_file")
        bridge._client.call_tool = AsyncMock(
            return_value=MagicMock(
                isError=False, content=[MagicMock(text="hello")]
            )
        )
        tool = AgenthiccMcpTool(bridge, schema)
        ctx = {"tool_call_id": "abc123"}
        result = await tool.execute({"path": "/tmp/x.txt"}, ctx)
        bridge._client.call_tool.assert_called_once_with(
            "read_file", {"path": "/tmp/x.txt"}
        )
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_execute_propagates_mcp_error(self):
        bridge = make_bridge()
        schema = make_schema("broken_tool")
        bridge._client.call_tool = AsyncMock(
            return_value=MagicMock(
                isError=True, content=[MagicMock(text="not found")]
            )
        )
        tool = AgenthiccMcpTool(bridge, schema)
        with pytest.raises(McpToolCallError, match="not found"):
            await tool.execute({}, {})

    @pytest.mark.asyncio
    async def test_execute_wraps_transport_exception(self):
        bridge = make_bridge()
        schema = make_schema("fragile")
        bridge._client.call_tool = AsyncMock(
            side_effect=ConnectionResetError("gone")
        )
        tool = AgenthiccMcpTool(bridge, schema)
        with pytest.raises(McpToolCallError):
            await tool.execute({}, {})

    @pytest.mark.asyncio
    async def test_tool_call_id_threaded_through_context(self):
        """tool_call_id from context must be accessible inside execute."""
        bridge = make_bridge()
        schema = make_schema()
        captured_ids: list[str] = []

        async def fake_call_tool(name, args, **kw):
            captured_ids.append(kw.get("tool_call_id", ""))
            r = MagicMock()
            r.isError = False
            r.content = [MagicMock(text="ok")]
            return r

        bridge.call_tool = fake_call_tool  # type: ignore[method-assign]
        tool = AgenthiccMcpTool(bridge, schema)
        await tool.execute({"path": "x"}, {"tool_call_id": "trace-42"})
        assert captured_ids[0] == "trace-42"


# ── Permission pattern generation ─────────────────────────────────────

class TestPermissionPattern:
    def test_compound_key_matches_server_glob(self):
        bridge = make_bridge("filesystem")
        schema = make_schema("read_file")
        tool = AgenthiccMcpTool(bridge, schema)
        assert fnmatch.fnmatch(tool.name, "mcp:filesystem:*")
        assert fnmatch.fnmatch(tool.name, "mcp:*")
        assert not fnmatch.fnmatch(tool.name, "mcp:github:*")

    def test_checker_denies_mcp_tool_when_rule_denies(self):
        policy = SecurityPolicy(
            permission_rules=(
                PermissionRule(tool_pattern="mcp:*", action="deny"),
            ),
            default_action="allow",
        )

        def checker(tool_name: str, args, ctx) -> bool | None:
            for rule in policy.permission_rules:
                if fnmatch.fnmatch(tool_name, rule.tool_pattern):
                    return rule.action != "deny"
            return policy.default_action != "deny"

        bridge = make_bridge("filesystem")
        tool = AgenthiccMcpTool(bridge, make_schema("read_file"))
        assert checker(tool.name, {}, {}) is False

    def test_checker_allows_specific_server_while_denying_others(self):
        policy = SecurityPolicy(
            permission_rules=(
                PermissionRule(tool_pattern="mcp:filesystem:*", action="allow"),
                PermissionRule(tool_pattern="mcp:*", action="deny"),
            ),
            default_action="deny",
        )

        def checker(tool_name: str, args, ctx) -> bool | None:
            for rule in policy.permission_rules:
                if fnmatch.fnmatch(tool_name, rule.tool_pattern):
                    return rule.action != "deny"
            return False

        fs_tool = AgenthiccMcpTool(make_bridge("filesystem"), make_schema("read_file"))
        gh_tool = AgenthiccMcpTool(make_bridge("github"), make_schema("push_code"))
        assert checker(fs_tool.name, {}, {}) is True
        assert checker(gh_tool.name, {}, {}) is False
```

### 10.2 Integration Tests

```python
# tests/integration/test_mcp_registry.py

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from agenthicc.kernel import EventProcessor, AppState
from agenthicc.tools.mcp import (
    McpServerConfig,
    McpToolBridge,
    McpToolRegistry,
)
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.hooks import HookRunner


def _mock_mcp_client(tool_names: list[str]) -> MagicMock:
    """Mock MCP client that advertises the given tool names."""
    client = AsyncMock()
    client.close = AsyncMock()
    client.list_tools = AsyncMock(return_value=[
        MagicMock(
            name=n,
            description=f"Tool {n}",
            inputSchema={"type": "object", "properties": {}},
        )
        for n in tool_names
    ])
    client.call_tool = AsyncMock(
        return_value=MagicMock(
            isError=False, content=[MagicMock(text="ok")]
        )
    )
    return client


@pytest.fixture
def event_processor():
    state = AppState.create()
    return EventProcessor(initial_state=state)


@pytest.fixture
def registry(event_processor):
    return McpToolRegistry(event_processor)


class TestMcpToolRegistryDiscovery:
    @pytest.mark.asyncio
    async def test_discover_all_registers_tools_in_appstate(
        self, registry, event_processor
    ):
        """Tools discovered from an MCP server appear in AppState.tools."""
        config = McpServerConfig(
            name="fs", url="echo hi", transport="stdio", auto_connect=True
        )
        registry.register_server(config)
        bridge = registry._servers["fs"]
        bridge._client = _mock_mcp_client(["read_file", "write_file"])

        await registry.discover_all()
        await event_processor.drain()

        state = event_processor.get_state()
        assert "mcp:fs:read_file" in state.tools
        assert "mcp:fs:write_file" in state.tools
        reg = state.tools["mcp:fs:read_file"]
        assert reg.is_builtin is False
        assert reg.description == "Tool read_file"

    @pytest.mark.asyncio
    async def test_register_duplicate_server_raises(self, registry):
        config = McpServerConfig(name="dup", url="x", transport="stdio")
        registry.register_server(config)
        with pytest.raises(ValueError, match="already registered"):
            registry.register_server(config)

    @pytest.mark.asyncio
    async def test_auto_connect_false_skips_discovery(
        self, registry, event_processor
    ):
        config = McpServerConfig(
            name="lazy", url="x", transport="stdio", auto_connect=False
        )
        registry.register_server(config)
        await registry.discover_all()
        await event_processor.drain()

        state = event_processor.get_state()
        # No tools registered because auto_connect=False.
        assert all(not k.startswith("mcp:lazy:") for k in state.tools)

    @pytest.mark.asyncio
    async def test_get_tool_returns_correct_instance(
        self, registry, event_processor
    ):
        config = McpServerConfig(name="srv", url="x", transport="stdio")
        registry.register_server(config)
        registry._servers["srv"]._client = _mock_mcp_client(["ping"])
        await registry.discover_all()

        tool = registry.get_tool("mcp:srv:ping")
        assert tool is not None
        assert tool.name == "mcp:srv:ping"

    @pytest.mark.asyncio
    async def test_all_tools_returns_flat_list(
        self, registry, event_processor
    ):
        config_a = McpServerConfig(name="a", url="x", transport="stdio")
        config_b = McpServerConfig(name="b", url="y", transport="stdio")
        registry.register_server(config_a)
        registry.register_server(config_b)
        registry._servers["a"]._client = _mock_mcp_client(["t1", "t2"])
        registry._servers["b"]._client = _mock_mcp_client(["t3"])
        await registry.discover_all()

        tools = registry.all_tools()
        names = {t.name for t in tools}
        assert names == {"mcp:a:t1", "mcp:a:t2", "mcp:b:t3"}

    @pytest.mark.asyncio
    async def test_call_tool_through_executor_pipeline(
        self, registry, event_processor
    ):
        """Executor -> AgenthiccMcpTool.execute -> bridge round-trip."""
        config = McpServerConfig(name="calc", url="x", transport="stdio")
        registry.register_server(config)
        mock_client = _mock_mcp_client(["add"])
        mock_client.call_tool = AsyncMock(
            return_value=MagicMock(
                isError=False, content=[MagicMock(text="42")]
            )
        )
        registry._servers["calc"]._client = mock_client
        await registry.discover_all()

        tool = registry.get_tool("mcp:calc:add")
        executor = AgenthiccToolExecutor(
            event_processor=event_processor,
            hook_runner=HookRunner(),
        )
        envelope = await executor.execute(
            tool, {"x": 20, "y": 22}, {"tool_call_id": "t1"}
        )
        assert envelope.ok is True
        assert envelope.value == "42"
        assert envelope.tool_name == "mcp:calc:add"
        assert envelope.tool_call_id == "t1"
```

### 10.3 E2E Tests

```python
# tests/e2e/test_mcp_e2e.py

"""
End-to-end: full agent run using an MCP filesystem tool.
The test mocks lauren_ai.mcp.McpServer to avoid real subprocesses.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agenthicc.kernel import AppState, EventProcessor
from agenthicc.tools.mcp import McpServerConfig, McpToolRegistry
from agenthicc.tools.executor import AgenthiccToolExecutor
from agenthicc.tools.hooks import HookRunner


class FakeMcpServer:
    """Minimal lauren_ai.mcp.McpServer stand-in."""

    def __init__(self, tools_data: dict[str, str]) -> None:
        self._tools_data = tools_data
        self.call_log: list[tuple[str, dict]] = []

    async def list_tools(self):
        return [
            MagicMock(
                name=name,
                description=f"MCP tool: {name}",
                inputSchema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            )
            for name in self._tools_data
        ]

    async def call_tool(self, name: str, args: dict):
        self.call_log.append((name, args))
        result = MagicMock()
        result.isError = False
        result.content = [MagicMock(text=self._tools_data.get(name, ""))]
        return result

    async def close(self):
        pass


@pytest.fixture
def event_processor():
    return EventProcessor(initial_state=AppState.create())


@pytest.mark.asyncio
async def test_agent_uses_mcp_filesystem_tool(event_processor):
    """
    Scenario:
      1. Registry discovers filesystem server with read_file tool.
      2. Executor dispatches read_file with a path argument.
      3. FakeMcpServer returns file content.
      4. ToolResultEnvelope has ok=True and the expected content.
    """
    fake_server = FakeMcpServer({"read_file": "Hello from workspace!"})

    config = McpServerConfig(
        name="filesystem",
        url="npx -y @mcp/server-filesystem /workspace",
        transport="stdio",
    )
    registry = McpToolRegistry(event_processor)
    registry.register_server(config)
    registry._servers["filesystem"]._client = fake_server

    await registry.discover_all()
    await event_processor.drain()

    state = event_processor.get_state()
    assert "mcp:filesystem:read_file" in state.tools

    tool = registry.get_tool("mcp:filesystem:read_file")
    assert tool is not None

    executor = AgenthiccToolExecutor(
        event_processor=event_processor,
        hook_runner=HookRunner(),
    )
    ctx = {"tool_call_id": "e2e-001", "agent_id": "agent-001"}
    envelope = await executor.execute(tool, {"path": "/workspace/README.md"}, ctx)

    assert envelope.ok is True
    assert envelope.value == "Hello from workspace!"
    assert envelope.tool_name == "mcp:filesystem:read_file"
    assert envelope.tool_call_id == "e2e-001"
    assert envelope.duration_ms > 0

    assert len(fake_server.call_log) == 1
    called_name, called_args = fake_server.call_log[0]
    assert called_name == "read_file"
    assert called_args["path"] == "/workspace/README.md"


@pytest.mark.asyncio
async def test_mcp_tool_permission_denied_via_executor(event_processor):
    """PermissionChecker returning False produces a denial envelope
    and the MCP server is never reached."""
    fake_server = FakeMcpServer({"dangerous_op": "boom"})
    config = McpServerConfig(name="danger", url="x", transport="stdio")
    registry = McpToolRegistry(event_processor)
    registry.register_server(config)
    registry._servers["danger"]._client = fake_server
    await registry.discover_all()

    tool = registry.get_tool("mcp:danger:dangerous_op")
    assert tool is not None

    executor = AgenthiccToolExecutor(
        event_processor=event_processor,
        permission_checker=lambda name, args, ctx: False,
    )
    envelope = await executor.execute(tool, {}, {})

    assert envelope.ok is False
    assert "permission_denied" in (envelope.error or "")
    assert len(fake_server.call_log) == 0  # server never reached


@pytest.mark.asyncio
async def test_mcp_connect_comm_tool_registers_tools_at_runtime(event_processor):
    """mcp_connect comm tool wires a new server at runtime and makes its
    tools available to agents immediately."""
    fake_server = FakeMcpServer({"echo": "echoed!"})
    registry = McpToolRegistry(event_processor)

    from agenthicc.runtime.pool import AgentPool
    from agenthicc.runtime.comm_tools import CommunicationTools

    pool = AgentPool()
    comm = CommunicationTools(
        processor=event_processor,
        pool=pool,
        mcp_registry=registry,
    )

    with patch(
        "agenthicc.tools.mcp.McpToolBridge._build_client",
        new=AsyncMock(return_value=fake_server),
    ):
        result = await comm.mcp_connect(
            url="npx echo-server",
            transport="stdio",
            name="echo_server",
        )

    assert result["server_name"] == "echo_server"
    assert result["tool_count"] == 1

    await event_processor.drain()
    state = event_processor.get_state()
    assert "mcp:echo_server:echo" in state.tools

    # Verify the tool is callable through the executor.
    tool = registry.get_tool("mcp:echo_server:echo")
    executor = AgenthiccToolExecutor(event_processor=event_processor)
    envelope = await executor.execute(tool, {}, {"tool_call_id": "x"})
    assert envelope.ok is True
    assert envelope.value == "echoed!"
```

---

## 11. Open Questions

| # | Question | Owner | Priority | Status |
|---|---|---|---|---|
| OQ-01 | The `tool_call_id` is passed to the bridge but the MCP spec has no standard trace field. Should we pass it as `_meta.tool_call_id` in request params for servers that support it? | Platform | Medium | Open |
| OQ-02 | `mcp_connect` at runtime does not persist the connection across application restarts. Should dynamic connections be written back to `agenthicc.toml`, or require manual config update? | Product | Low | Open |
| OQ-03 | When a MCP server advertises 200+ tools, all of them are forwarded to the LLM's tool list, inflating the context window. Should we add a `tool_allowlist` / `tool_blocklist` per server config entry? | Platform | High | Open |
| OQ-04 | The reconnect logic sets `self._client = None` and re-calls `connect()`. This is not safe if two coroutines call `call_tool` simultaneously during a reconnect. The `self._lock` in `connect()` covers the connect path but not teardown. Consider wrapping the full reconnect sequence under the lock. | Platform | High | Open |
| OQ-05 | `AgenthiccMcpTool.parameters` is the raw MCP `inputSchema`. If we add server-side arg validation later, MCP schemas with `$ref` or `anyOf` require a full JSON Schema validator, not basic type checking. | Platform | Medium | Open |
| OQ-06 | Should `McpToolRegistry.discover_all()` fail fast (raise on first error) or continue and register tools from healthy servers while logging errors? Current spec is log-and-continue. | Platform | High | Open |
| OQ-07 | The `mcp_connect` comm tool is accessible to any agent. Should there be a dedicated `mcp:connect` permission so administrators can restrict which agents can add new server connections? | Security | High | Open |
| OQ-08 | Do we need `mcp_disconnect` / `McpToolRegistry.remove_server(name)` to let agents cleanly drop servers and deregister their tools? | Product | Low | Open |
