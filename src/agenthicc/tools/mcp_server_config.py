"""MCP server configuration parsed from ``[[tools.mcp_servers]]`` (AGX-01).

Kept apart from :mod:`agenthicc.tools.mcp`, which imports the MCP client
library, so that loading configuration does not import MCP transports.
"""

from __future__ import annotations

import math
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = ["McpConfigurationError", "McpServerConfig"]

_ENV_RE = re.compile(r"\${([A-Z_][A-Z0-9_]*)}")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TRANSPORT_ALIASES = {
    "streamable": "streamable_http",
    "http": "streamable_http",
    "websocket": "ws",
}
_SUPPORTED_TRANSPORTS = frozenset({"stdio", "streamable_http", "sse", "ws"})


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
        return _TRANSPORT_ALIASES.get(
            self.transport.strip().lower(), self.transport.strip().lower()
        )

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
        allowed = {item.name for item in fields(cls)}
        aliases = {
            "reconnect_delay_s": "reconnect_delay_seconds",
            "timeout_s": "tool_timeout_s",
        }
        unknown = sorted(str(k) for k in d if k not in allowed and k not in aliases)
        if strict and unknown:
            raise McpConfigurationError("unknown MCP configuration field(s): " + ", ".join(unknown))
        values = {aliases.get(k, k): v for k, v in d.items() if k in allowed or k in aliases}
        raw_command = values.get("command", ())
        # A string command is accepted only as a legacy compatibility value;
        # the bridge still never invokes a shell.
        command = (
            tuple(shlex.split(raw_command))
            if isinstance(raw_command, str)
            else _strings(values, "command")
        )
        if "name" not in values:
            raise McpConfigurationError("MCP server entry has no name")
        defaults = cls(name="")
        return cls(
            name=_text(values, "name", ""),
            url=_text(values, "url", defaults.url),
            transport=_text(values, "transport", defaults.transport),
            token=_text(values, "token", defaults.token),
            command=command,
            cwd=_text(values, "cwd", defaults.cwd),
            env=_string_map(values, "env"),
            env_vars=_strings(values, "env_vars"),
            headers=_string_map(values, "headers"),
            env_headers=_string_map(values, "env_headers"),
            enabled=_flag(values, "enabled", defaults.enabled),
            required=_flag(values, "required", defaults.required),
            auto_connect=_flag(values, "auto_connect", defaults.auto_connect),
            reconnect_attempts=_count(values, "reconnect_attempts", defaults.reconnect_attempts),
            reconnect_delay_seconds=_seconds(
                values, "reconnect_delay_seconds", defaults.reconnect_delay_seconds
            ),
            startup_timeout_s=_seconds(values, "startup_timeout_s", defaults.startup_timeout_s),
            tool_timeout_s=_seconds(values, "tool_timeout_s", defaults.tool_timeout_s),
            enabled_tools=_strings(values, "enabled_tools"),
            disabled_tools=_strings(values, "disabled_tools"),
            default_approval_mode=_text(
                values, "default_approval_mode", defaults.default_approval_mode
            ),
            tool_approval=_string_map(values, "tool_approval"),
            oauth=_oauth(values.get("oauth")),
            metadata=_object_map(values, "metadata"),
        )

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
            "env": {
                key: "<redacted>" if _looks_secret(key) else value
                for key, value in self.env.items()
            },
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
            raise McpConfigurationError(
                "MCP server name must be non-empty and contain no spaces or ':'"
            )
        transport = self.normalized_transport
        if transport not in _SUPPORTED_TRANSPORTS:
            raise McpConfigurationError(f"unsupported MCP transport: {self.transport!r}")
        if self.reconnect_attempts < 0 or not math.isfinite(self.reconnect_delay_seconds):
            raise McpConfigurationError("MCP reconnect settings must be finite and non-negative")
        if self.reconnect_delay_seconds < 0:
            raise McpConfigurationError("MCP reconnect delay cannot be negative")
        for label, value in (("startup", self.startup_timeout_s), ("tool", self.tool_timeout_s)):
            if not math.isfinite(value) or value <= 0:
                raise McpConfigurationError(
                    f"MCP {label} timeout must be finite and greater than zero"
                )
        for env_name in (*self.env, *self.env_vars, *self.env_headers.values()):
            if not _ENV_NAME_RE.fullmatch(env_name):
                raise McpConfigurationError(f"invalid MCP environment variable name: {env_name!r}")
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


# ── typed readers for raw TOML values ──────────────────────────────────────────


def _wrong_type(key: str, expected: str) -> McpConfigurationError:
    return McpConfigurationError(f"MCP field {key!r} must be {expected}")


def _text(values: Mapping[str, object], key: str, default: str) -> str:
    value = values.get(key, default)
    if not isinstance(value, str):
        raise _wrong_type(key, "a string")
    return value


def _flag(values: Mapping[str, object], key: str, default: bool) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise _wrong_type(key, "true or false")
    return value


def _count(values: Mapping[str, object], key: str, default: int) -> int:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _wrong_type(key, "an integer")
    return value


def _seconds(values: Mapping[str, object], key: str, default: float) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _wrong_type(key, "a number of seconds")
    return float(value)


def _strings(values: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = values.get(key, ())
    if not isinstance(value, (list, tuple)):
        raise _wrong_type(key, "a list of strings")
    return tuple(str(item) for item in value)


def _string_map(values: Mapping[str, object], key: str) -> dict[str, str]:
    value = values.get(key, {})
    if not isinstance(value, Mapping):
        raise _wrong_type(key, "a table of strings")
    return {str(name): str(item) for name, item in value.items()}


def _object_map(values: Mapping[str, object], key: str) -> dict[str, object]:
    value = values.get(key, {})
    if not isinstance(value, Mapping):
        raise _wrong_type(key, "a table")
    return {str(name): item for name, item in value.items()}


def _oauth(value: object) -> dict[str, object] | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return {str(name): item for name, item in value.items()}
    raise _wrong_type("oauth", "true, false or a table")
