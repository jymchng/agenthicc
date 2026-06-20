# PRD-108 — HTTP Timeout Safety: Preventing ReadTimeout Tool Failures

## Summary

Several agenthicc tools make outbound HTTP calls with inconsistent or missing
timeout configuration.  A `ReadTimeout` from any of these tools currently
propagates uncaught to the agent turn layer, ending the entire turn instead of
returning a clean error result to the agent.

This PRD introduces a shared HTTP client factory, a centralized timeout config
field, and per-tool network error boundaries so that a slow API never kills a
turn.

---

## Problem Statement

### Tools that propagate ReadTimeout uncaught to the turn layer

| Tool | File | Timeout | Endpoint |
|---|---|---|---|
| `SearchWebTool._brave_search` | `skills/web_search.py:40` | 10 s per-request | `api.search.brave.com` |
| `GraphApiOutlookBackend._get` | `tools/outlook/__init__.py:48` | 15 s per-request | `graph.microsoft.com` |
| `GraphApiOutlookBackend._post` | `tools/outlook/__init__.py:55` | 15 s per-request | `graph.microsoft.com` |
| `McpToolBridge.call_tool` | `tools/mcp.py` | Library default (unknown) | MCP server endpoints |

### Auth calls that crash the CLI with unhandled ReadTimeout

| Site | File | Timeout | Problem |
|---|---|---|---|
| `_exchange_code` | `auth.py:197` | None — httpx default 5 s | No try/except; slow server crashes login |
| `_refresh` | `auth.py:218` | None — httpx default 5 s | No try/except; slow server crashes session start |

### No configurable HTTP timeout in AgenthiccConfig

`ToolSettings` has no `http_timeout_s` field.  Users on slow or throttled
networks have no knob to adjust HTTP timeouts across all tools.

---

## Design — Five Layers

### Layer 1 — Shared HTTP client factory (`tools/http.py`)

A new `tools/http.py` module provides:

```python
@asynccontextmanager
async def agenthicc_http_client(
    *, timeout: float | None = None, follow_redirects: bool = True
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Yield a configured AsyncClient. timeout defaults to ToolSettings.http_timeout_s."""

def configure(timeout_s: float) -> None:
    """Set the module-level default timeout (called at session startup)."""

def is_network_error(exc: BaseException) -> bool:
    """Return True for httpx.TimeoutException, httpx.HTTPError, ConnectionError, etc."""
```

All tools that make HTTP calls switch to `agenthicc_http_client()`.  Timeout
is configured once at startup from config; every tool inherits it.

The `httpx.Timeout` object always sets `connect=10.0` regardless of the read
timeout — a hung connection is a hard failure, but a slow read may succeed with
more time.

### Layer 2 — Per-tool network error boundary

Tools that make network calls wrap their HTTP logic in:

```python
try:
    async with agenthicc_http_client() as client:
        ...
except Exception as exc:
    if is_network_error(exc):
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "recoverable": True}
    raise
```

`recoverable: True` signals to the agent that it can retry or move on.  The
turn continues; only this tool call fails.

### Layer 3 — `ToolSettings.http_timeout_s` in config

```toml
# agenthicc.toml
[tools]
http_timeout_s = 30.0   # read timeout for all tool HTTP calls; 0 = no limit
```

```python
@dataclass
class ToolSettings:
    ...
    http_timeout_s: float = 30.0
```

`_build_session_context()` calls `tools.http.configure(cfg.tools.http_timeout_s)` immediately
after loading config.

### Layer 4 — Hardened auth.py

`_exchange_code()` and `_refresh()` gain:
- Explicit `timeout=15.0` on the httpx client
- `try/except` that converts `httpx.TimeoutException` / `httpx.HTTPError` to
  `AuthNetworkError`, a new exception class that the CLI login command catches
  and presents as a human-readable message — not a raw traceback

### Layer 5 — LLM transport stall (`turn_timeout_s`)

The LLM transport has no per-request timeout exposed through config.  The
`execution.turn_timeout_s` watchdog (PRD-107) is the correct coarse guard.
Default remains `0.0` (disabled) to preserve existing behaviour; documentation
recommends `120.0` for production deployments.

---

## Acceptance Criteria

| # | Requirement |
|---|---|
| 1 | `agenthicc_http_client()` sets both read and connect timeouts from a single config value. |
| 2 | `configure(timeout_s)` changes the module-level default used by all future clients. |
| 3 | `is_network_error()` returns True for `httpx.ReadTimeout`, `httpx.ConnectTimeout`, `httpx.HTTPError`, `TimeoutError`, `ConnectionError`. |
| 4 | `SearchWebTool.execute()` returns `{"ok": False, "error": "ReadTimeout: ...", "recoverable": True}` on network error — does not raise. |
| 5 | `GraphApiOutlookBackend._get()` and `_post()` return `{"ok": False, ...}` on network error. |
| 6 | `ToolSettings.http_timeout_s = 30.0` is the default read timeout. |
| 7 | `_build_session_context()` calls `tools.http.configure()` after loading config. |
| 8 | `auth._exchange_code()` raises `AuthNetworkError` (not bare `ReadTimeout`) on network failure. |
| 9 | `auth._refresh()` raises `AuthNetworkError` on network failure. |
| 10 | No tool can propagate `httpx.ReadTimeout` to the agent turn layer without first returning a clean error dict. |
| 11 | `[tools] http_timeout_s = 0.0` disables the read timeout (unbounded). |

---

## Files Changed

| File | Change |
|---|---|
| `tools/http.py` | New — shared HTTP client factory |
| `config.py` | `ToolSettings.http_timeout_s: float = 30.0` |
| `runners/tui_session.py` | `tools.http.configure()` at session startup |
| `skills/web_search.py` | `SearchWebTool` uses `agenthicc_http_client` + network boundary |
| `tools/outlook/__init__.py` | `_get`/`_post` use `agenthicc_http_client` + network boundary |
| `auth.py` | Explicit timeout + `AuthNetworkError` in `_exchange_code`/`_refresh` |
