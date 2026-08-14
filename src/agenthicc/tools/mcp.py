"""MCP tool bridge and registry (PRD-28)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import shlex
from pathlib import Path
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from agenthicc.kernel.processor import EventProcessor

from agenthicc.tools.base import Tool
from agenthicc.kernel import Event

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\${([A-Z_][A-Z0-9_]*)}")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_INVALID_PROVIDER_TOOL_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]")
_PROVIDER_TOOL_NAME_MAX_LENGTH = 64
_TRANSPORT_ALIASES = {
    "streamable": "streamable_http",
    "http": "streamable_http",
    "websocket": "ws",
}
_SUPPORTED_TRANSPORTS = frozenset({"stdio", "streamable_http", "sse", "ws"})


def _provider_safe_tool_name(name: str) -> str:
    """Return a provider-compatible representation of an MCP tool name.

    Agenthicc keeps MCP tools internally under the canonical
    ``mcp:<server>:<tool>`` identity. Provider tool schemas are stricter and
    accept only alphanumeric characters, hyphens, and underscores (and may
    impose a 64-character limit). Replace separators and other punctuation,
    then add a short digest when truncation is required.
    """
    safe = _INVALID_PROVIDER_TOOL_CHARS_RE.sub("_", name)
    if not safe:
        safe = "mcp_tool"
    if len(safe) > _PROVIDER_TOOL_NAME_MAX_LENGTH:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[: _PROVIDER_TOOL_NAME_MAX_LENGTH - 9]}_{digest}"
    return safe


def _mutable_json_copy(value: object) -> object:
    """Copy frozen MCP metadata into provider-serializable JSON containers."""
    if isinstance(value, Mapping):
        return {key: _mutable_json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json_copy(item) for item in value]
    return copy.deepcopy(value)


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
    "McpStaleCatalogError",
    "McpConfigurationError",
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
    url: str = ""  # legacy command string (stdio) or URL (remote)
    transport: str = "stdio"  # stdio | streamable_http | sse | legacy aliases
    token: str = ""  # bearer token; supports ${ENV_VAR}
    command: tuple[str, ...] = ()
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    env_vars: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    env_headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    required: bool = False
    auto_connect: bool = True
    reconnect_attempts: int = 3
    reconnect_delay_seconds: float = 1.0
    startup_timeout_s: float = 10.0
    tool_timeout_s: float = 60.0
    enabled_tools: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()
    default_approval_mode: str = "prompt"
    tool_approval: dict[str, str] = field(default_factory=dict)
    oauth: dict[str, object] | bool | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # TOML produces lists while callers and tests often use tuples. Store
        # immutable sequence fields so a config cannot mutate beneath a
        # running session manager.
        self.command = tuple(str(item) for item in self.command)
        self.env_vars = tuple(str(item) for item in self.env_vars)
        self.enabled_tools = tuple(str(item) for item in self.enabled_tools)
        self.disabled_tools = tuple(str(item) for item in self.disabled_tools)
        self.env = {str(k): str(v) for k, v in dict(self.env).items()}
        self.headers = {str(k): str(v) for k, v in dict(self.headers).items()}
        self.env_headers = {str(k): str(v) for k, v in dict(self.env_headers).items()}
        self.tool_approval = {str(k): str(v) for k, v in dict(self.tool_approval).items()}
        self.metadata = dict(self.metadata)

    @property
    def normalized_transport(self) -> str:
        return _TRANSPORT_ALIASES.get(self.transport.strip().lower(), self.transport.strip().lower())

    @property
    def effective_startup_timeout_s(self) -> float:
        return self.startup_timeout_s

    @property
    def effective_tool_timeout_s(self) -> float:
        return self.tool_timeout_s

    @classmethod
    def from_dict(cls, d: dict[str, object], *, strict: bool = False) -> "McpServerConfig":
        """Build from a raw TOML mapping.

        ``strict=False`` preserves the historical parser behavior for old
        configuration files. New callers can request strict validation and
        receive an actionable error for misspelled fields.
        """
        allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        aliases = {
            "reconnect_delay_s": "reconnect_delay_seconds",
            "timeout_s": "tool_timeout_s",
        }
        unknown = sorted(str(k) for k in d if k not in allowed and k not in aliases)
        if strict and unknown:
            raise McpConfigurationError("unknown MCP configuration field(s): " + ", ".join(unknown))
        values = {aliases.get(k, k): v for k, v in d.items() if k in allowed or k in aliases}
        if "command" in values and isinstance(values["command"], str):
            # A string command is accepted only as a legacy compatibility
            # value; the bridge still never invokes a shell.
            values["command"] = tuple(shlex.split(values["command"]))
        return cls(**values)

    def resolved_token(self) -> str:
        """Expand ``${ENV_VAR}`` tokens in the token field."""
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), self.token)

    def resolved_url(self) -> str:
        """Expand ``${ENV_VAR}`` tokens in the url field."""
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), self.url)

    def resolved_command(self) -> list[str]:
        """Return the executable argv without shell interpretation."""
        if self.command:
            return [
                _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), part)
                for part in self.command
            ]
        return shlex.split(self.resolved_url())

    def resolved_env(self) -> dict[str, str]:
        """Return explicit environment plus explicitly allowed inherited vars."""
        values = dict(self.env)
        for name in self.env_vars:
            if not _ENV_NAME_RE.fullmatch(name):
                raise McpConfigurationError(f"invalid MCP environment variable name: {name!r}")
            if name in os.environ:
                values[name] = os.environ[name]
        return {
            key: _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
            for key, value in values.items()
        }

    def resolved_headers(self) -> dict[str, str]:
        """Resolve remote headers, keeping secret values out of config objects."""
        values = dict(self.headers)
        for header, env_name in self.env_headers.items():
            if not _ENV_NAME_RE.fullmatch(env_name):
                raise McpConfigurationError(f"invalid MCP header environment name: {env_name!r}")
            value = os.environ.get(env_name)
            if value is not None:
                values[header] = value
        token = self.resolved_token()
        if token:
            values.setdefault("Authorization", f"Bearer {token}")
        return values

    def redacted(self) -> dict[str, object]:
        """Return safe diagnostics without secret values."""
        command = list(self.command) if self.command else ["<legacy command>"]
        safe_command: list[str] = []
        redact_next = False
        for item in command:
            if redact_next or _looks_secret(item):
                safe_command.append("<redacted>")
                redact_next = False
            else:
                safe_command.append(item)
            if _looks_secret(item) and "=" not in item:
                redact_next = True
        return {
            "name": self.name,
            "transport": self.normalized_transport,
            "url": _redacted_url(self.url) if self.normalized_transport != "stdio" else "<stdio>",
            "command": safe_command,
            "cwd": self.cwd,
            "env": {key: "<redacted>" if _looks_secret(key) else value for key, value in self.env.items()},
            "env_vars": list(self.env_vars),
            "headers": {key: "<redacted>" for key in self.headers},
            "env_headers": dict(self.env_headers),
            "enabled": self.enabled,
            "required": self.required,
            "auto_connect": self.auto_connect,
            "startup_timeout_s": self.startup_timeout_s,
            "tool_timeout_s": self.tool_timeout_s,
            "enabled_tools": list(self.enabled_tools),
            "disabled_tools": list(self.disabled_tools),
        }

    def validate(self, *, workspace_root: Path | None = None) -> None:
        """Validate fields that can be checked before opening a connection."""
        if not self.name.strip() or ":" in self.name or any(ch.isspace() for ch in self.name):
            raise McpConfigurationError("MCP server name must be non-empty and contain no spaces or ':'")
        transport = self.normalized_transport
        if transport not in _SUPPORTED_TRANSPORTS:
            raise McpConfigurationError(f"unsupported MCP transport: {self.transport!r}")
        if self.reconnect_attempts < 0 or not math.isfinite(self.reconnect_delay_seconds):
            raise McpConfigurationError("MCP reconnect settings must be finite and non-negative")
        if self.reconnect_delay_seconds < 0:
            raise McpConfigurationError("MCP reconnect delay cannot be negative")
        for label, value in (("startup", self.startup_timeout_s), ("tool", self.tool_timeout_s)):
            if not math.isfinite(value) or value <= 0:
                raise McpConfigurationError(f"MCP {label} timeout must be finite and greater than zero")
        for env_name in (*self.env, *self.env_vars, *self.env_headers.values()):
            if not _ENV_NAME_RE.fullmatch(env_name):
                raise McpConfigurationError(
                    f"invalid MCP environment variable name: {env_name!r}"
                )
        secret_headers = sorted(header for header in self.headers if _looks_secret(header))
        if secret_headers:
            raise McpConfigurationError(
                "sensitive MCP headers must use env_headers or token references: "
                + ", ".join(secret_headers)
            )
        self.resolved_headers()
        if transport == "stdio" and not self.resolved_command():
            raise McpConfigurationError(f"MCP stdio server {self.name!r} has no command")
        if transport in {"streamable_http", "sse", "ws"}:
            from urllib.parse import urlsplit

            parsed = urlsplit(self.resolved_url())
            allowed_schemes = {"ws", "wss"} if transport == "ws" else {"http", "https"}
            if parsed.scheme not in allowed_schemes or not parsed.netloc:
                raise McpConfigurationError(f"MCP server {self.name!r} has an invalid URL")
            if parsed.username or parsed.password:
                raise McpConfigurationError("MCP URLs must not contain embedded credentials")
        if self.cwd and workspace_root is not None:
            candidate = (workspace_root / self.cwd).resolve()
            root = workspace_root.resolve()
            if candidate != root and root not in candidate.parents:
                raise McpConfigurationError("MCP stdio cwd must remain inside the workspace")


class McpConfigurationError(ValueError):
    """Raised when an MCP server definition is unsafe or malformed."""


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH"))


def _redacted_url(value: str) -> str:
    """Redact environment expansions and secret-looking query parameters."""
    if not value:
        return value
    value = _ENV_RE.sub("<redacted>", value)
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted>"
    query = [
        (key, "<redacted>" if _looks_secret(key) else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


@dataclass(frozen=True, slots=True)
class McpToolSchema:
    """Lightweight representation of a tool advertised by an MCP server."""

    name: str
    description: str
    input_schema: Mapping[str, object]  # verbatim MCP inputSchema JSON


class McpToolCallError(RuntimeError):
    """Raised when an MCP tool call fails at the transport or protocol level."""


class McpStaleCatalogError(McpToolCallError):
    """Raised when a prepared call belongs to a replaced catalog revision."""


# ---------------------------------------------------------------------------
# AgenthiccMcpTool
# ---------------------------------------------------------------------------


class AgenthiccMcpTool(Tool):
    """A :class:`Tool` that proxies calls to a remote MCP server tool."""

    def __init__(
        self,
        bridge: "McpToolBridge",
        schema: McpToolSchema,
        *,
        provider_name_override: str | None = None,
        catalog_revision: int = 0,
        catalog_revision_checker: object | None = None,
    ) -> None:
        self._bridge = bridge
        self._schema = schema
        self._provider_name_override = provider_name_override
        self.catalog_revision = catalog_revision
        self._catalog_revision_checker = catalog_revision_checker

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"mcp:{self._bridge.server_name}:{self._schema.name}"

    @property
    def provider_name(self) -> str:
        """Return the provider-safe name used in Lauren tool schemas."""
        return self._provider_name_override or _provider_safe_tool_name(self.name)

    @property
    def description(self) -> str:  # type: ignore[override]
        return self._schema.description

    @property
    def parameters(self) -> dict[str, object]:  # type: ignore[override]
        return _mutable_json_copy(self._schema.input_schema)  # type: ignore[return-value]

    async def execute(
        self,
        args: dict[str, object],
        context: dict[str, object],
    ) -> object:
        checker = self._catalog_revision_checker
        if callable(checker) and not checker(self.name, self.catalog_revision):
            raise McpStaleCatalogError(
                f"MCP tool {self.name!r} belongs to a stale catalog revision"
            )
        tool_call_id = context.get("tool_call_id", "")
        return await self._bridge.call_tool(self._schema.name, args, tool_call_id=tool_call_id)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_tool_content(result: object) -> object:
    """Extract the usable payload from an MCP ``CallToolResult``."""
    structured = _result_field(result, "structuredContent", None)
    if isinstance(structured, Mapping) and "result" in structured:
        return structured["result"]

    content = _result_field(result, "content", None)
    if not content:
        return None
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return content
    if len(content) == 1:
        block = content[0]
        text = _result_field(block, "text", None)
        if isinstance(text, str):
            return _decode_json_text(text)
        return _result_field(block, "data", None) or str(block)
    return [
        _result_field(b, "text", None) or _result_field(b, "data", None) or str(b) for b in content
    ]


def _decode_json_text(text: str) -> object:
    """Decode JSON-encoded MCP text while leaving ordinary text untouched."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _result_field(result: object, field_name: str, default: object = None) -> object:
    """Read a field from either an MCP model object or JSON-like mapping."""
    if isinstance(result, Mapping):
        return result.get(field_name, default)
    return getattr(result, field_name, default)


def _extract_text_content(result: object) -> str:
    """Extract a plain-text summary from an MCP result (used for error messages)."""
    content = _result_field(result, "content", [])
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return str(content) if content else str(result)
    return " ".join(str(_result_field(b, "text", b)) for b in content) if content else str(result)


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
        event_processor: EventProcessor | None = None,
        *,
        network_guard: object | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._cfg = config
        self._events = event_processor
        self._client: object = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._change_callback: object = None
        self._network_guard = network_guard
        self._workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self._last_stderr_tail = ""

    @property
    def server_name(self) -> str:
        return self._cfg.name

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def stderr_tail(self) -> str:
        """Return bounded child diagnostics when the client exposes them."""
        value = getattr(self._client, "stderr_tail", "") if self._client is not None else ""
        if isinstance(value, str) and value:
            return value
        return self._last_stderr_tail

    async def connect(self) -> None:
        """Connect to the MCP server, retrying with exponential backoff."""
        async with self._lock:
            if self._connected:
                return

            if not _LAUREN_MCP_AVAILABLE:
                raise ImportError(
                    "lauren_mcp is not installed. Install it with: pip install lauren-mcp"
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
                    self._last_stderr_tail = ""
                    self._install_change_callback()
                    log.info("Connected to MCP server %r", self._cfg.name)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    client = self._client
                    stderr_tail = getattr(client, "stderr_tail", "") if client is not None else ""
                    if isinstance(stderr_tail, str) and stderr_tail:
                        self._last_stderr_tail = stderr_tail
                    self._client = None
                    self._connected = False
                    if client is not None:
                        try:
                            await client.close()
                        except Exception:  # noqa: BLE001
                            pass

            raise McpToolCallError(
                f"Failed to connect to MCP server {self._cfg.name!r} "
                f"after {self._cfg.reconnect_attempts + 1} attempts: {last_exc}"
            )

    async def _build_client(self) -> object:
        """Instantiate (but do not connect) the appropriate McpServer client."""
        url = self._cfg.resolved_url()
        headers = self._cfg.resolved_headers() or None
        transport = self._cfg.normalized_transport

        if transport != "stdio" and self._network_guard is not None:
            checker = getattr(self._network_guard, "check", None)
            if not callable(checker):
                checker = getattr(self._network_guard, "check_url", None)
            if callable(checker):
                checker(url)

        if transport == "stdio":
            command = self._cfg.resolved_command()
            kwargs: dict[str, object] = {
                "max_retries": self._cfg.reconnect_attempts,
                "startup_timeout": self._cfg.effective_startup_timeout_s,
            }
            if self._cfg.cwd:
                cwd = Path(self._cfg.cwd)
                if not cwd.is_absolute() and self._workspace_root is not None:
                    cwd = self._workspace_root / cwd
                kwargs["cwd"] = str(cwd.resolve())
            if self._cfg.env or self._cfg.env_vars:
                kwargs["env"] = self._cfg.resolved_env()
            try:
                client = _McpServer.stdio(command, **kwargs)
            except TypeError:
                # Older lauren-mcp releases accepted argv but not the newer
                # cwd/env/startup-timeout options. Keep those releases
                # import-compatible while the supported client receives the
                # complete process policy above.
                client = _McpServer.stdio(
                    command,
                    max_retries=self._cfg.reconnect_attempts,
                )
        elif transport == "ws":
            client = _McpServer.ws(
                url,
                headers=headers,
                max_retries=self._cfg.reconnect_attempts,
                startup_timeout=self._cfg.effective_startup_timeout_s,
            )
        elif transport == "sse":
            factory = getattr(_McpServer, "sse", None) or _McpServer.streamable_http
            client = factory(
                url,
                headers=headers,
                max_retries=self._cfg.reconnect_attempts,
                startup_timeout=self._cfg.effective_startup_timeout_s,
            )
        elif transport == "streamable_http":
            client = _McpServer.streamable_http(
                url,
                headers=headers,
                max_retries=self._cfg.reconnect_attempts,
                startup_timeout=self._cfg.effective_startup_timeout_s,
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
        if isinstance(raw_tools, Mapping):
            raw_tools = raw_tools.get("tools", [])
        if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
            raise McpToolCallError(f"MCP server {self._cfg.name!r} returned an invalid tools list")
        return [
            McpToolSchema(
                name=str(_result_field(t, "name", "")),
                description=str(_result_field(t, "description", "") or ""),
                input_schema=(
                    dict(input_schema)
                    if isinstance(input_schema := _result_field(t, "inputSchema", None), Mapping)
                    else {}
                ),
            )
            for t in raw_tools
        ]

    async def get_instructions(self) -> str:
        """Read optional server instructions without requiring SDK support."""
        if not self._connected or self._client is None:
            return ""
        for attr_name in ("get_instructions", "server_instructions", "instructions"):
            value = getattr(self._client, attr_name, None)
            if callable(value):
                value = value()
                if hasattr(value, "__await__"):
                    value = await value
            if isinstance(value, str):
                return value.strip()
        return ""

    def capabilities(self) -> dict[str, object]:
        """Return negotiated capabilities without assuming SDK model types."""
        value = getattr(self._client, "capabilities", None) if self._client is not None else None
        if callable(value):
            value = value()
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def protocol_version(self) -> str:
        """Expose the negotiated protocol version when the SDK provides it."""
        if self._client is None:
            return ""
        value = getattr(self._client, "protocol_version", "")
        return value if isinstance(value, str) else str(getattr(value, "value", "") or "")

    @property
    def server_info(self) -> dict[str, object]:
        """Expose negotiated server metadata without requiring one SDK model."""
        if self._client is None:
            return {}
        value = getattr(self._client, "server_info", {})
        if callable(value):
            value = value()
        return dict(value) if isinstance(value, Mapping) else {}

    async def list_prompts(self) -> list[object]:
        """Feature-detect the optional MCP prompts primitive."""
        if self._client is None:
            return []
        method = getattr(self._client, "list_prompts", None)
        if not callable(method):
            return []
        value = method()
        if hasattr(value, "__await__"):
            value = await value
        if isinstance(value, Mapping):
            value = value.get("prompts", [])
        return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []

    async def list_resources(self) -> list[object]:
        """Feature-detect the optional MCP resources primitive."""
        if self._client is None:
            return []
        method = getattr(self._client, "list_resources", None)
        if not callable(method):
            return []
        value = method()
        if hasattr(value, "__await__"):
            value = await value
        if isinstance(value, Mapping):
            value = value.get("resources", [])
        return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []

    def set_change_callback(self, callback: object) -> None:
        """Install a best-effort callback for SDK list-change notifications."""
        self._change_callback = callback
        self._install_change_callback()

    def _install_change_callback(self) -> None:
        client = self._client
        callback = self._change_callback
        if client is None or callback is None:
            return
        # lauren-mcp exposes the protocol-native notification surface as
        # ``on_list_changed(handler)``.  Keep the older callback spellings
        # below for compatible third-party clients, but prefer this API so
        # ``notifications/tools/list_changed`` actually reaches the session
        # manager instead of silently leaving a stale catalog in place.
        method = getattr(client, "on_list_changed", None)
        if callable(method):
            method(callback)
            return
        for method_name in ("set_tools_changed_callback", "set_tool_list_changed_callback"):
            method = getattr(client, method_name, None)
            if callable(method):
                method(callback)
                return
        for attr_name in ("on_tools_changed", "on_tool_list_changed", "tools_changed_callback"):
            if hasattr(client, attr_name):
                try:
                    setattr(client, attr_name, callback)
                except Exception:  # noqa: BLE001
                    pass

    async def _invoke_tool(
        self, tool_name: str, args: dict[str, object], tool_call_id: str
    ) -> object:
        call = getattr(self._client, "call_tool")
        try:
            return await call(tool_name, args, tool_call_id=tool_call_id)
        except TypeError as exc:
            if "tool_call_id" not in str(exc) and "unexpected keyword" not in str(exc):
                raise
            return await call(tool_name, args)

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, object],
        tool_call_id: str = "",
    ) -> object:
        """Call a tool on the remote MCP server and return the extracted result."""
        if not self._connected:
            raise McpToolCallError(f"Server {self._cfg.name!r} is not connected")
        try:
            result = await asyncio.wait_for(
                self._invoke_tool(tool_name, args, tool_call_id),
                timeout=self._cfg.effective_tool_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            # Re-raise McpCallError from lauren_mcp transparently when available
            if (
                _LAUREN_MCP_AVAILABLE
                and _McpCallError is not None
                and isinstance(exc, _McpCallError)
            ):
                raise McpToolCallError(
                    f"MCP call {self._cfg.name}/{tool_name} failed: {exc}"
                ) from exc
            raise McpToolCallError(f"MCP call {self._cfg.name}/{tool_name} failed: {exc}") from exc

        if _result_field(result, "isError", False):
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

    def __init__(self, event_processor: EventProcessor | None = None) -> None:
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

    async def _register_tools_from_bridge(self, bridge: McpToolBridge) -> list[AgenthiccMcpTool]:
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
