---
title: "PRD-29: MCP Configuration and Session Startup"
status: draft
version: 0.1.0
created: 2026-06-12
depends-on: prd-28-mcp-bridge-and-registry.md
---

# PRD-29: MCP Configuration and Session Startup

## Context

PRD-28 defines the `McpServerConfig`, `McpToolBridge`, and `McpToolRegistry` types.
This PRD specifies how those types are wired into the **configuration system**
(`config.py`) and **session startup** (`__main__.py`) so that MCP servers
declared in `agenthicc.toml` are automatically connected and their tools
registered before the first agent turn.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `ToolSettings.mcp_servers` changes type from `list[dict]` to `list[McpServerConfig]` |
| G2 | `_dict_to_config()` converts each `[[tools.mcp_servers]]` stanza into a `McpServerConfig` |
| G3 | `_run_tui_session()` instantiates `McpToolRegistry`, calls `discover_all()`, and attaches it to `renderer` |
| G4 | Plugin tools from PRD-24/25 and MCP tools are merged into a single tool list for each agent turn |
| G5 | `registry.shutdown()` is called in the session `finally` block |
| G6 | A session startup summary lists connected MCP servers and their tool counts |
| G7 | Headless mode (`--headless`) also initialises MCP servers |

## Non-Goals
- Dynamic server hot-reload mid-session
- Persisting MCP server state across sessions

---

## Files to Modify

1. **`src/agenthicc/config.py`** — `ToolSettings` type + `_dict_to_config()`
2. **`src/agenthicc/__main__.py`** — `_run_tui_session()` and `_run_agent_turn()`

---

## 1. `config.py` Changes

### `ToolSettings.mcp_servers` type change

```python
# Before
@dataclass
class ToolSettings:
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    ...

# After
from __future__ import annotations
# (use TYPE_CHECKING guard to avoid circular imports)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agenthicc.tools.mcp import McpServerConfig

@dataclass
class ToolSettings:
    mcp_servers: list[Any] = field(default_factory=list)  # list[McpServerConfig] at runtime
    ...
```

The runtime type is `list[McpServerConfig]`; we use `list[Any]` in the annotation
to avoid importing `mcp.py` at config-load time (keeps the module dependency tree clean).

### `_dict_to_config()` conversion

In the function that builds `AgenthiccConfig` from the merged TOML dict, replace
the raw dict passthrough with typed construction:

```python
# src/agenthicc/config.py — inside _dict_to_config()

def _parse_mcp_servers(raw_list: list[dict]) -> list[Any]:
    try:
        from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415
        return [McpServerConfig.from_dict(d) for d in raw_list]
    except ImportError:
        return raw_list   # fall back to raw dicts if mcp.py not available


# When building ToolSettings:
tools_raw = data.get("tools", {})
mcp_raw = tools_raw.get("mcp_servers", [])
tool_settings = ToolSettings(
    mcp_servers=_parse_mcp_servers(mcp_raw),
    plugins=tools_raw.get("plugins", []),
    allowed=tools_raw.get("allowed", []),
    denied=tools_raw.get("denied", []),
)
```

---

## 2. `__main__.py` — `_run_tui_session()` Changes

### After renderer creation and skills discovery, add MCP initialisation

```python
# ── MCP server initialisation ──────────────────────────────────────────────
_mcp_registry = None
if cfg.tools.mcp_servers:
    try:
        from agenthicc.tools.mcp import McpToolRegistry  # noqa: PLC0415
        _mcp_registry = McpToolRegistry(event_processor=processor)
        for srv_cfg in cfg.tools.mcp_servers:
            _mcp_registry.register_server(srv_cfg)
        discovered = await _mcp_registry.discover_all()
        if discovered:
            from rich.console import Console as _C  # noqa: PLC0415
            _C().print(
                f"[dim]MCP: {len(discovered)} tool(s) from "
                f"{len(cfg.tools.mcp_servers)} server(s)[/dim]"
            )
        renderer._mcp_registry = _mcp_registry
    except Exception as exc:  # noqa: BLE001
        log.error("MCP initialisation failed: %s", exc)
```

### Update `_run_tui_session()` `finally` block

```python
finally:
    proc_task.cancel()
    if ad_task is not None:
        ad_task.cancel()
    await asyncio.gather(proc_task, *([ad_task] if ad_task else []), return_exceptions=True)
    if _mcp_registry is not None:
        await _mcp_registry.shutdown()
    conv_store.close()
```

---

## 3. `__main__.py` — `_run_agent_turn()` Changes

The existing `build_registry()` call (from PRD-25) now also receives MCP tools:

```python
from agenthicc.plugins.registry import build_registry  # noqa: PLC0415

_mcp_tools = []
_mcp_reg = getattr(renderer, "_mcp_registry", None)
if _mcp_reg is not None:
    _mcp_tools = _mcp_reg.all_tools()

_registry = build_registry(
    agent_name=getattr(renderer, "_active_agent", None) or "default",
    project_plugin_tools=(
        getattr(renderer, "_project_plugin_tools", None) or []
    ) + _mcp_tools,
)
```

MCP tools are appended to `project_plugin_tools` so they go through the same
deduplication and precedence logic in `ToolRegistry`.

---

## Configuration Reference

```toml
# .agenthicc/agenthicc.toml

[[tools.mcp_servers]]
name       = "filesystem"
url        = "npx -y @modelcontextprotocol/server-filesystem /workspace"
transport  = "stdio"
auto_connect         = true
reconnect_attempts   = 3
reconnect_delay_seconds = 1.0

[[tools.mcp_servers]]
name      = "github"
url       = "wss://github-mcp.example.com"
transport = "ws"
token     = "${GITHUB_MCP_TOKEN}"
auto_connect = true

[[tools.mcp_servers]]
name         = "internal_search"
url          = "https://search.internal/mcp"
transport    = "streamable"
token        = "${SEARCH_API_TOKEN}"
auto_connect = false    # connect on demand via mcp_connect comm tool
```

---

## Startup Log Output

```
 ~ agenthicc==0.1.0
 Skills loaded: /git-summary, /deploy
 Tool plugins: 3 tool(s) from .agenthicc/tools/
 MCP: 14 tool(s) from 2 server(s)
 openai/...  │  0 turns  │  $0.000
```

---

## Tests

```python
# tests/unit/test_mcp_config.py

import pytest
from agenthicc.config import ToolSettings

pytestmark = pytest.mark.unit


def test_tool_settings_default_empty():
    ts = ToolSettings()
    assert ts.mcp_servers == []


def test_parse_mcp_servers_from_dict_list():
    from agenthicc.tools.mcp import McpServerConfig
    from agenthicc.config import _parse_mcp_servers
    raw = [{"name": "x", "url": "y", "transport": "stdio"}]
    result = _parse_mcp_servers(raw)
    assert len(result) == 1
    assert isinstance(result[0], McpServerConfig)
    assert result[0].name == "x"


def test_parse_mcp_servers_graceful_on_missing_import(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "agenthicc.tools.mcp", None)
    from agenthicc.config import _parse_mcp_servers
    raw = [{"name": "x", "url": "y"}]
    result = _parse_mcp_servers(raw)
    assert result == raw   # falls back to raw dicts
```

---

## Verification

```bash
PYTHONPATH=src .venv/bin/pytest tests/unit/test_mcp_config.py -v

# Full session with a real MCP server (requires npx):
cat >> .agenthicc/agenthicc.toml << 'EOF'
[[tools.mcp_servers]]
name = "fs"
url  = "npx -y @modelcontextprotocol/server-filesystem /tmp"
transport = "stdio"
auto_connect = true
EOF
uv run agenthicc
# → "MCP: N tool(s) from 1 server(s)" in startup output
# Ask: "what tools do you have?" → agent lists mcp:fs:* tools
```
