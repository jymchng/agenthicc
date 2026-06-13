---
title: "PRD-30: mcp_connect Comm Tool — Runtime Server Connection"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-28-mcp-bridge-and-registry.md, prd-29-mcp-config-and-startup.md
---

# PRD-30: mcp_connect Comm Tool

## Context

PRD-29 handles MCP servers declared statically in `agenthicc.toml`.  This PRD
specifies the `mcp_connect` **comm tool** that lets agents (and users via slash
commands) connect to new MCP servers at runtime — without restarting the
session.  This directly implements PRD-12 Goal G-07.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `mcp_connect(url, transport, name, token)` connects to a server and registers its tools |
| G2 | The comm tool validates `transport` against `["stdio", "ws", "streamable"]` |
| G3 | A human-readable `name` is auto-generated from the URL if not supplied |
| G4 | Newly registered tools are immediately available in subsequent agent turns |
| G5 | `mcp_connect` is reachable from `CommunicationTools` (the agent comm layer) |
| G6 | A `/mcp` slash command in the TUI surfaces server status and lets users connect interactively |

## Non-Goals
- Disconnecting a server at runtime (session lifecycle manages teardown)
- Connecting servers that require OAuth flows (use pre-minted tokens)

---

## Files to Modify

1. **`src/agenthicc/runtime/comm_tools.py`** — add `mcp_connect` method
2. **`src/agenthicc/tui/app.py`** — add `/mcp` slash command to `SlashCommandHandler`

---

## 1. `CommunicationTools` — `mcp_connect`

```python
# src/agenthicc/runtime/comm_tools.py  (additions)

_VALID_TRANSPORTS = frozenset({"stdio", "ws", "websocket", "streamable", "http"})


def _auto_name(url: str) -> str:
    """Generate a slug from the URL for use as server name."""
    import re, urllib.parse  # noqa: PLC0415
    try:
        parsed = urllib.parse.urlparse(url)
        base = parsed.netloc or parsed.path.split()[-1]
        return re.sub(r"[^a-z0-9-]", "-", base.lower()).strip("-")[:32] or "mcp-server"
    except Exception:
        return "mcp-server"


class CommunicationTools:
    # Existing __init__ gains optional mcp_registry parameter:
    def __init__(
        self,
        processor: Any,
        agent_pool: Any | None = None,
        mcp_registry: Any | None = None,     # ← new optional param
    ) -> None:
        ...
        self._mcp_registry = mcp_registry

    async def mcp_connect(
        self,
        url: str,
        transport: str = "stdio",
        name: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Connect to an MCP server at runtime and register its tools.

        Args:
            url: Command string (stdio) or server URL (ws/streamable).
            transport: One of "stdio", "ws", "streamable".
            name: Optional server slug; auto-generated from URL if omitted.
            token: Bearer token for authenticated servers.

        Returns:
            {"server_name": str, "tool_count": int, "tools": list[str]}
        """
        transport = transport.lower()
        if transport not in _VALID_TRANSPORTS:
            return {
                "ok": False,
                "error": f"Unknown transport {transport!r}. "
                         f"Use one of: {sorted(_VALID_TRANSPORTS)}",
            }

        if self._mcp_registry is None:
            return {"ok": False, "error": "McpToolRegistry not available in this session"}

        server_name = name or _auto_name(url)

        try:
            from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415
            cfg = McpServerConfig(
                name=server_name,
                url=url,
                transport=transport,
                token=token or "",
                auto_connect=True,
            )
            self._mcp_registry.register_server(cfg)
            tools = await self._mcp_registry.connect_server(server_name)
            tool_names = [t.name for t in tools]
            await self._emit(
                "application_log",
                {
                    "level": "info",
                    "message": f"mcp_connect: connected {server_name!r}, "
                               f"{len(tools)} tool(s) registered",
                },
            )
            return {
                "ok": True,
                "server_name": server_name,
                "tool_count": len(tools),
                "tools": tool_names,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
```

---

## 2. `/mcp` Slash Command

Add to `SlashCommandHandler` in `src/agenthicc/tui/app.py`:

```python
# In SLASH_HELP:
"/mcp": "Show MCP server status or connect a new server  (/mcp connect <url> [transport])",

# In handle():
if first == "/mcp":
    self._mcp(stripped, console)
    return True

# New method:
def _mcp(self, cmd: str, console: Any) -> None:
    if not RICH_AVAILABLE:
        return
    parts = cmd.split()
    mcp_registry = getattr(self._renderer, "_mcp_registry", None) if self._renderer else None

    # /mcp  — show status
    if len(parts) == 1:
        if mcp_registry is None:
            console.print("[dim]No MCP registry active.[/dim]")
            return
        table = Table(title="MCP Servers", box=rich_box.SIMPLE)
        table.add_column("Server", style="cyan")
        table.add_column("Transport")
        table.add_column("Connected")
        table.add_column("Tools", justify="right")
        for name, bridge in mcp_registry._bridges.items():
            connected = "[green]✓[/green]" if bridge.is_connected else "[dim]–[/dim]"
            tool_count = str(len([t for t in mcp_registry.all_tools() if t.name.startswith(f"mcp:{name}:")]))
            table.add_row(name, bridge._cfg.transport, connected, tool_count)
        console.print(table)
        return

    # /mcp connect <url> [transport] [name]
    if parts[1] == "connect" and len(parts) >= 3:
        url = parts[2]
        transport = parts[3] if len(parts) > 3 else "stdio"
        name = parts[4] if len(parts) > 4 else None
        if mcp_registry is None:
            console.print("[red]No MCP registry — cannot connect.[/red]")
            return
        if self._renderer is not None:
            self._renderer._pending_skill = f"[System: connecting to MCP server {url!r}]"
        console.print(f"  [dim]Connecting to MCP server {url!r} via {transport}…[/dim]")
        # The actual async connect happens through on_input → mcp_connect comm tool.
        # For now, store the intent so the LLM can call mcp_connect().
        if self._renderer is not None:
            self._renderer._pending_skill = (
                f"Connect to the MCP server at {url!r} using transport {transport!r}"
                + (f" with name {name!r}" if name else "")
                + ". Use the mcp_connect tool."
            )
        return

    console.print("[dim]Usage: /mcp  OR  /mcp connect <url> [transport] [name][/dim]")
```

---

## Wiring `mcp_registry` into `CommunicationTools`

In `_run_tui_session()` in `__main__.py`, pass the registry to `CommunicationTools`
(or wherever `CommunicationTools` is instantiated for the agent turn):

```python
# _mcp_registry is set in _run_tui_session() per PRD-29.
# Pass it when creating comm_tools or adapter.
adapter = TUIEventAdapter(model, mcp_registry=_mcp_registry)
```

Or, if `CommunicationTools` is constructed inside the kernel adapter,
inject it via `renderer._mcp_registry` (already set by PRD-29) and
read it in `_run_agent_turn()` when building the agent comm context.

---

## Tests

```python
# tests/unit/test_mcp_connect.py

import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.unit


def _make_comm_tools_with_registry():
    from agenthicc.runtime.comm_tools import CommunicationTools
    mock_registry = MagicMock()
    mock_registry.register_server = MagicMock()
    mock_registry.connect_server = AsyncMock(return_value=[])
    proc = MagicMock()
    proc.emit = AsyncMock()
    ct = CommunicationTools(processor=proc, mcp_registry=mock_registry)
    return ct, mock_registry


@pytest.mark.asyncio
async def test_mcp_connect_invalid_transport():
    ct, _ = _make_comm_tools_with_registry()
    result = await ct.mcp_connect(url="u", transport="ftp")
    assert result["ok"] is False
    assert "transport" in result["error"].lower()


@pytest.mark.asyncio
async def test_mcp_connect_no_registry():
    from agenthicc.runtime.comm_tools import CommunicationTools
    ct = CommunicationTools(processor=MagicMock())
    result = await ct.mcp_connect(url="u")
    assert result["ok"] is False
    assert "registry" in result["error"].lower()


@pytest.mark.asyncio
async def test_mcp_connect_success():
    from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolBridge, McpToolSchema, McpServerConfig
    bridge = MagicMock()
    bridge.server_name = "x"
    schema = McpToolSchema(name="ping", description="", input_schema={})
    tool = AgenthiccMcpTool(bridge, schema)

    ct, mock_reg = _make_comm_tools_with_registry()
    mock_reg.connect_server = AsyncMock(return_value=[tool])

    result = await ct.mcp_connect(url="echo hi", transport="stdio", name="myserver")
    assert result["ok"] is True
    assert result["server_name"] == "myserver"
    assert result["tool_count"] == 1
    assert "mcp:x:ping" in result["tools"]


def test_auto_name_from_url():
    from agenthicc.runtime.comm_tools import _auto_name
    assert _auto_name("wss://github.example.com") == "github-example-com"
    assert _auto_name("npx -y @mcp/server /path") == "-y"   # takes last path segment
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mcp_connect.py -v

uv run agenthicc
# /mcp                        → shows server status table
# /mcp connect npx -y @modelcontextprotocol/server-filesystem /tmp stdio fs
#   → agent calls mcp_connect, connects, logs tool count
# "list files in /tmp using filesystem tools" → uses mcp:fs:list_directory
```
