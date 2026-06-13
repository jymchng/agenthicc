---
title: "PRD-28: MCP Bridge and Registry — Core Transport Layer"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-12-mcp-integration.md
---

# PRD-28: MCP Bridge and Registry

## Context

PRD-12 specifies the full MCP integration architecture.  This PRD covers the
core transport layer: the data types, the `McpToolBridge` connection wrapper,
the `AgenthiccMcpTool` subclass, and the `McpToolRegistry` singleton.
All other MCP PRDs (29–31) build on the types and interfaces defined here.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `McpServerConfig` parses `[[tools.mcp_servers]]` TOML stanzas and expands `${ENV_VAR}` tokens |
| G2 | `McpToolBridge` wraps a `lauren_ai.mcp.McpServer` connection for stdio / WebSocket / Streamable HTTP |
| G3 | `McpToolBridge.connect()` retries with exponential backoff; concurrent reconnects are serialised |
| G4 | `AgenthiccMcpTool` subclasses `Tool`; its `name` is `mcp:{server}:{tool_name}` |
| G5 | `McpToolRegistry.discover_all()` connects all `auto_connect=True` servers and returns all discovered tools |
| G6 | `McpToolRegistry` emits a `ToolRegistered` event for every discovered tool |
| G7 | `lauren_ai.mcp` import is guarded by `try/except ImportError`; clear error on missing dep |

## Non-Goals
- stdio subprocess management beyond launching (PRD-12 NG-01)
- MCP resources/prompts (PRD-12 NG-02)

---

## File to Create

`src/agenthicc/tools/mcp.py`

---

## Data Structures

### `McpServerConfig`

```python
import os, re
from dataclasses import dataclass, field
from typing import Any

_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

@dataclass
class McpServerConfig:
    name: str
    url: str                    # command string (stdio) or URL (ws/streamable)
    transport: str = "stdio"    # "stdio" | "ws" | "streamable"
    token: str = ""             # bearer token; supports ${ENV_VAR}
    auto_connect: bool = True
    reconnect_attempts: int = 3
    reconnect_delay_seconds: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "McpServerConfig":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def resolved_token(self) -> str:
        """Expand ${ENV_VAR} tokens in the token field."""
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), self.token)

    def resolved_url(self) -> str:
        """Expand ${ENV_VAR} tokens in the url field."""
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), self.url)
```

### `McpToolSchema`

```python
@dataclass
class McpToolSchema:
    name: str
    description: str
    input_schema: dict[str, Any]   # verbatim MCP inputSchema JSON
```

### `McpToolCallError`

```python
class McpToolCallError(RuntimeError):
    """Raised when an MCP tool call fails at the transport or protocol level."""
```

---

## `AgenthiccMcpTool`

Subclasses `Tool` (from `agenthicc.tools.base`).

```python
from agenthicc.tools.base import Tool

class AgenthiccMcpTool(Tool):
    def __init__(self, bridge: "McpToolBridge", schema: McpToolSchema) -> None:
        self._bridge = bridge
        self._schema = schema

    @property
    def name(self) -> str:
        return f"mcp:{self._bridge.server_name}:{self._schema.name}"

    @property
    def description(self) -> str:
        return self._schema.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema.input_schema   # passed verbatim (G-04)

    async def execute(
        self,
        args: dict[str, Any],
        context: dict[str, Any],
    ) -> Any:
        tool_call_id = context.get("tool_call_id", "")
        return await self._bridge.call_tool(
            self._schema.name, args, tool_call_id=tool_call_id
        )
```

---

## `McpToolBridge`

```python
import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

class McpToolBridge:
    def __init__(
        self,
        config: McpServerConfig,
        event_processor: Any | None = None,
    ) -> None:
        self._cfg = config
        self._events = event_processor
        self._client: Any = None        # lauren_ai.mcp.McpServer instance
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
            try:
                from lauren_ai.mcp import McpServer  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError(
                    "lauren_ai.mcp is not installed. "
                    "Install it with: pip install lauren-ai[mcp]"
                ) from exc

            last_exc: Exception | None = None
            for attempt in range(self._cfg.reconnect_attempts + 1):
                if attempt > 0:
                    delay = self._cfg.reconnect_delay_seconds * (2 ** (attempt - 1))
                    log.warning(
                        "MCP server %r connection attempt %d/%d — retrying in %.1fs",
                        self._cfg.name, attempt, self._cfg.reconnect_attempts, delay,
                    )
                    await asyncio.sleep(delay)
                try:
                    self._client = await self._build_client(McpServer)
                    self._connected = True
                    log.info("Connected to MCP server %r", self._cfg.name)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc

            raise McpToolCallError(
                f"Failed to connect to MCP server {self._cfg.name!r} "
                f"after {self._cfg.reconnect_attempts + 1} attempts: {last_exc}"
            )

    async def _build_client(self, McpServer: Any) -> Any:
        url = self._cfg.resolved_url()
        token = self._cfg.resolved_token() or None
        transport = self._cfg.transport.lower()
        if transport == "stdio":
            return await McpServer.stdio(url)
        elif transport in ("ws", "websocket"):
            return await McpServer.websocket(url, token=token)
        elif transport in ("streamable", "streamable_http", "http"):
            return await McpServer.streamable_http(url, token=token)
        else:
            raise ValueError(f"Unknown MCP transport: {self._cfg.transport!r}")

    async def disconnect(self) -> None:
        async with self._lock:
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None
            self._connected = False

    async def list_tools(self) -> list[McpToolSchema]:
        if not self._connected:
            raise McpToolCallError(f"Server {self._cfg.name!r} is not connected")
        raw_tools = await self._client.list_tools()
        return [
            McpToolSchema(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.inputSchema) if hasattr(t, "inputSchema") else {},
            )
            for t in raw_tools
        ]

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: str = "",
    ) -> Any:
        if not self._connected:
            raise McpToolCallError(f"Server {self._cfg.name!r} is not connected")
        try:
            result = await self._client.call_tool(tool_name, args)
        except Exception as exc:  # noqa: BLE001
            raise McpToolCallError(
                f"MCP call {self._cfg.name}/{tool_name} failed: {exc}"
            ) from exc

        if getattr(result, "isError", False):
            err_text = _extract_text_content(result)
            raise McpToolCallError(
                f"MCP server {self._cfg.name!r} returned error for {tool_name!r}: {err_text}"
            )
        return _extract_tool_content(result)


def _extract_tool_content(result: Any) -> Any:
    """Extract the usable payload from an MCP CallToolResult."""
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
    content = getattr(result, "content", [])
    return " ".join(getattr(b, "text", str(b)) for b in content) if content else str(result)
```

---

## `McpToolRegistry`

```python
from uuid import uuid4
from agenthicc.kernel import Event

class McpToolRegistry:
    def __init__(self, event_processor: Any | None = None) -> None:
        self._events = event_processor
        self._bridges: dict[str, McpToolBridge] = {}
        self._tools: dict[str, AgenthiccMcpTool] = {}

    def register_server(self, config: McpServerConfig) -> None:
        if config.name in self._bridges:
            raise ValueError(f"MCP server {config.name!r} is already registered")
        self._bridges[config.name] = McpToolBridge(config, self._events)
        log.debug("Registered MCP server config %r", config.name)

    async def discover_all(self) -> list[AgenthiccMcpTool]:
        discovered: list[AgenthiccMcpTool] = []
        for name, bridge in self._bridges.items():
            if not bridge._cfg.auto_connect:
                continue
            try:
                await bridge.connect()
                tools = await self._register_tools_from_bridge(bridge)
                discovered.extend(tools)
                log.info(
                    "MCP server %r: discovered %d tool(s)", name, len(tools)
                )
            except Exception as exc:  # noqa: BLE001
                log.error("MCP server %r: discovery failed — %s", name, exc)
        return discovered

    async def connect_server(self, server_name: str) -> list[AgenthiccMcpTool]:
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
        await self._events.emit(Event.create(
            "ToolRegistered",
            {
                "tool_id": uuid4().hex,
                "name": tool.name,
                "description": tool.description,
                "parameters_schema": tool.parameters,
                "is_builtin": False,
                "source_agent_id": None,
            },
        ))

    def get_tool(self, name: str) -> AgenthiccMcpTool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[AgenthiccMcpTool]:
        return list(self._tools.values())

    async def shutdown(self) -> None:
        for bridge in self._bridges.values():
            await bridge.disconnect()
        log.info("McpToolRegistry shut down")
```

---

## `__all__` and exports

```python
__all__ = [
    "McpServerConfig",
    "McpToolSchema",
    "McpToolCallError",
    "AgenthiccMcpTool",
    "McpToolBridge",
    "McpToolRegistry",
]
```

Update `src/agenthicc/tools/__init__.py` to re-export these symbols (behind a
`try/except ImportError` guard since `lauren_ai.mcp` is optional).

---

## Tests

```python
# tests/unit/test_mcp_bridge.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from agenthicc.tools.mcp import (
    McpServerConfig, McpToolSchema, McpToolBridge,
    AgenthiccMcpTool, McpToolRegistry, McpToolCallError,
    _extract_tool_content,
)

pytestmark = pytest.mark.unit


# ── McpServerConfig ──────────────────────────────────────────────────────────

def test_resolved_token_expands_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret123")
    cfg = McpServerConfig(name="s", url="u", token="${MY_TOKEN}")
    assert cfg.resolved_token() == "secret123"


def test_resolved_token_missing_env_keeps_literal(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    cfg = McpServerConfig(name="s", url="u", token="${MISSING_VAR}")
    assert cfg.resolved_token() == "${MISSING_VAR}"


def test_from_dict_ignores_unknown_keys():
    cfg = McpServerConfig.from_dict({"name": "x", "url": "y", "unknown": "z"})
    assert cfg.name == "x"


# ── AgenthiccMcpTool ─────────────────────────────────────────────────────────

def _make_tool():
    bridge = MagicMock()
    bridge.server_name = "myserver"
    schema = McpToolSchema(name="my_tool", description="Does stuff", input_schema={"type": "object"})
    return AgenthiccMcpTool(bridge, schema)


def test_tool_name_is_compound():
    tool = _make_tool()
    assert tool.name == "mcp:myserver:my_tool"


def test_tool_parameters_passthrough():
    tool = _make_tool()
    assert tool.parameters == {"type": "object"}


@pytest.mark.asyncio
async def test_tool_execute_passes_tool_call_id():
    bridge = MagicMock()
    bridge.server_name = "s"
    bridge.call_tool = AsyncMock(return_value="result")
    schema = McpToolSchema(name="t", description="", input_schema={})
    tool = AgenthiccMcpTool(bridge, schema)
    result = await tool.execute({"x": 1}, {"tool_call_id": "abc123"})
    bridge.call_tool.assert_called_once_with("t", {"x": 1}, tool_call_id="abc123")
    assert result == "result"


# ── McpToolBridge ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_sets_connected_flag():
    cfg = McpServerConfig(name="s", url="echo hello", transport="stdio")
    bridge = McpToolBridge(cfg)
    mock_server = AsyncMock()
    with patch("agenthicc.tools.mcp.McpToolBridge._build_client", return_value=mock_server):
        await bridge.connect()
    assert bridge.is_connected


@pytest.mark.asyncio
async def test_connect_idempotent():
    cfg = McpServerConfig(name="s", url="u", transport="stdio")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    # Should not raise even though _build_client is never called
    await bridge.connect()


@pytest.mark.asyncio
async def test_call_tool_raises_when_not_connected():
    bridge = McpToolBridge(McpServerConfig(name="s", url="u"))
    with pytest.raises(McpToolCallError, match="not connected"):
        await bridge.call_tool("t", {})


@pytest.mark.asyncio
async def test_call_tool_raises_on_mcp_error():
    cfg = McpServerConfig(name="s", url="u")
    bridge = McpToolBridge(cfg)
    bridge._connected = True
    err_result = MagicMock()
    err_result.isError = True
    err_result.content = []
    bridge._client = AsyncMock()
    bridge._client.call_tool = AsyncMock(return_value=err_result)
    with pytest.raises(McpToolCallError):
        await bridge.call_tool("t", {})


# ── McpToolRegistry ───────────────────────────────────────────────────────────

def test_register_server_duplicate_raises():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="x", url="u")
    reg.register_server(cfg)
    with pytest.raises(ValueError, match="already registered"):
        reg.register_server(cfg)


@pytest.mark.asyncio
async def test_discover_all_skips_non_auto_connect():
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="x", url="u", auto_connect=False)
    reg.register_server(cfg)
    discovered = await reg.discover_all()
    assert discovered == []


@pytest.mark.asyncio
async def test_discover_all_connects_and_emits(mocker):
    reg = McpToolRegistry()
    cfg = McpServerConfig(name="srv", url="echo", transport="stdio", auto_connect=True)
    reg.register_server(cfg)

    schema = McpToolSchema(name="ping", description="Ping", input_schema={})
    bridge = reg._bridges["srv"]
    mocker.patch.object(bridge, "connect", AsyncMock())
    mocker.patch.object(bridge, "list_tools", AsyncMock(return_value=[schema]))

    tools = await reg.discover_all()
    assert len(tools) == 1
    assert tools[0].name == "mcp:srv:ping"


# ── _extract_tool_content ─────────────────────────────────────────────────────

def test_extract_single_text_block():
    block = MagicMock()
    block.text = "hello"
    result = MagicMock()
    result.content = [block]
    assert _extract_tool_content(result) == "hello"


def test_extract_multiple_blocks():
    b1, b2 = MagicMock(), MagicMock()
    b1.text = "a"
    b2.text = "b"
    result = MagicMock()
    result.content = [b1, b2]
    out = _extract_tool_content(result)
    assert out == ["a", "b"]
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mcp_bridge.py -v

# With a real MCP server (requires npx):
mkdir -p .agenthicc
cat >> .agenthicc/agenthicc.toml << 'EOF'
[[tools.mcp_servers]]
name = "filesystem"
url = "npx -y @modelcontextprotocol/server-filesystem /tmp"
transport = "stdio"
auto_connect = true
EOF

uv run agenthicc
# Session log should show: "MCP server 'filesystem': discovered N tool(s)"
# Ask agent: "list the files in /tmp using the mcp:filesystem:list_directory tool"
```
