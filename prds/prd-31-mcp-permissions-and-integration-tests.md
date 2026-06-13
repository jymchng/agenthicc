---
title: "PRD-31: MCP Permissions, Executor Integration, and Integration Tests"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-28-mcp-bridge-and-registry.md, prd-29-mcp-config-and-startup.md, prd-30-mcp-connect-comm-tool.md
---

# PRD-31: MCP Permissions, Executor Integration, and Integration Tests

## Context

PRDs 28–30 define the MCP transport layer, startup, and runtime connection.
This PRD closes the loop: MCP tools must flow through the same
`AgenthiccToolExecutor` pipeline as built-in tools (permission check,
before/after/error hooks, timeout, event emission), and the full integration
must be covered by integration and e2e tests using a lightweight in-process
MCP test server.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `AgenthiccMcpTool` instances pass through `AgenthiccToolExecutor.execute()` unchanged |
| G2 | `PermissionChecker` evaluates `mcp:{server}:{tool}` names with glob patterns |
| G3 | `on_before` / `on_after` / `on_error` hooks fire for MCP tool calls exactly as for built-ins |
| G4 | `ToolCallStarted` and `ToolCallComplete` events are emitted for every MCP call |
| G5 | `McpToolCallError` is captured as a tool error envelope (not a session crash) |
| G6 | An in-process fake MCP server fixture enables integration tests without npx or a real server |
| G7 | Integration tests cover: discovery, permission deny, hook firing, error propagation |

---

## 1. Executor Integration — No Code Changes Required

`AgenthiccToolExecutor.execute()` already accepts any `Tool` subclass.
`AgenthiccMcpTool` satisfies the `Tool` ABC (`name`, `description`,
`parameters`, async `execute(args, context)`), so MCP tools flow through the
full pipeline without modification.

Verify the existing pipeline handles MCP tools correctly by passing an
`AgenthiccMcpTool` instance to the executor in tests.

---

## 2. Permission Patterns for MCP Tools

No changes to `AgenthiccToolExecutor` or `PermissionChecker`.
The compound naming convention `mcp:{server}:{tool}` is chosen specifically
to work with the existing `fnmatch`-based glob:

```toml
# Allow all filesystem MCP tools
[[security.permission_rules]]
tool_pattern = "mcp:filesystem:*"
action = "allow"

# Allow a specific tool only
[[security.permission_rules]]
tool_pattern = "mcp:github:create_pull_request"
action = "allow"

# Block all MCP tools by default
[[security.permission_rules]]
tool_pattern = "mcp:*"
action = "deny"
```

Document this pattern in `prd-12-mcp-integration.md` (no code changes needed).

---

## 3. `McpToolCallError` → Error Envelope

`AgenthiccToolExecutor.execute()` already wraps all exceptions:

```python
except Exception as exc:
    ...
    env = envelope(False, error=f"{type(exc).__name__}: {exc}")
```

`McpToolCallError` is a subclass of `RuntimeError`, so it is caught by the
existing `except Exception` block. No changes needed.

To improve error messages, add one human-friendly mapping in the executor:

```python
# In AgenthiccToolExecutor.execute(), catch block:
except Exception as exc:
    from agenthicc.tools.mcp import McpToolCallError  # noqa: PLC0415
    if isinstance(exc, McpToolCallError):
        error_str = str(exc)   # already human-readable
    else:
        error_str = f"{type(exc).__name__}: {exc}"
    env = envelope(False, error=error_str)
    ...
```

This is the **only** required change to `executor.py`.

---

## 4. In-Process Fake MCP Server Fixture

For integration tests that don't need `npx`, use a pytest fixture that creates
a `McpToolBridge` with a monkey-patched `_client`:

```python
# tests/conftest.py  (addition) or tests/integration/conftest.py

import pytest
from unittest.mock import AsyncMock, MagicMock
from agenthicc.tools.mcp import McpToolBridge, McpToolSchema, McpServerConfig


@pytest.fixture
def fake_mcp_server():
    """A McpToolBridge whose underlying client is a mock."""
    cfg = McpServerConfig(name="fake", url="fake://test", transport="stdio")
    bridge = McpToolBridge(cfg)
    bridge._connected = True

    # Default tools exposed by the fake server
    schemas = [
        McpToolSchema(name="echo", description="Echo the input", input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }),
        McpToolSchema(name="fail_tool", description="Always fails", input_schema={}),
    ]

    mock_client = AsyncMock()
    mock_client.list_tools = AsyncMock(return_value=schemas)

    async def _call_tool(name, args):
        if name == "fail_tool":
            result = MagicMock()
            result.isError = True
            result.content = [MagicMock(text="intentional failure")]
            return result
        result = MagicMock()
        result.isError = False
        block = MagicMock()
        block.text = args.get("message", "ok")
        result.content = [block]
        return result

    mock_client.call_tool = _call_tool
    bridge._client = mock_client
    return bridge
```

---

## 5. Integration Tests

```python
# tests/integration/test_mcp_registry.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agenthicc.tools.mcp import (
    McpServerConfig, McpToolRegistry, AgenthiccMcpTool,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_bridge_factory():
    """Return a factory that creates connected bridges with N mock tools."""
    from agenthicc.tools.mcp import McpToolBridge, McpToolSchema

    def _factory(server_name: str, tool_names: list[str]):
        cfg = McpServerConfig(name=server_name, url="fake://", transport="stdio")
        bridge = McpToolBridge(cfg)
        bridge._connected = True
        schemas = [McpToolSchema(name=n, description=f"Tool {n}", input_schema={}) for n in tool_names]
        bridge._client = AsyncMock()
        bridge._client.list_tools = AsyncMock(return_value=schemas)
        bridge._client.call_tool = AsyncMock(return_value=MagicMock(isError=False, content=[]))
        return bridge

    return _factory


@pytest.mark.asyncio
async def test_registry_discover_registers_tools(fake_bridge_factory):
    reg = McpToolRegistry()
    bridge = fake_bridge_factory("srv", ["tool_a", "tool_b"])
    reg._bridges["srv"] = bridge

    tools = await reg.discover_all()
    assert len(tools) == 2
    names = {t.name for t in tools}
    assert "mcp:srv:tool_a" in names
    assert "mcp:srv:tool_b" in names


@pytest.mark.asyncio
async def test_registry_emits_tool_registered_events(fake_bridge_factory):
    mock_proc = MagicMock()
    mock_proc.emit = AsyncMock()
    reg = McpToolRegistry(event_processor=mock_proc)
    bridge = fake_bridge_factory("s", ["ping"])
    reg._bridges["s"] = bridge

    await reg.discover_all()
    mock_proc.emit.assert_called_once()
    event = mock_proc.emit.call_args[0][0]
    assert event.event_type == "ToolRegistered"
    assert event.payload["name"] == "mcp:s:ping"


@pytest.mark.asyncio
async def test_registry_connect_server_on_demand(fake_bridge_factory):
    reg = McpToolRegistry()
    bridge = fake_bridge_factory("on_demand", ["lazy_tool"])
    cfg = McpServerConfig(name="on_demand", url="fake://", auto_connect=False)
    reg._bridges["on_demand"] = bridge   # inject pre-built bridge

    tools = await reg.connect_server("on_demand")
    assert len(tools) == 1
    assert tools[0].name == "mcp:on_demand:lazy_tool"


@pytest.mark.asyncio
async def test_registry_get_tool_lookup(fake_bridge_factory):
    reg = McpToolRegistry()
    bridge = fake_bridge_factory("x", ["ping"])
    reg._bridges["x"] = bridge
    await reg.discover_all()
    tool = reg.get_tool("mcp:x:ping")
    assert tool is not None
    assert isinstance(tool, AgenthiccMcpTool)


# ── Executor integration test ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_tool_through_executor(fake_mcp_server):
    """AgenthiccMcpTool flows through AgenthiccToolExecutor without changes."""
    from agenthicc.tools.executor import AgenthiccToolExecutor
    from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolSchema

    schema = McpToolSchema(name="echo", description="Echo", input_schema={})
    tool = AgenthiccMcpTool(fake_mcp_server, schema)

    executor = AgenthiccToolExecutor(event_processor=None)
    env = await executor.execute(tool, {"message": "hello"}, {})

    assert env.ok
    assert env.value == "hello"
    assert env.duration_ms >= 0


@pytest.mark.asyncio
async def test_mcp_tool_error_becomes_envelope(fake_mcp_server):
    """McpToolCallError is wrapped into a failed ToolResultEnvelope."""
    from agenthicc.tools.executor import AgenthiccToolExecutor
    from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolSchema

    schema = McpToolSchema(name="fail_tool", description="Fails", input_schema={})
    tool = AgenthiccMcpTool(fake_mcp_server, schema)

    executor = AgenthiccToolExecutor(event_processor=None)
    env = await executor.execute(tool, {}, {})

    assert not env.ok
    assert "failure" in env.error.lower()


@pytest.mark.asyncio
async def test_mcp_tool_permission_denied():
    """PermissionChecker with mcp:* deny pattern blocks MCP tools."""
    from agenthicc.tools.executor import AgenthiccToolExecutor
    from agenthicc.tools.mcp import AgenthiccMcpTool, McpToolSchema, McpToolBridge, McpServerConfig
    import fnmatch

    def deny_all_mcp(name, args, ctx):
        return False if fnmatch.fnmatch(name, "mcp:*") else None

    bridge = MagicMock()
    bridge.server_name = "s"
    schema = McpToolSchema(name="t", description="", input_schema={})
    tool = AgenthiccMcpTool(bridge, schema)

    executor = AgenthiccToolExecutor(
        event_processor=None,
        permission_checker=deny_all_mcp,
    )
    env = await executor.execute(tool, {}, {})
    assert not env.ok
    assert "permission_denied" in env.error
```

---

## 6. E2E Test Sketch (requires npx)

```python
# tests/e2e/test_mcp_e2e.py
# Skipped automatically unless AGENTHICC_MCP_E2E=1 is set.

import os
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("AGENTHICC_MCP_E2E"),
        reason="Set AGENTHICC_MCP_E2E=1 to run MCP e2e tests (requires npx)",
    ),
]


@pytest.mark.asyncio
async def test_filesystem_server_real_connection():
    """Connect to the reference MCP filesystem server and list /tmp."""
    from agenthicc.tools.mcp import McpServerConfig, McpToolRegistry

    cfg = McpServerConfig(
        name="fs",
        url="npx -y @modelcontextprotocol/server-filesystem /tmp",
        transport="stdio",
        auto_connect=True,
    )
    reg = McpToolRegistry()
    reg.register_server(cfg)
    tools = await reg.discover_all()
    await reg.shutdown()

    assert len(tools) > 0
    names = {t.name for t in tools}
    assert any("list" in n for n in names)
```

---

## Tests Checklist

| Test file | Coverage |
|---|---|
| `tests/unit/test_mcp_bridge.py` | PRD-28 types, bridge, registry unit |
| `tests/unit/test_mcp_config.py` | PRD-29 config parsing |
| `tests/unit/test_mcp_connect.py` | PRD-30 comm tool |
| `tests/integration/test_mcp_registry.py` | PRD-31 executor + registry integration |
| `tests/e2e/test_mcp_e2e.py` | Full real-server e2e (opt-in) |

---

## Verification

```bash
# Unit + integration (no npx needed)
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mcp_bridge.py \
                                 tests/unit/test_mcp_config.py \
                                 tests/unit/test_mcp_connect.py \
                                 tests/integration/test_mcp_registry.py -v

# Full suite (make sure nothing else broke)
PYTHONPATH=src .venv/bin/pytest tests/ -q --ignore=tests/e2e --tb=short

# E2E with real server
AGENTHICC_MCP_E2E=1 PYTHONPATH=src .venv/bin/pytest tests/e2e/test_mcp_e2e.py -v
```
