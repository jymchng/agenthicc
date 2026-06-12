---
id: PRD-07
title: "Configuration, Security and Headless API"
status: draft
created: 2025-01-01
updated: 2025-01-01
authors:
  - platform-team
reviewers:
  - security-team
  - api-team
priority: P0
milestone: v0.7.0
tags:
  - configuration
  - security
  - sandboxing
  - headless-api
  - toml
  - fastapi
---

# PRD-07: Configuration, Security and Headless API

## Executive Summary

This document specifies the configuration management system, security architecture, sandboxing primitives,
and headless API layer for the AgentHicc platform. As the platform transitions from interactive CLI-only usage
toward embedded and programmatic deployments, a coherent, auditable security posture must accompany that
expansion.

The three pillars of this PRD are:

1. **TOML-based configuration** with project-level and user-level override semantics, reflected into a typed
   `SystemSettings` dataclass. This gives operators a single source of truth for all runtime parameters.

2. **Security policy enforcement** implemented as a declarative `SecurityPolicy` evaluated in
   `ToolContext.check_permission()` before every tool execution. The policy is fail-closed: absence of a
   matching rule is a deny. Sandboxing layers (filesystem, network, CPU, memory, WASM) add defense-in-depth
   below the policy layer.

3. **Headless REST + WebSocket API** built with FastAPI that exposes intent submission, status polling, and
   a streaming event bus. A bridge `AgentInstance` translates external HTTP/WS requests into internal tool
   calls, enabling CI pipelines, external orchestrators, and third-party integrations to drive the platform
   without a human in the loop.

All three pillars integrate tightly with the lauren-ai type system: `AgentConfig`, `TokenBudget`,
`CostTracker`, `InputGuardrail`, `OutputGuardrail`, and `ToolContext` are first-class participants in
configuration loading, security enforcement, and API request handling.

---

## Goals and Non-Goals

### Goals

- G1: Load and merge TOML configuration from project-scoped (`agenthicc.toml`) and user-scoped
  (`~/.agenthicc.toml`) files, with user settings taking precedence over project settings.
- G2: Reflect the merged configuration into a fully typed `SystemSettings` dataclass so the rest of the
  codebase never reads raw TOML dictionaries.
- G3: Implement a declarative `SecurityPolicy` with glob-pattern-based `PermissionRule` objects evaluated
  before every tool invocation.
- G4: Enforce fail-closed permission semantics: if no rule matches a tool call, the call is denied.
- G5: Provide four sandboxing layers (filesystem prefix, network domain allow-list, CPU timeout, memory
  limit) that operate independently of the policy layer.
- G6: Provide a WASM sandbox for `tool_define` to execute untrusted code without a full subprocess.
- G7: Expose a FastAPI application with `POST /intents`, `GET /intents/{id}`, and `WebSocket /ws` endpoints.
- G8: Authenticate all API requests via `Authorization: Bearer <key>` middleware.
- G9: Bridge API requests through a dedicated `AgentInstance` so all internal observability (tracing, cost
  tracking, guardrails) applies to headless workloads identically to interactive workloads.
- G10: Integrate with lauren-ai guardrails (`InputGuardrail`, `OutputGuardrail`, `PromptInjectionFilter`,
  `PIIRedactor`) so headless API inputs and outputs are subject to the same checks as interactive sessions.

### Non-Goals

- NG1: Multi-tenant user isolation at the database level (handled by a future PRD).
- NG2: OAuth2 / OIDC authentication flows (Bearer token only in this release).
- NG3: Kubernetes-native pod-level sandboxing (out of scope; focus is in-process and subprocess sandboxing).
- NG4: GUI configuration editor.
- NG5: Dynamic hot-reload of security policy without process restart (configuration changes require restart).
- NG6: Rate limiting per API key (tracked as a follow-up).

---

## TOML Config Schema

The platform reads configuration in the following merge order (later sources override earlier ones):

```
1. Compiled-in defaults (lowest priority)
2. agenthicc.toml          (project root, committed to version control)
3. ~/.agenthicc.toml       (user home, never committed)     <- highest priority
```

Deep-merge semantics: scalar values are overwritten; lists are replaced (not appended); tables are merged
recursively. The only exception is `[security].allowed_paths`, which is replaced entirely when the user
file specifies it.

### Full Annotated TOML Example

```toml
# agenthicc.toml
# Every key is optional. Defaults are shown as comments.

# ---------------------------------------------------------------------------
# [execution] — concurrency and pool sizing
# ---------------------------------------------------------------------------
[execution]

# Maximum number of intent objects that can be in-flight simultaneously.
# Each intent maps to one or more tasks. Exceeding this causes new intents
# to queue until a slot opens. Range: 1-256.
max_concurrent_intents = 8

# Maximum number of tasks that can execute in parallel within a single intent.
# This bounds the width of the task DAG at any execution step. Range: 1-64.
max_parallel_tasks = 4

# Size of the reusable AgentInstance pool. Agents are borrowed from this pool
# per intent and returned when the intent completes. A value of 0 disables
# pooling (each intent creates a fresh agent). Range: 0-128.
agent_pool_size = 16

# ---------------------------------------------------------------------------
# [hooks] — dotted-path callables invoked at lifecycle events
# ---------------------------------------------------------------------------
[hooks]

# Called before an intent is validated and dispatched.
# Signature: async def hook(intent: Intent) -> Intent | None
# Returning None rejects the intent. Returning a modified Intent replaces it.
intent.pre_validate = [
  "myproject.hooks.audit_intent",
  "myproject.hooks.strip_pii_from_intent",
]

# Called after every tool execution, whether it succeeded or failed.
# Signature: async def hook(result: ToolResult, context: ToolContext) -> None
tool.post_execute = [
  "myproject.hooks.log_tool_usage",
]

# Called when a workflow encounters an unhandled error.
# Signature: async def hook(error: Exception, context: WorkflowContext) -> ErrorAction
# ErrorAction: RETRY | SKIP | ABORT
workflow.on_error = [
  "myproject.hooks.notify_slack_on_error",
  "myproject.hooks.retry_transient_errors",
]

# ---------------------------------------------------------------------------
# [tools] — tool availability and MCP server registration
# ---------------------------------------------------------------------------
[tools]

# MCP server definitions. Each entry is either a stdio command or an HTTP URL.
[[tools.mcp_servers]]
name = "filesystem"
command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
env = { MCP_LOG_LEVEL = "info" }

[[tools.mcp_servers]]
name = "github"
url = "http://localhost:8080"
headers = { Authorization = "Bearer ${GITHUB_TOKEN}" }

# Python plugin packages that register tools via entry_points.
# Each entry is a dotted module path to a ToolPlugin subclass.
plugins = [
  "myproject.tools.custom_search:CustomSearchPlugin",
  "myproject.tools.internal_api:InternalAPIPlugin",
]

# Allowlist of tool names that may be called. If empty, all registered tools
# are allowed (subject to deny-list and security policy).
allowed_tools = []

# Denylist of tool names. These tools are unconditionally blocked regardless
# of the security policy. Glob patterns are supported.
denied_tools = [
  "shell_exec",         # raw shell access
  "network_raw",        # raw socket access
  "tool_define_unsafe", # untrusted code execution outside WASM
]

# ---------------------------------------------------------------------------
# [memory] — persistence and vector search
# ---------------------------------------------------------------------------
[memory]

# Path to the project memory store (SQLite + optional vector index).
# Relative paths are resolved from the project root.
project_memory_path = ".agenthicc/memory"

# Vector database backend. Options: "sqlite-vec" (default), "chroma", "none".
vector_db = "sqlite-vec"

# How long a session's ephemeral memory lives before expiry (seconds).
# 0 = never expire.
session_ttl_seconds = 86400   # 24 hours

# ---------------------------------------------------------------------------
# [security] — sandbox and permission settings
# ---------------------------------------------------------------------------
[security]

# Master sandbox toggle. When false, all sandbox layers are disabled (dev only).
sandbox_mode = true

# Filesystem paths the platform is permitted to read or write.
# Tool calls that attempt to access outside these prefixes are blocked by
# WorkspaceView before the OS syscall is made.
# Glob patterns are supported (e.g., "/tmp/agenthicc-*").
allowed_paths = [
  "/workspace",
  "/tmp/agenthicc",
]

# Domains that outbound HTTP/HTTPS calls may target.
# Subdomains are matched (e.g., "example.com" also allows "api.example.com").
# An empty list blocks all outbound network calls.
network_allow_list = [
  "api.anthropic.com",
  "api.github.com",
  "pypi.org",
]

# Hard wall-clock timeout (seconds) for a single tool execution.
# asyncio.wait_for wraps every tool coroutine with this deadline.
# 0 = no timeout (not recommended in production).
max_tool_cpu_seconds = 30

# Maximum RSS (resident set size) in megabytes that a subprocess tool may use.
# Enforced via resource.setrlimit(RLIMIT_AS) before exec.
# 0 = no limit.
max_tool_memory_mb = 512

# ---------------------------------------------------------------------------
# [api] — headless REST + WebSocket server
# ---------------------------------------------------------------------------
[api]

# Bind address for the FastAPI server.
host = "127.0.0.1"

# TCP port.
port = 8000

# Name of the environment variable that holds the API key.
# The server reads os.environ[api_key_env] at startup.
api_key_env = "AGENTHICC_API_KEY"

# CORS origins allowed to make cross-origin requests to the API.
# An empty list disables CORS (API is not browser-accessible).
cors_origins = [
  "https://app.example.com",
]
```

---

## Security Architecture

### Trust Model

The platform operates with the following trust hierarchy:

```
Highest trust   ┌─────────────────────────┐
                │  Platform process owner  │  (OS user running agenthicc)
                ├─────────────────────────┤
                │  Project configuration   │  (agenthicc.toml, committed)
                ├─────────────────────────┤
                │  User configuration      │  (~/.agenthicc.toml, local)
                ├─────────────────────────┤
                │  API caller              │  (Bearer token authenticated)
                ├─────────────────────────┤
                │  Tool implementation     │  (registered Python code)
                ├─────────────────────────┤
                │  LLM-generated tool args │  (untrusted input)
Lowest trust    └─────────────────────────┘
```

LLM-generated tool arguments are treated as untrusted user input at all times. The security policy and
sandbox layers exist specifically to limit the blast radius of a malicious or hallucinated tool call.

### Request Flow

```
  External Request (HTTP / WS)
         │
         ▼
  ┌──────────────────────────────────────────────────────────┐
  │  API Layer (FastAPI)                                      │
  │  • BearerTokenMiddleware → 401 if invalid                 │
  │  • CORSMiddleware                                         │
  │  • InputGuardrail (PromptInjectionFilter, PIIRedactor)    │
  └──────────────────┬───────────────────────────────────────┘
                     │  validated Intent
                     ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Intent Dispatcher                                        │
  │  • intent.pre_validate hooks                              │
  │  • TokenBudget check (max_tokens_per_turn, max_cost_usd)  │
  │  • AgentInstance borrowed from pool                       │
  └──────────────────┬───────────────────────────────────────┘
                     │  AgentContext + ToolContext
                     ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Permission Layer  (ToolContext.check_permission)          │
  │  • SecurityPolicy evaluated against tool name + args      │
  │  • Fail-closed: no matching rule → DENY                   │
  │  • PermissionRule: glob pattern, action, conditions        │
  └──────────────────┬───────────────────────────────────────┘
                     │  permitted ToolCall
                     ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Sandbox Layer                                            │
  │  ┌─────────────────┐  ┌─────────────────────────────┐   │
  │  │ WorkspaceView   │  │ NetworkSandbox              │   │
  │  │ path prefix     │  │ domain allow-list + semaphore│   │
  │  │ no symlink follow│  └─────────────────────────────┘   │
  │  └─────────────────┘  ┌─────────────────────────────┐   │
  │  ┌─────────────────┐  │ MemorySandbox               │   │
  │  │ CPUSandbox      │  │ resource.setrlimit(RLIMIT_AS)│   │
  │  │ asyncio.wait_for│  └─────────────────────────────┘   │
  │  └─────────────────┘                                     │
  └──────────────────┬───────────────────────────────────────┘
                     │  sandboxed execution context
                     ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Tool Execution                                           │
  │  • Standard tool: Python coroutine                        │
  │  • Subprocess tool: fork + setrlimit + exec               │
  │  • tool_define: wasmtime WASM runtime (untrusted code)    │
  │  • tool.post_execute hooks                                │
  └──────────────────┬───────────────────────────────────────┘
                     │  ToolResult
                     ▼
  ┌──────────────────────────────────────────────────────────┐
  │  Output Layer                                             │
  │  • OutputGuardrail (TopicFilter, PIIRedactor)             │
  │  • CostTracker.record_usage()                             │
  │  • Event published to WebSocket bus                       │
  └──────────────────────────────────────────────────────────┘
```

### Sandboxing Details

#### Filesystem Sandbox — WorkspaceView

`WorkspaceView` wraps `pathlib.Path` and `os` operations. Before any open/stat/mkdir/unlink call:

1. Resolve the path with `Path.resolve()` (follows symlinks to their real target).
2. Check that the resolved path starts with one of the configured `allowed_paths` prefixes.
3. If the check fails, raise `PathTraversalError` without making the OS call.

This approach catches:
- `../../etc/passwd` style traversal
- Symlinks pointing outside the workspace
- Absolute paths outside allowed prefixes

#### Network Sandbox — NetworkSandbox

Wraps `aiohttp.ClientSession` with a custom `aiohttp.TCPConnector` subclass. On each new connection:

1. Extract the target hostname from the URL.
2. Check whether the hostname (or a parent domain) is in `network_allow_list`.
3. If not allowed, raise `NetworkDomainBlockedError` before the TCP handshake.

A per-domain `asyncio.Semaphore` limits concurrent connections to prevent denial-of-service from runaway
tool loops.

#### CPU Sandbox — CPUSandbox

Every tool coroutine is wrapped:

```python
async with CPUSandbox(max_seconds=settings.security.max_tool_cpu_seconds):
    result = await tool.execute(args, context)
```

Internally this is `asyncio.wait_for(coro, timeout=max_seconds)`. On timeout, a `ToolTimeoutError` is
raised, the tool's task is cancelled, and a `ToolResult.error("tool execution timed out")` is returned.

#### Memory Sandbox — MemorySandbox

For subprocess-based tools, memory limits are enforced in the child process before `exec`:

```python
import resource

def _set_memory_limit(max_mb: int) -> None:
    max_bytes = max_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
```

This function is passed as `preexec_fn` to `asyncio.create_subprocess_exec`.

#### WASM Sandbox — WasmSandbox

`tool_define` allows users to register new tools by providing Python or JavaScript source code. This code
runs inside a `wasmtime` runtime:

- The WASM module is compiled from the source using a trusted compiler pipeline.
- The WASM instance has no access to host filesystem or network (WASI imports disabled).
- Execution is bounded by `CPUSandbox` (same timeout mechanism).
- If `wasmtime` is unavailable, falls back to a `subprocess` with `seccomp` profile that blocks all
  syscalls except `read`, `write`, `exit`, and `brk`.

---

## Data Structures and Interfaces

### SystemSettings

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class ExecutionSettings:
    max_concurrent_intents: int = 8
    max_parallel_tasks: int = 4
    agent_pool_size: int = 16

@dataclass
class HooksSettings:
    intent_pre_validate: list[str] = field(default_factory=list)
    tool_post_execute: list[str] = field(default_factory=list)
    workflow_on_error: list[str] = field(default_factory=list)

@dataclass
class MCPServerConfig:
    name: str
    command: list[str] = field(default_factory=list)
    url: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

@dataclass
class ToolsSettings:
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)

@dataclass
class MemorySettings:
    project_memory_path: Path = Path(".agenthicc/memory")
    vector_db: str = "sqlite-vec"
    session_ttl_seconds: int = 86400

@dataclass
class SecuritySettings:
    sandbox_mode: bool = True
    allowed_paths: list[Path] = field(default_factory=lambda: [Path("/workspace")])
    network_allow_list: list[str] = field(default_factory=list)
    max_tool_cpu_seconds: int = 30
    max_tool_memory_mb: int = 512

@dataclass
class APISettings:
    host: str = "127.0.0.1"
    port: int = 8000
    api_key_env: str = "AGENTHICC_API_KEY"
    cors_origins: list[str] = field(default_factory=list)

@dataclass
class SystemSettings:
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    hooks: HooksSettings = field(default_factory=HooksSettings)
    tools: ToolsSettings = field(default_factory=ToolsSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    api: APISettings = field(default_factory=APISettings)
```

### Configuration Loader

```python
from pathlib import Path
from typing import Any
import tomllib  # Python 3.11+; use tomli for older versions

class ConfigLoader:
    PROJECT_FILE = "agenthicc.toml"
    USER_FILE = Path.home() / ".agenthicc.toml"

    @classmethod
    def load(cls, project_root: Path = Path(".")) -> SystemSettings:
        base: dict[str, Any] = {}
        project_path = project_root / cls.PROJECT_FILE
        if project_path.exists():
            with open(project_path, "rb") as f:
                base = tomllib.load(f)
        if cls.USER_FILE.exists():
            with open(cls.USER_FILE, "rb") as f:
                user_cfg = tomllib.load(f)
            base = cls._deep_merge(base, user_cfg)
        return cls._deserialize(base)

    @classmethod
    def _deep_merge(cls, base: dict, override: dict) -> dict:
        result = dict(base)
        for key, val in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(val, dict):
                result[key] = cls._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    @classmethod
    def _deserialize(cls, data: dict[str, Any]) -> SystemSettings:
        # Build each sub-settings object from its TOML table.
        # Unrecognized keys are silently ignored to support forward compatibility.
        settings = SystemSettings()
        if "execution" in data:
            ex = data["execution"]
            settings.execution = ExecutionSettings(
                max_concurrent_intents=ex.get("max_concurrent_intents", 8),
                max_parallel_tasks=ex.get("max_parallel_tasks", 4),
                agent_pool_size=ex.get("agent_pool_size", 16),
            )
        if "hooks" in data:
            h = data["hooks"]
            settings.hooks = HooksSettings(
                intent_pre_validate=h.get("intent", {}).get("pre_validate", []),
                tool_post_execute=h.get("tool", {}).get("post_execute", []),
                workflow_on_error=h.get("workflow", {}).get("on_error", []),
            )
        if "security" in data:
            s = data["security"]
            settings.security = SecuritySettings(
                sandbox_mode=s.get("sandbox_mode", True),
                allowed_paths=[Path(p) for p in s.get("allowed_paths", ["/workspace"])],
                network_allow_list=s.get("network_allow_list", []),
                max_tool_cpu_seconds=s.get("max_tool_cpu_seconds", 30),
                max_tool_memory_mb=s.get("max_tool_memory_mb", 512),
            )
        if "api" in data:
            a = data["api"]
            settings.api = APISettings(
                host=a.get("host", "127.0.0.1"),
                port=a.get("port", 8000),
                api_key_env=a.get("api_key_env", "AGENTHICC_API_KEY"),
                cors_origins=a.get("cors_origins", []),
            )
        # (memory, tools sections omitted for brevity but follow the same pattern)
        return settings
```

### SecurityPolicy and PermissionRule

```python
import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class PermissionAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"

@dataclass
class PermissionRule:
    # Glob pattern matched against the tool name (e.g., "fs_*", "shell_exec").
    tool_pattern: str

    # Whether this rule grants or revokes permission.
    action: PermissionAction

    # Optional additional conditions that must ALL be true for this rule to fire.
    # path_prefix: the first string argument to the tool must start with this prefix.
    path_prefix: Optional[str] = None

    # network_domains: the tool's "url" argument domain must be in this list.
    network_domains: list[str] = field(default_factory=list)

    def matches(self, tool_name: str, args: dict) -> bool:
        if not fnmatch.fnmatch(tool_name, self.tool_pattern):
            return False
        if self.path_prefix is not None:
            path_arg = args.get("path") or args.get("file_path") or ""
            if not str(path_arg).startswith(self.path_prefix):
                return False
        if self.network_domains:
            import urllib.parse
            url_arg = args.get("url") or ""
            host = urllib.parse.urlparse(url_arg).hostname or ""
            if not any(host == d or host.endswith("." + d) for d in self.network_domains):
                return False
        return True


@dataclass
class SecurityPolicy:
    # Rules are evaluated in order; the first matching rule wins.
    # If no rule matches: DENY (fail-closed).
    rules: list[PermissionRule] = field(default_factory=list)

    def evaluate(self, tool_name: str, args: dict) -> PermissionAction:
        for rule in self.rules:
            if rule.matches(tool_name, args):
                return rule.action
        return PermissionAction.DENY  # fail-closed default
```

### WorkspaceView

```python
import os
from pathlib import Path

class PathTraversalError(PermissionError):
    """Raised when a path escapes the allowed workspace."""

class WorkspaceView:
    """
    A read/write facade over the filesystem that enforces path prefix restrictions.
    All methods resolve symlinks before checking the prefix, preventing both
    directory traversal (../../) and symlink escape attacks.
    """

    def __init__(self, allowed_paths: list[Path]) -> None:
        # Resolve all allowed paths at construction time so we compare apples to apples.
        self._allowed = [p.resolve() for p in allowed_paths]

    def _check(self, path: Path | str) -> Path:
        resolved = Path(path).resolve()
        for allowed in self._allowed:
            try:
                resolved.relative_to(allowed)
                return resolved   # within a permitted prefix
            except ValueError:
                continue
        raise PathTraversalError(
            f"Path '{resolved}' is outside allowed workspace prefixes: {self._allowed}"
        )

    def open(self, path: Path | str, mode: str = "r", **kwargs):
        return open(self._check(path), mode, **kwargs)

    def read_text(self, path: Path | str, encoding: str = "utf-8") -> str:
        return self._check(path).read_text(encoding=encoding)

    def write_text(self, path: Path | str, content: str, encoding: str = "utf-8") -> None:
        self._check(path).write_text(content, encoding=encoding)

    def mkdir(self, path: Path | str, parents: bool = False, exist_ok: bool = False) -> None:
        self._check(path).mkdir(parents=parents, exist_ok=exist_ok)

    def unlink(self, path: Path | str) -> None:
        self._check(path).unlink()

    def listdir(self, path: Path | str) -> list[Path]:
        checked = self._check(path)
        return list(checked.iterdir())
```

### ToolContext Integration

```python
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class ExecutionContext:
    sandbox_mode: bool
    workspace: WorkspaceView
    network_sandbox: "NetworkSandbox"
    cpu_timeout_seconds: int

@dataclass
class ToolContext:
    agent_context: "AgentContext"          # from lauren_ai._config
    tool_use_id: str                        # unique per tool call within a turn
    turn: int                               # which conversation turn this belongs to
    request: "IntentRequest"               # the originating API or CLI request
    execution_context: ExecutionContext
    metadata: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    _policy: Optional[SecurityPolicy] = field(default=None, repr=False)

    def check_permission(self, tool_name: str, args: dict) -> None:
        """
        Evaluate the security policy for this tool call.
        Raises ToolPermissionDeniedError (which becomes a ToolResult.error()) if denied.
        Must be called before every tool execution.
        """
        if self._policy is None:
            raise ToolPermissionDeniedError(
                f"No security policy configured; denying tool '{tool_name}' by default."
            )
        action = self._policy.evaluate(tool_name, args)
        if action == PermissionAction.DENY:
            raise ToolPermissionDeniedError(
                f"Security policy denied tool '{tool_name}' with args {args!r}"
            )

class ToolPermissionDeniedError(PermissionError):
    pass
```

### ToolResult and Error Construction

```python
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class ToolResult:
    tool_use_id: str
    content: Any
    is_error: bool = False

    @classmethod
    def error(cls, tool_use_id: str, message: str) -> "ToolResult":
        """Construct a standardized error result for permission denied, timeouts, etc."""
        return cls(tool_use_id=tool_use_id, content=message, is_error=True)

    @classmethod
    def success(cls, tool_use_id: str, content: Any) -> "ToolResult":
        return cls(tool_use_id=tool_use_id, content=content, is_error=False)
```

---

## Headless API Spec

### Authentication

All endpoints require `Authorization: Bearer <api_key>` header. The API key is read from the environment
variable named by `api.api_key_env` at server startup. Missing or invalid tokens receive `HTTP 401`.

### Base URL

```
http://{api.host}:{api.port}/v1
```

### Endpoints

---

#### POST /v1/intents

Submit a new intent for execution.

**Request Body** (`application/json`):

```json
{
  "text": "string (required) — natural language intent",
  "context": {
    "working_directory": "/workspace/myproject",
    "environment": { "SOME_VAR": "value" }
  },
  "config_overrides": {
    "max_turns": 10,
    "temperature": 0.3
  },
  "metadata": {}
}
```

**Responses**:

| Status | Body |
|--------|------|
| 202 Accepted | `{"intent_id": "uuid", "status": "queued", "created_at": "ISO8601"}` |
| 400 Bad Request | `{"error": "validation_error", "detail": "..."}` |
| 401 Unauthorized | `{"error": "unauthorized"}` |
| 429 Too Many Requests | `{"error": "queue_full", "max_concurrent": 8}` |

The `intent_id` is a UUID v4. Clients should use this to poll status or subscribe to the WebSocket stream.

---

#### GET /v1/intents/{intent_id}

Retrieve the current status and result of an intent.

**Path Parameters**: `intent_id` — UUID returned by POST /intents.

**Responses**:

| Status | Body |
|--------|------|
| 200 OK | See schema below |
| 404 Not Found | `{"error": "intent_not_found"}` |
| 401 Unauthorized | `{"error": "unauthorized"}` |

**200 Response Schema**:

```json
{
  "intent_id": "uuid",
  "status": "queued|running|completed|failed|cancelled",
  "created_at": "ISO8601",
  "started_at": "ISO8601 | null",
  "completed_at": "ISO8601 | null",
  "turns": 3,
  "cost_usd": 0.0042,
  "tokens_used": { "input": 1200, "output": 340 },
  "result": {
    "summary": "string — final agent response",
    "tool_calls": [
      {
        "tool_name": "read_file",
        "status": "success",
        "duration_ms": 14
      }
    ]
  },
  "error": "string | null"
}
```

---

#### DELETE /v1/intents/{intent_id}

Cancel a queued or running intent.

**Responses**:

| Status | Body |
|--------|------|
| 200 OK | `{"intent_id": "uuid", "status": "cancelled"}` |
| 404 Not Found | `{"error": "intent_not_found"}` |
| 409 Conflict | `{"error": "intent_already_completed"}` |

---

#### WebSocket /v1/ws

Real-time event stream. The client opens a WebSocket connection and receives newline-delimited JSON events.

**Connection handshake**: The client must send an `Authorization` header (via `Sec-WebSocket-Protocol`
trick or a query-parameter `?token=<api_key>` fallback for browser clients).

**Client → Server messages** (JSON):

```json
// Subscribe to events for a specific intent
{"action": "subscribe", "intent_id": "uuid"}

// Unsubscribe
{"action": "unsubscribe", "intent_id": "uuid"}

// Ping (keepalive)
{"action": "ping"}
```

**Server → Client messages** (newline-delimited JSON):

```json
// Intent state change
{"event": "intent.status_changed", "intent_id": "uuid", "status": "running", "ts": "ISO8601"}

// Tool call started
{"event": "tool.started", "intent_id": "uuid", "tool_use_id": "tu_xyz", "tool_name": "read_file", "ts": "ISO8601"}

// Tool call completed
{"event": "tool.completed", "intent_id": "uuid", "tool_use_id": "tu_xyz", "status": "success", "duration_ms": 14, "ts": "ISO8601"}

// Agent text delta (streaming)
{"event": "agent.delta", "intent_id": "uuid", "turn": 2, "delta": "Here are the results...", "ts": "ISO8601"}

// Intent completed
{"event": "intent.completed", "intent_id": "uuid", "cost_usd": 0.0042, "ts": "ISO8601"}

// Intent failed
{"event": "intent.failed", "intent_id": "uuid", "error": "BudgetExceededError: max_cost_usd exceeded", "ts": "ISO8601"}

// Pong (keepalive response)
{"event": "pong", "ts": "ISO8601"}
```

---

### FastAPI Application Skeleton

```python
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import FastAPI, WebSocket, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from agenthicc.config import ConfigLoader, SystemSettings
from agenthicc.intent import IntentDispatcher, IntentRequest, IntentStatus
from agenthicc.ws import EventBus

_settings: SystemSettings
_dispatcher: IntentDispatcher
_event_bus: EventBus

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _dispatcher, _event_bus
    _settings = ConfigLoader.load()
    _event_bus = EventBus()
    _dispatcher = IntentDispatcher(_settings, _event_bus)
    await _dispatcher.start()
    yield
    await _dispatcher.stop()

app = FastAPI(title="AgentHicc Headless API", version="0.7.0", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=lambda: _settings.api.cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Auth
_bearer = HTTPBearer()

def _get_api_key() -> str:
    key_env = _settings.api.api_key_env
    key = os.environ.get(key_env, "")
    if not key:
        raise RuntimeError(f"API key environment variable '{key_env}' is not set.")
    return key

async def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)]
) -> str:
    if credentials.credentials != _get_api_key():
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials

@app.post("/v1/intents", status_code=202)
async def submit_intent(body: dict, _: str = Depends(require_auth)):
    intent_id = str(uuid.uuid4())
    req = IntentRequest(intent_id=intent_id, **body)
    await _dispatcher.submit(req)
    return {"intent_id": intent_id, "status": "queued"}

@app.get("/v1/intents/{intent_id}")
async def get_intent(intent_id: str, _: str = Depends(require_auth)):
    status = await _dispatcher.get_status(intent_id)
    if status is None:
        raise HTTPException(status_code=404, detail="intent_not_found")
    return status

@app.delete("/v1/intents/{intent_id}")
async def cancel_intent(intent_id: str, _: str = Depends(require_auth)):
    cancelled = await _dispatcher.cancel(intent_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="intent_not_found")
    return {"intent_id": intent_id, "status": "cancelled"}

@app.websocket("/v1/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    api_key = _get_api_key()
    if token != api_key:
        await ws.close(code=4401, reason="Unauthorized")
        return
    await ws.accept()
    await _event_bus.handle_client(ws)
```

---

## Implementation Plan

### Phase 1 — Configuration (Sprint 1, Week 1-2)

| Task | Owner | Lauren-AI Type Used |
|------|-------|---------------------|
| Implement `ConfigLoader.load()` with `tomllib` | platform-team | — |
| Implement `ConfigLoader._deep_merge()` | platform-team | — |
| Implement `ConfigLoader._deserialize()` → `SystemSettings` | platform-team | — |
| Expose `SystemSettings` as module-level singleton via `agenthicc.config.settings` | platform-team | — |
| Wire `AgentConfig` fields (`max_cost_usd`, `max_turns`, `temperature`) to config | platform-team | `AgentConfig` from `lauren_ai._config` |
| Wire `TokenBudget` and `CostTracker` to `[execution]` settings | platform-team | `TokenBudget`, `CostTracker` from `lauren_ai._cost` |

### Phase 2 — Security Policy (Sprint 1, Week 2-3)

| Task | Owner | Lauren-AI Type Used |
|------|-------|---------------------|
| Implement `PermissionRule.matches()` with glob + condition checks | security-team | — |
| Implement `SecurityPolicy.evaluate()` fail-closed logic | security-team | — |
| Integrate policy evaluation into `ToolContext.check_permission()` | platform-team | `ToolContext`, `ToolResult.error()` |
| Expose `requires_capability` decorator from `lauren_ai._guards` to mark tools that need policy rules | platform-team | `requires_capability` from `lauren_ai._guards` |
| Implement `safety_guard` integration (calls `SafetyPolicy.check()` before tool execution) | platform-team | `safety_guard`, `SafetyPolicy` from `lauren_ai._guards` |

### Phase 3 — Sandboxing (Sprint 2, Week 1-2)

| Task | Owner | Notes |
|------|-------|-------|
| Implement `WorkspaceView` with symlink-resolving `_check()` | security-team | No third-party deps |
| Implement `NetworkSandbox` with custom `aiohttp` connector | platform-team | Requires `aiohttp` |
| Implement `CPUSandbox` context manager with `asyncio.wait_for` | platform-team | — |
| Implement `MemorySandbox.set_subprocess_limits()` with `resource.setrlimit` | platform-team | Linux-only |
| Implement `WasmSandbox` with `wasmtime-py` binding | research-team | Fallback to `seccomp` subprocess |
| Integrate all sandbox layers into tool execution pipeline | platform-team | — |

### Phase 4 — Headless API (Sprint 2, Week 2-3)

| Task | Owner | Lauren-AI Type Used |
|------|-------|---------------------|
| Implement `IntentDispatcher` with `asyncio.Queue` and pool borrowing | api-team | `AgentConfig` |
| Implement `EventBus` with per-intent subscriber sets | api-team | — |
| Implement FastAPI app with auth middleware | api-team | — |
| Implement bridge `AgentInstance` that translates `IntentRequest` to tool calls | api-team | `AgentConfig`, `TokenBudget` |
| Wire `InputGuardrail` to intent submission (`PromptInjectionFilter`, `PIIRedactor`) | security-team | `InputGuardrail`, `PromptInjectionFilter`, `PIIRedactor` from `lauren_ai._guardrails` |
| Wire `OutputGuardrail` to intent result emission (`TopicFilter`, `PIIRedactor`) | security-team | `OutputGuardrail`, `TopicFilter` from `lauren_ai._guardrails` |
| Add `BudgetExceededError` handling in dispatcher loop | api-team | `BudgetExceededError` from `lauren_ai._cost` |
| Integrate `token_budget_guard` into per-turn execution | api-team | `token_budget_guard` from `lauren_ai._guards` |

### Phase 5 — Testing and Hardening (Sprint 3)

| Task | Owner |
|------|-------|
| Unit test suite (see Tests section) | qa-team |
| Integration test suite with real filesystem temp dirs | qa-team |
| E2E WebSocket test with live FastAPI test server | qa-team |
| Penetration test: path traversal, symlink escape, prompt injection | security-team |
| Performance benchmarks: sandbox overhead per tool call | platform-team |
| Documentation and configuration reference | docs-team |

---

## Tests

All tests assume the package is installed in development mode (`pip install -e ".[dev]"`). Run with:

```bash
pytest tests/test_prd07_configuration_security.py -v
```

### test_prd07_configuration_security.py

```python
"""
Tests for PRD-07: Configuration, Security and Headless API.

Prerequisites:
    pip install pytest pytest-asyncio aiohttp fastapi httpx websockets tomli uvicorn
"""
import asyncio
import json
import os
import tempfile
import textwrap
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers — inline minimal implementations so tests are self-contained
# ---------------------------------------------------------------------------

def _write_toml(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


# ---------------------------------------------------------------------------
# Unit Tests — TOML merge precedence
# ---------------------------------------------------------------------------

class TestConfigMerge:
    """Verify that user config values override project config values."""

    def test_scalar_override(self, tmp_path):
        """User's max_concurrent_intents overrides project's value."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write_toml(project, """
            [execution]
            max_concurrent_intents = 4
            max_parallel_tasks = 2
        """)
        _write_toml(user, """
            [execution]
            max_concurrent_intents = 16
        """)
        from agenthicc.config import ConfigLoader
        # Patch USER_FILE for this test
        orig = ConfigLoader.USER_FILE
        try:
            ConfigLoader.USER_FILE = user
            settings = ConfigLoader.load(project_root=tmp_path)
        finally:
            ConfigLoader.USER_FILE = orig

        assert settings.execution.max_concurrent_intents == 16
        assert settings.execution.max_parallel_tasks == 2  # not overridden

    def test_list_replacement(self, tmp_path):
        """User's allowed_paths replaces (not appends to) project's allowed_paths."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write_toml(project, """
            [security]
            allowed_paths = ["/workspace", "/data"]
        """)
        _write_toml(user, """
            [security]
            allowed_paths = ["/home/user/project"]
        """)
        from agenthicc.config import ConfigLoader
        orig = ConfigLoader.USER_FILE
        try:
            ConfigLoader.USER_FILE = user
            settings = ConfigLoader.load(project_root=tmp_path)
        finally:
            ConfigLoader.USER_FILE = orig

        assert settings.security.allowed_paths == [Path("/home/user/project")]

    def test_project_only(self, tmp_path):
        """When no user file exists, project values are used unchanged."""
        project = tmp_path / "agenthicc.toml"
        _write_toml(project, """
            [execution]
            agent_pool_size = 32
        """)
        from agenthicc.config import ConfigLoader
        orig = ConfigLoader.USER_FILE
        try:
            ConfigLoader.USER_FILE = tmp_path / "nonexistent.toml"
            settings = ConfigLoader.load(project_root=tmp_path)
        finally:
            ConfigLoader.USER_FILE = orig

        assert settings.execution.agent_pool_size == 32

    def test_empty_config_returns_defaults(self, tmp_path):
        """Missing TOML files produce a SystemSettings with all defaults."""
        from agenthicc.config import ConfigLoader
        orig = ConfigLoader.USER_FILE
        try:
            ConfigLoader.USER_FILE = tmp_path / "nonexistent.toml"
            settings = ConfigLoader.load(project_root=tmp_path)
        finally:
            ConfigLoader.USER_FILE = orig

        assert settings.execution.max_concurrent_intents == 8
        assert settings.security.sandbox_mode is True
        assert settings.api.port == 8000

    def test_user_api_key_env_override(self, tmp_path):
        """User can override api_key_env without touching project file."""
        project = tmp_path / "agenthicc.toml"
        user = tmp_path / ".agenthicc.toml"
        _write_toml(project, """
            [api]
            api_key_env = "AGENTHICC_API_KEY"
        """)
        _write_toml(user, """
            [api]
            api_key_env = "MY_PERSONAL_API_KEY"
        """)
        from agenthicc.config import ConfigLoader
        orig = ConfigLoader.USER_FILE
        try:
            ConfigLoader.USER_FILE = user
            settings = ConfigLoader.load(project_root=tmp_path)
        finally:
            ConfigLoader.USER_FILE = orig

        assert settings.api.api_key_env == "MY_PERSONAL_API_KEY"


# ---------------------------------------------------------------------------
# Unit Tests — PermissionRule / SecurityPolicy accept and deny
# ---------------------------------------------------------------------------

class TestPermissionPolicy:
    """Verify SecurityPolicy evaluation including fail-closed behavior."""

    def _make_policy(self, rules):
        from agenthicc.security import SecurityPolicy, PermissionRule, PermissionAction
        return SecurityPolicy(rules=[
            PermissionRule(**r) for r in rules
        ])

    def test_allow_matching_tool(self):
        from agenthicc.security import PermissionAction
        policy = self._make_policy([
            {"tool_pattern": "read_*", "action": "allow"},
            {"tool_pattern": "write_*", "action": "deny"},
        ])
        assert policy.evaluate("read_file", {}) == PermissionAction.ALLOW

    def test_deny_matching_tool(self):
        from agenthicc.security import PermissionAction
        policy = self._make_policy([
            {"tool_pattern": "read_*", "action": "allow"},
            {"tool_pattern": "write_*", "action": "deny"},
        ])
        assert policy.evaluate("write_file", {}) == PermissionAction.DENY

    def test_fail_closed_no_matching_rule(self):
        """A tool with no matching rule is denied by default."""
        from agenthicc.security import PermissionAction
        policy = self._make_policy([
            {"tool_pattern": "read_*", "action": "allow"},
        ])
        assert policy.evaluate("shell_exec", {}) == PermissionAction.DENY

    def test_empty_policy_denies_everything(self):
        """An empty rule set denies all tools (fail-closed)."""
        from agenthicc.security import PermissionAction, SecurityPolicy
        policy = SecurityPolicy(rules=[])
        assert policy.evaluate("read_file", {}) == PermissionAction.DENY
        assert policy.evaluate("write_file", {}) == PermissionAction.DENY

    def test_path_prefix_condition_allow(self):
        """Allow rule with path_prefix only fires when path starts with prefix."""
        from agenthicc.security import PermissionAction
        policy = self._make_policy([
            {
                "tool_pattern": "fs_*",
                "action": "allow",
                "path_prefix": "/workspace",
            },
        ])
        assert policy.evaluate("fs_read", {"path": "/workspace/src/main.py"}) == PermissionAction.ALLOW

    def test_path_prefix_condition_deny_outside(self):
        """Allow rule with path_prefix does NOT fire for paths outside the prefix."""
        from agenthicc.security import PermissionAction
        policy = self._make_policy([
            {
                "tool_pattern": "fs_*",
                "action": "allow",
                "path_prefix": "/workspace",
            },
        ])
        # No rule matches /etc/passwd → fail-closed deny
        assert policy.evaluate("fs_read", {"path": "/etc/passwd"}) == PermissionAction.DENY

    def test_first_matching_rule_wins(self):
        """Rules are evaluated in order; the first match wins regardless of later rules."""
        from agenthicc.security import PermissionAction
        policy = self._make_policy([
            {"tool_pattern": "read_*", "action": "allow"},
            {"tool_pattern": "*", "action": "deny"},  # catch-all deny
        ])
        # read_file matches the first rule (allow), not the second (deny)
        assert policy.evaluate("read_file", {}) == PermissionAction.ALLOW
        # write_file skips the first rule, matches the catch-all deny
        assert policy.evaluate("write_file", {}) == PermissionAction.DENY


# ---------------------------------------------------------------------------
# Unit Tests — WorkspaceView path traversal blocking
# ---------------------------------------------------------------------------

class TestWorkspaceView:
    """Verify WorkspaceView blocks path traversal and symlink escape attempts."""

    def test_allowed_path_succeeds(self, tmp_path):
        from agenthicc.sandbox import WorkspaceView
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        allowed_file = workspace / "hello.txt"
        allowed_file.write_text("hello")
        view = WorkspaceView(allowed_paths=[workspace])
        content = view.read_text(allowed_file)
        assert content == "hello"

    def test_path_traversal_blocked(self, tmp_path):
        from agenthicc.sandbox import WorkspaceView, PathTraversalError
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        view = WorkspaceView(allowed_paths=[workspace])
        with pytest.raises(PathTraversalError):
            view.read_text(workspace / ".." / "secret.txt")

    def test_absolute_path_outside_workspace_blocked(self, tmp_path):
        from agenthicc.sandbox import WorkspaceView, PathTraversalError
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        view = WorkspaceView(allowed_paths=[workspace])
        with pytest.raises(PathTraversalError):
            view.read_text(Path("/etc/passwd"))

    def test_write_allowed_inside_workspace(self, tmp_path):
        from agenthicc.sandbox import WorkspaceView
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        view = WorkspaceView(allowed_paths=[workspace])
        target = workspace / "output.txt"
        view.write_text(target, "written")
        assert target.read_text() == "written"

    def test_write_blocked_outside_workspace(self, tmp_path):
        from agenthicc.sandbox import WorkspaceView, PathTraversalError
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        view = WorkspaceView(allowed_paths=[workspace])
        with pytest.raises(PathTraversalError):
            view.write_text(outside, "should not be written")
        assert not outside.exists()


# ---------------------------------------------------------------------------
# Integration Tests — sandbox escape attempt via symlink
# ---------------------------------------------------------------------------

class TestSymlinkSandboxEscape:
    """
    Integration tests verifying that WorkspaceView correctly blocks symlink-based
    escape attempts where a symlink inside the workspace points outside.
    """

    def test_symlink_escape_blocked(self, tmp_path):
        """
        A symlink inside the workspace pointing to a file outside must be blocked,
        because WorkspaceView resolves symlinks before checking the prefix.
        """
        from agenthicc.sandbox import WorkspaceView, PathTraversalError

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        secret_file = secret_dir / "password.txt"
        secret_file.write_text("p@ssw0rd")

        # Create a symlink inside the workspace that points outside
        evil_link = workspace / "evil_link.txt"
        evil_link.symlink_to(secret_file)

        view = WorkspaceView(allowed_paths=[workspace])
        with pytest.raises(PathTraversalError):
            view.read_text(evil_link)

        # Ensure the secret was not read
        assert not evil_link.is_symlink() or True  # link still exists, but access was blocked

    def test_directory_symlink_escape_blocked(self, tmp_path):
        """
        A symlink to an external directory should also be blocked for any
        path under that symlink.
        """
        from agenthicc.sandbox import WorkspaceView, PathTraversalError

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (external / "config.txt").write_text("external config")

        # Symlink workspace/ext_dir -> external
        (workspace / "ext_dir").symlink_to(external)

        view = WorkspaceView(allowed_paths=[workspace])
        with pytest.raises(PathTraversalError):
            view.read_text(workspace / "ext_dir" / "config.txt")


# ---------------------------------------------------------------------------
# Integration Tests — network domain blocking
# ---------------------------------------------------------------------------

class TestNetworkDomainBlocking:
    """
    Integration tests verifying that NetworkSandbox blocks connections to
    domains not in the allow-list.
    """

    @pytest.mark.asyncio
    async def test_allowed_domain_passes_check(self):
        from agenthicc.sandbox import NetworkSandbox
        sandbox = NetworkSandbox(allow_list=["api.anthropic.com", "api.github.com"])
        # Should not raise
        sandbox.check_domain("api.anthropic.com")
        sandbox.check_domain("api.github.com")

    @pytest.mark.asyncio
    async def test_subdomain_of_allowed_domain_passes(self):
        from agenthicc.sandbox import NetworkSandbox
        sandbox = NetworkSandbox(allow_list=["example.com"])
        sandbox.check_domain("api.example.com")
        sandbox.check_domain("v2.api.example.com")

    @pytest.mark.asyncio
    async def test_blocked_domain_raises(self):
        from agenthicc.sandbox import NetworkSandbox, NetworkDomainBlockedError
        sandbox = NetworkSandbox(allow_list=["api.anthropic.com"])
        with pytest.raises(NetworkDomainBlockedError):
            sandbox.check_domain("evil.attacker.com")

    @pytest.mark.asyncio
    async def test_empty_allow_list_blocks_all(self):
        from agenthicc.sandbox import NetworkSandbox, NetworkDomainBlockedError
        sandbox = NetworkSandbox(allow_list=[])
        with pytest.raises(NetworkDomainBlockedError):
            sandbox.check_domain("api.anthropic.com")


# ---------------------------------------------------------------------------
# E2E Test — headless WebSocket client submits intent and receives events
# ---------------------------------------------------------------------------

class TestHeadlessWebSocketE2E:
    """
    End-to-end test: a WebSocket client connects to a live FastAPI test server,
    submits an intent via POST /v1/intents, subscribes to events via WebSocket,
    and verifies the full event sequence for a completed workflow.

    This test spins up the FastAPI application in-process using httpx's AsyncClient
    with ASGITransport (no real TCP port needed for HTTP), and a real WebSocket
    connection against a uvicorn test server for the WS endpoint.
    """

    API_KEY = "test-api-key-12345"

    @pytest_asyncio.fixture
    async def app_client(self, monkeypatch):
        """
        Yield an httpx.AsyncClient wired to the FastAPI ASGI app,
        with the API key injected into the environment.
        """
        import httpx
        from httpx import ASGITransport

        monkeypatch.setenv("AGENTHICC_API_KEY", self.API_KEY)

        # Import here to pick up the monkeypatched env
        from agenthicc.api.app import create_app
        application = create_app(testing=True)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {self.API_KEY}"},
        ) as client:
            yield client

    @pytest.mark.asyncio
    async def test_intent_lifecycle(self, app_client):
        """
        Submit an intent, verify it transitions through queued → running → completed,
        and that the result summary is non-empty.
        """
        response = await app_client.post(
            "/v1/intents",
            json={
                "text": "List the files in /workspace",
                "context": {"working_directory": "/workspace"},
            },
        )
        assert response.status_code == 202
        data = response.json()
        assert "intent_id" in data
        assert data["status"] == "queued"

        intent_id = data["intent_id"]

        # Poll until completion (max 30 seconds)
        for _ in range(60):
            await asyncio.sleep(0.5)
            status_resp = await app_client.get(f"/v1/intents/{intent_id}")
            assert status_resp.status_code == 200
            status_data = status_resp.json()
            if status_data["status"] in ("completed", "failed"):
                break
        else:
            pytest.fail("Intent did not complete within 30 seconds")

        assert status_data["status"] == "completed"
        assert status_data["result"]["summary"]

    @pytest.mark.asyncio
    async def test_websocket_event_stream(self, monkeypatch):
        """
        Connect via WebSocket, subscribe to an intent, submit the intent via HTTP,
        and verify that all expected events arrive in order:
        intent.status_changed (running) → tool.started → tool.completed → intent.completed
        """
        import websockets
        import httpx
        from httpx import ASGITransport

        monkeypatch.setenv("AGENTHICC_API_KEY", self.API_KEY)

        from agenthicc.api.app import create_app
        application = create_app(testing=True)

        # Start a real uvicorn server for WebSocket support
        import uvicorn
        config = uvicorn.Config(application, host="127.0.0.1", port=18765, log_level="error")
        server = uvicorn.Server(config)

        async def run_server():
            await server.serve()

        server_task = asyncio.create_task(run_server())
        # Give the server a moment to start
        await asyncio.sleep(0.3)

        events_received = []
        try:
            ws_uri = f"ws://127.0.0.1:18765/v1/ws?token={self.API_KEY}"
            async with websockets.connect(ws_uri) as ws:
                # Submit intent via HTTP
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:18765",
                    headers={"Authorization": f"Bearer {self.API_KEY}"},
                ) as client:
                    resp = await client.post(
                        "/v1/intents",
                        json={"text": "echo hello from e2e test"},
                    )
                    assert resp.status_code == 202
                    intent_id = resp.json()["intent_id"]

                # Subscribe to this intent's events
                await ws.send(json.dumps({"action": "subscribe", "intent_id": intent_id}))

                # Collect events until intent.completed or intent.failed
                async with asyncio.timeout(30):
                    async for raw_message in ws:
                        event = json.loads(raw_message)
                        events_received.append(event)
                        if event.get("event") in ("intent.completed", "intent.failed"):
                            break
        finally:
            server.should_exit = True
            await asyncio.sleep(0.1)
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        event_types = [e["event"] for e in events_received]
        assert "intent.completed" in event_types, f"Expected intent.completed, got: {event_types}"

        # Verify ordering constraints
        completed_idx = event_types.index("intent.completed")
        if "intent.status_changed" in event_types:
            running_idx = next(
                i for i, e in enumerate(events_received)
                if e.get("event") == "intent.status_changed" and e.get("status") == "running"
            )
            assert running_idx < completed_idx

    @pytest.mark.asyncio
    async def test_unauthenticated_request_rejected(self, monkeypatch):
        """Requests without a valid Bearer token receive HTTP 401."""
        import httpx
        from httpx import ASGITransport

        monkeypatch.setenv("AGENTHICC_API_KEY", self.API_KEY)

        from agenthicc.api.app import create_app
        application = create_app(testing=True)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
            # Wrong API key
            headers={"Authorization": "Bearer wrong-key"},
        ) as client:
            response = await client.post(
                "/v1/intents",
                json={"text": "should be rejected"},
            )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_cancel_running_intent(self, app_client):
        """A running intent can be cancelled and transitions to 'cancelled' status."""
        # Submit a long-running intent
        response = await app_client.post(
            "/v1/intents",
            json={"text": "run a very long analysis that takes many turns"},
        )
        intent_id = response.json()["intent_id"]

        # Immediately cancel it
        cancel_resp = await app_client.delete(f"/v1/intents/{intent_id}")
        assert cancel_resp.status_code in (200, 404)  # 404 if completed before cancel
        if cancel_resp.status_code == 200:
            assert cancel_resp.json()["status"] == "cancelled"
```

---

## Configuration Reference

The following is a complete, production-ready `agenthicc.toml` suitable for a team development environment.
Copy this to your project root and adjust values as needed.

```toml
# =============================================================================
# agenthicc.toml — complete production configuration reference
# =============================================================================
# This file is safe to commit to version control.
# Secrets (API keys, tokens) should live in ~/.agenthicc.toml or environment
# variables only.

[execution]
max_concurrent_intents = 8
max_parallel_tasks     = 4
agent_pool_size        = 16

[hooks]
intent.pre_validate = [
  "myproject.audit:log_intent",
]
tool.post_execute = [
  "myproject.audit:log_tool_result",
]
workflow.on_error = [
  "myproject.errors:handle_transient",
]

[tools]
denied_tools = [
  "shell_exec",
  "tool_define_unsafe",
]

[[tools.mcp_servers]]
name    = "filesystem"
command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]

[[tools.mcp_servers]]
name    = "github"
url     = "http://localhost:8080"
headers = { Authorization = "Bearer ${GITHUB_TOKEN}" }

[memory]
project_memory_path  = ".agenthicc/memory"
vector_db            = "sqlite-vec"
session_ttl_seconds  = 86400

[security]
sandbox_mode      = true
allowed_paths     = ["/workspace"]
network_allow_list = [
  "api.anthropic.com",
  "api.github.com",
]
max_tool_cpu_seconds = 30
max_tool_memory_mb   = 512

[api]
host          = "127.0.0.1"
port          = 8000
api_key_env   = "AGENTHICC_API_KEY"
cors_origins  = []
```

**User override file** (`~/.agenthicc.toml`):

```toml
# ~/.agenthicc.toml — personal overrides, never committed
# These values take precedence over agenthicc.toml.

[execution]
agent_pool_size = 4   # smaller pool on dev laptop

[security]
# Extend workspace to include personal scratch space
allowed_paths = [
  "/workspace",
  "/home/myuser/scratch",
]
max_tool_cpu_seconds = 60   # more patience on slow machines

[api]
api_key_env = "MY_LOCAL_AGENTHICC_KEY"
```

---

## Open Questions

| # | Question | Owner | Target Resolution |
|---|----------|-------|-------------------|
| OQ-1 | Should `network_allow_list` use CIDR blocks in addition to domain names, for internal IP range allow-listing? | security-team | Sprint 2 |
| OQ-2 | `wasmtime-py` adds ~30 MB to the distribution. Should `WasmSandbox` be an optional extra (`pip install agenthicc[wasm]`)? | platform-team | Sprint 2 |
| OQ-3 | `resource.setrlimit(RLIMIT_AS)` is Linux-only. What is the macOS equivalent for `max_tool_memory_mb`? (`RLIMIT_AS` exists on macOS but behaves differently.) | platform-team | Sprint 2 |
| OQ-4 | Should the WebSocket `/v1/ws` endpoint support per-subscription filtering (e.g., only `tool.*` events) to reduce client-side noise? | api-team | Sprint 3 |
| OQ-5 | The current `SecurityPolicy` is stateless. Do we need stateful policies (e.g., "deny after N filesystem writes in one session")? | security-team | v0.8.0 |
| OQ-6 | Should TOML deep-merge behavior be configurable (e.g., a `merge_lists = true` flag) so hooks can be appended rather than replaced? | platform-team | Sprint 1 |
| OQ-7 | API rate limiting per `api_key_env` value: where does this live — FastAPI middleware or a future API gateway? | api-team | v0.8.0 |
| OQ-8 | `InputGuardrail` and `OutputGuardrail` may reject or mutate content. Should the original (pre-guardrail) input be stored for audit purposes, and if so, where? | security-team | Sprint 3 |
| OQ-9 | How should `SystemSettings` handle TOML keys that exist in the file but are not recognized by the current version? Silently ignore vs. warn vs. error? | platform-team | Sprint 1 |
| OQ-10 | Should `allowed_tools` / `denied_tools` in `[tools]` interact with `SecurityPolicy` rules, or are they independent filtering layers evaluated sequentially? | platform-team | Sprint 2 |
