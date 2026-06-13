"""MCP tool bridge and registry (PRD-28)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from agenthicc.tools.base import Tool
from agenthicc.kernel import Event

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\${([A-Z_][A-Z0-9_]*)}")

# ---------------------------------------------------------------------------
# Optional lauren_mcp import guard (G7)
# ---------------------------------------------------------------------------
try:
    from lauren_mcp import McpServer as _McpServer
    from lauren_mcp._client._stdio import McpCallError as _McpCallError
    from lauren_mcp._types import ToolSchema as _ToolSchema, ToolResult as _ToolResult

    _LAUREN_MCP_AVAILABLE = True
except ImportError:
    _LAUREN_MCP_AVAILABLE = False
    _McpServer = _McpCallError = _ToolSchema = _ToolResult = None  # type: ignore[assignment,misc]

__all__ = [
    "McpServerConfig",
    "McpToolSchema",
    "McpToolCallError",
    "AgenthiccMcpTool",
    "McpToolBridge",
    "McpToolRegistry",
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class McpServerConfig:
    """Configuration for a single MCP server connection.

    Corresponds to a ``[[tools.mcp_servers]]`` TOML stanza.
    """

    name: str
    url: str  # command string (stdio) or URL (ws/streamable)
    transport: str = "stdio"  # "stdio" | "ws" | "websocket" | "streamable" | "http"
    token: str = ""  # bearer token; supports ${ENV_VAR}
    auto_connect: bool = True
    reconnect_attempts: int = 3
    reconnect_delay_seconds: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "McpServerConfig":
        """Build from a raw dict, silently ignoring unknown keys."""
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def resolved_token(self) -> str:
        """Expand ``${ENV_VAR}`` tokens in the token field."""
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), self.token)

    def resolved_url(self) -> str:
        """Expand ``${ENV_VAR}`` tokens in the url field."""
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), self.url)


@dataclass
class McpToolSchema:
    """Lightweight representation of a tool advertised by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]  # verbatim MCP inputSchema JSON


class McpToolCallError(RuntimeError):
    """Raised when an MCP tool call fails at the transport or protocol level."""


# ---------------------------------------------------------------------------
# AgenthiccMcpTool
# ---------------------------------------------------------------------------


class AgenthiccMcpTool(Tool):
    """A :class:`Tool` that proxies calls to a remote MCP server tool."""

    def __init__(self, bridge: "McpToolBridge", schema: McpToolSchema) -> None:
        self._bridge = bridge
        self._schema = schema

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"mcp:{self._bridge.server_name}:{self._schema.name}"

    @property
    def description(self) -> str:  # type: ignore[override]
        return self._schema.description

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        return self._schema.input_schema

    async def execute(
        self,
        args: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        tool_call_id = context.get("tool_call_id", "")
        return await self._bridge.call_tool(
            self._schema.name, args, tool_call_id=tool_call_id
        )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_tool_content(result: Any) -> Any:
    """Extract the usable payload from an MCP ``CallToolResult``."""
    content = getattr(result, "content", None)
    if not content:
        return None
    if len(content) == 1:
        block = content[0]
        return getattr(block, "text", None) or getattr(block, "data", None) or str(block)
    return [
        getattr(b, "text", None) or getattr(b, "data", None) or str(b)
        for b in content
    ]


def _extract_text_content(result: Any) -> str:
    """Extract a plain-text summary from an MCP result (used for error messages)."""
    content = getattr(result, "content", [])
    return " ".join(getattr(b, "text", str(b)) for b in content) if content else str(result)


# ---------------------------------------------------------------------------
# McpToolBridge
# ---------------------------------------------------------------------------


class McpToolBridge:
    """Wraps a single MCP server connection.

    Supports stdio, WebSocket, and Streamable HTTP transports.
    ``connect()`` serialises concurrent callers via an ``asyncio.Lock`` and
    retries with exponential backoff.
    """

    def __init__(
        self,
        config: McpServerConfig,
        event_processor: Any | None = None,
    ) -> None:
        self._cfg = config
        self._events = event_processor
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def server_name(self) -> str:
        return self._cfg.name

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the MCP server, retrying with exponential backoff."""
        async with self._lock:
            if self._connected:
                return

            if not _LAUREN_MCP_AVAILABLE:
                raise ImportError(
                    "lauren_mcp is not installed. "
                    "Install it with: pip install lauren-mcp"
                )

            last_exc: Exception | None = None
            for attempt in range(self._cfg.reconnect_attempts + 1):
                if attempt > 0:
                    delay = self._cfg.reconnect_delay_seconds * (2 ** (attempt - 1))
                    log.warning(
                        "MCP server %r connection attempt %d/%d — retrying in %.1fs",
                        self._cfg.name,
                        attempt,
                        self._cfg.reconnect_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                try:
                    self._client = await self._build_client()
                    await self._client.connect()
                    self._connected = True
                    log.info("Connected to MCP server %r", self._cfg.name)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc

            raise McpToolCallError(
                f"Failed to connect to MCP server {self._cfg.name!r} "
                f"after {self._cfg.reconnect_attempts + 1} attempts: {last_exc}"
            )

    async def _build_client(self) -> Any:
        """Instantiate (but do not connect) the appropriate McpServer client."""
        url = self._cfg.resolved_url()
        token = self._cfg.resolved_token() or None
        transport = self._cfg.transport.lower()

        if transport == "stdio":
            command = shlex.split(url)
            client = _McpServer.stdio(command, max_retries=self._cfg.reconnect_attempts)
        elif transport in ("ws", "websocket"):
            headers = {"Authorization": f"Bearer {token}"} if token else None
            client = _McpServer.ws(url, headers=headers, max_retries=self._cfg.reconnect_attempts)
        elif transport in ("streamable", "streamable_http", "http"):
            headers = {"Authorization": f"Bearer {token}"} if token else None
            client = _McpServer.streamable_http(
                url, headers=headers, max_retries=self._cfg.reconnect_attempts
            )
        else:
            raise ValueError(f"Unknown MCP transport: {self._cfg.transport!r}")

        return client

    async def disconnect(self) -> None:
        """Close the underlying client connection."""
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None
            self._connected = False

    async def list_tools(self) -> list[McpToolSchema]:
        """Return all tools advertised by the connected MCP server."""
        if not self._connected:
            raise McpToolCallError(f"Server {self._cfg.name!r} is not connected")
        raw_tools = await self._client.list_tools()
        return [
            McpToolSchema(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.inputSchema) if getattr(t, "inputSchema", None) else {},
            )
            for t in raw_tools
        ]

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str = "",
    ) -> Any:
        """Call a tool on the remote MCP server and return the extracted result."""
        if not self._connected:
            raise McpToolCallError(f"Server {self._cfg.name!r} is not connected")
        try:
            result = await self._client.call_tool(tool_name, args)
        except Exception as exc:  # noqa: BLE001
            # Re-raise McpCallError from lauren_mcp transparently when available
            if _LAUREN_MCP_AVAILABLE and _McpCallError is not None and isinstance(exc, _McpCallError):
                raise McpToolCallError(
                    f"MCP call {self._cfg.name}/{tool_name} failed: {exc}"
                ) from exc
            raise McpToolCallError(
                f"MCP call {self._cfg.name}/{tool_name} failed: {exc}"
            ) from exc

        if getattr(result, "isError", False):
            err_text = _extract_text_content(result)
            raise McpToolCallError(
                f"MCP server {self._cfg.name!r} returned error for {tool_name!r}: {err_text}"
            )
        return _extract_tool_content(result)


# ---------------------------------------------------------------------------
# McpToolRegistry
# ---------------------------------------------------------------------------


class McpToolRegistry:
    """Manages a collection of :class:`McpToolBridge` instances.

    Call :meth:`register_server` for each MCP server config, then
    :meth:`discover_all` to connect and enumerate all tools.
    """

    def __init__(self, event_processor: Any | None = None) -> None:
        self._events = event_processor
        self._bridges: dict[str, McpToolBridge] = {}
        self._tools: dict[str, AgenthiccMcpTool] = {}

    def register_server(self, config: McpServerConfig) -> None:
        """Register an MCP server config; raises :exc:`ValueError` on duplicates."""
        if config.name in self._bridges:
            raise ValueError(f"MCP server {config.name!r} is already registered")
        self._bridges[config.name] = McpToolBridge(config, self._events)
        log.debug("Registered MCP server config %r", config.name)

    async def discover_all(self) -> list[AgenthiccMcpTool]:
        """Connect all ``auto_connect=True`` servers and return their tools."""
        discovered: list[AgenthiccMcpTool] = []
        for name, bridge in self._bridges.items():
            if not bridge._cfg.auto_connect:
                continue
            try:
                await bridge.connect()
                tools = await self._register_tools_from_bridge(bridge)
                discovered.extend(tools)
                log.info("MCP server %r: discovered %d tool(s)", name, len(tools))
            except Exception as exc:  # noqa: BLE001
                log.error("MCP server %r: discovery failed — %s", name, exc)
        return discovered

    async def connect_server(self, server_name: str) -> list[AgenthiccMcpTool]:
        """Explicitly connect a server and register its tools."""
        bridge = self._bridges.get(server_name)
        if bridge is None:
            raise KeyError(f"No MCP server registered with name {server_name!r}")
        await bridge.connect()
        return await self._register_tools_from_bridge(bridge)

    async def _register_tools_from_bridge(
        self, bridge: McpToolBridge
    ) -> list[AgenthiccMcpTool]:
        schemas = await bridge.list_tools()
        tools: list[AgenthiccMcpTool] = []
        for schema in schemas:
            tool = AgenthiccMcpTool(bridge, schema)
            self._tools[tool.name] = tool
            await self._emit_tool_registered(tool)
            tools.append(tool)
        return tools

    async def _emit_tool_registered(self, tool: AgenthiccMcpTool) -> None:
        if self._events is None:
            return
        await self._events.emit(
            Event.create(
                "ToolRegistered",
                {
                    "tool_id": uuid4().hex,
                    "name": tool.name,
                    "description": tool.description,
                    "parameters_schema": tool.parameters,
                    "is_builtin": False,
                    "source_agent_id": None,
                },
            )
        )

    def get_tool(self, name: str) -> AgenthiccMcpTool | None:
        """Look up a registered tool by its compound name."""
        return self._tools.get(name)

    def all_tools(self) -> list[AgenthiccMcpTool]:
        """Return all registered MCP tools."""
        return list(self._tools.values())

    async def shutdown(self) -> None:
        """Disconnect all bridges."""
        for bridge in self._bridges.values():
            await bridge.disconnect()
        log.info("McpToolRegistry shut down")
