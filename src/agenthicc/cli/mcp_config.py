"""Persistent MCP server configuration for the CLI ``mcp add`` command."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agenthicc.config import PROJECT_CONFIG_CANDIDATES

_USER_CONFIG_CANDIDATES = [
    Path(".agenthicc") / "agenthicc.toml",
    Path(".agenthicc") / ".agenthicc.toml",
    Path(".agenthicc.toml"),
]

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TRANSPORTS = frozenset({"stdio", "ws", "websocket", "streamable", "http"})


class McpConfigError(ValueError):
    """Raised when an MCP server cannot be safely added to configuration."""


@dataclass(frozen=True)
class McpConfigResult:
    """Description of a newly persisted MCP server configuration."""

    name: str
    path: Path
    scope: str


def _config_path(
    *,
    global_scope: bool,
    project_scope: bool,
    project_dir: Path | None,
    user_dir: Path | None,
    explicit_path: str | None,
) -> Path:
    if global_scope and project_scope:
        raise McpConfigError("choose only one target: --global or --project")
    if explicit_path:
        return Path(explicit_path).expanduser()

    base = user_dir if global_scope else project_dir
    candidates = _USER_CONFIG_CANDIDATES if global_scope else PROJECT_CONFIG_CANDIDATES
    if base is None:
        base = Path.home() if global_scope else Path.cwd()

    resolved_candidates = [base / candidate for candidate in candidates]
    for candidate in resolved_candidates:
        if candidate.is_file():
            return candidate
    return base / ".agenthicc" / "agenthicc.toml"


def mcp_config_path(
    *,
    global_scope: bool = False,
    project_scope: bool = False,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    explicit_path: str | None = None,
) -> Path:
    """Return the selected MCP configuration path without modifying it."""
    return _config_path(
        global_scope=global_scope,
        project_scope=project_scope,
        project_dir=project_dir,
        user_dir=user_dir,
        explicit_path=explicit_path,
    )


def read_mcp_servers(path: Path) -> list[dict[str, object]]:
    """Read and validate the MCP array from *path*.

    The returned mappings are raw TOML values so CLI JSON output can retain
    configuration intent without resolving bearer tokens.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise McpConfigError(f"could not read {path}: {exc}") from exc
    tools = data.get("tools", {})
    if tools is None:
        return []
    if not isinstance(tools, Mapping):
        raise McpConfigError("[tools] must be a TOML table")
    raw_servers = tools.get("mcp_servers", [])
    if not isinstance(raw_servers, list):
        raise McpConfigError("tools.mcp_servers must be an array of tables")
    return [dict(item) for item in raw_servers if isinstance(item, Mapping)]


def _validate(
    *,
    name: str,
    url: str,
    transport: str,
    token_env: str,
    reconnect_attempts: int,
    reconnect_delay_seconds: float,
) -> tuple[str, str, str, str]:
    clean_name = name.strip()
    if not _SERVER_NAME_RE.fullmatch(clean_name):
        raise McpConfigError(
            "server name must start with a letter or number and contain only "
            "letters, numbers, '.', '_' or '-'"
        )
    clean_url = url.strip()
    if not clean_url:
        raise McpConfigError("server URL or stdio command cannot be empty")
    clean_transport = transport.strip().lower()
    if clean_transport not in _TRANSPORTS:
        raise McpConfigError(
            f"unsupported transport {transport!r}; choose from {', '.join(sorted(_TRANSPORTS))}"
        )
    clean_token_env = token_env.strip()
    if clean_token_env and not _ENV_NAME_RE.fullmatch(clean_token_env):
        raise McpConfigError("--token-env must be an uppercase environment variable name")
    if reconnect_attempts < 0:
        raise McpConfigError("reconnect attempts cannot be negative")
    if not math.isfinite(reconnect_delay_seconds) or reconnect_delay_seconds < 0:
        raise McpConfigError("reconnect delay must be a finite, non-negative number")
    return clean_name, clean_url, clean_transport, clean_token_env


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _server_block(
    *,
    name: str,
    url: str,
    transport: str,
    token_env: str,
    auto_connect: bool,
    reconnect_attempts: int,
    reconnect_delay_seconds: float,
) -> str:
    lines = [
        "[[tools.mcp_servers]]",
        f"name = {_toml_string(name)}",
        f"url = {_toml_string(url)}",
        f"transport = {_toml_string(transport)}",
    ]
    if token_env:
        lines.append(f"token = {_toml_string('${' + token_env + '}')}")
    if not auto_connect:
        lines.append("auto_connect = false")
    if reconnect_attempts != 3:
        lines.append(f"reconnect_attempts = {reconnect_attempts}")
    if reconnect_delay_seconds != 1.0:
        lines.append(f"reconnect_delay_seconds = {reconnect_delay_seconds!r}")
    return "\n".join(lines) + "\n"


def _project_root(start: Path) -> Path:
    """Return the nearest ancestor containing ``pyproject.toml``."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return start


def _normalise_local_stdio_source(url: str, transport: str) -> str:
    """Turn a local Lauren MCP app path into an executable stdio command.

    ``mcp add NAME PATH`` is a convenient smoke-test and development flow for
    Lauren MCP examples. Those examples are Python applications, not directly
    executable commands: their stdio entry point is ``lmcp run server.py
    --stdio``. Keep ordinary command strings unchanged and only normalize an
    existing directory or Python file.
    """
    if transport != "stdio":
        return url
    try:
        candidate = Path(url).expanduser().resolve()
    except (OSError, ValueError):
        return url
    if candidate.is_dir():
        server_file = candidate / "server.py"
        if not server_file.is_file():
            raise McpConfigError(f"local MCP directory {candidate} must contain server.py")
        project = _project_root(candidate)
    elif candidate.is_file() and candidate.suffix == ".py":
        server_file = candidate
        project = _project_root(candidate.parent)
    else:
        return url
    return shlex.join(
        (
            "uv",
            "run",
            "--project",
            str(project),
            "lmcp",
            "run",
            str(server_file),
            "--stdio",
        )
    )


def _existing_server_names(data: Mapping[str, object]) -> set[str]:
    tools = data.get("tools")
    if tools is None:
        return set()
    if not isinstance(tools, Mapping):
        raise McpConfigError("[tools] must be a TOML table")
    raw_servers = tools.get("mcp_servers")
    if raw_servers is None:
        return set()
    if not isinstance(raw_servers, list):
        raise McpConfigError("tools.mcp_servers must be an array of tables")
    names: set[str] = set()
    for item in raw_servers:
        if isinstance(item, Mapping):
            raw_name = item.get("name")
            if isinstance(raw_name, str):
                names.add(raw_name)
    return names


def _append_config(path: Path, block: str) -> None:
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise McpConfigError(f"could not read {path}: {exc}") from exc

    separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    updated = existing + separator + block
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise McpConfigError(f"could not write {path}: {exc}") from exc


def add_mcp_server(
    *,
    name: str,
    url: str,
    transport: str = "stdio",
    token_env: str = "",
    auto_connect: bool = True,
    reconnect_attempts: int = 3,
    reconnect_delay_seconds: float = 1.0,
    global_scope: bool = False,
    project_scope: bool = False,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    explicit_path: str | None = None,
) -> McpConfigResult:
    """Validate and append one MCP server to the selected TOML config file."""
    name, url, transport, token_env = _validate(
        name=name,
        url=url,
        transport=transport,
        token_env=token_env,
        reconnect_attempts=reconnect_attempts,
        reconnect_delay_seconds=reconnect_delay_seconds,
    )
    path = _config_path(
        global_scope=global_scope,
        project_scope=project_scope,
        project_dir=project_dir,
        user_dir=user_dir,
        explicit_path=explicit_path,
    )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise McpConfigError(f"cannot safely update {path}: {exc}") from exc

    if name in _existing_server_names(data):
        raise McpConfigError(f"MCP server already exists: {name}")
    url = _normalise_local_stdio_source(url, transport)
    block = _server_block(
        name=name,
        url=url,
        transport=transport,
        token_env=token_env,
        auto_connect=auto_connect,
        reconnect_attempts=reconnect_attempts,
        reconnect_delay_seconds=reconnect_delay_seconds,
    )
    _append_config(path, block)
    return McpConfigResult(name=name, path=path, scope="global" if global_scope else "project")


def remove_mcp_server(
    *,
    name: str,
    global_scope: bool = False,
    project_scope: bool = False,
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    explicit_path: str | None = None,
) -> McpConfigResult:
    """Remove one MCP stanza while preserving unrelated TOML text."""
    clean_name = name.strip()
    if not _SERVER_NAME_RE.fullmatch(clean_name):
        raise McpConfigError("invalid MCP server name")
    path = _config_path(
        global_scope=global_scope,
        project_scope=project_scope,
        project_dir=project_dir,
        user_dir=user_dir,
        explicit_path=explicit_path,
    )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    servers = read_mcp_servers(path)
    if not any(item.get("name") == clean_name for item in servers):
        raise McpConfigError(f"MCP server not found: {clean_name}")

    lines = existing.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.strip() == "[[tools.mcp_servers]]"
    ]
    remove_start = remove_end = None
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = "".join(lines[start:end])
        try:
            parsed = tomllib.loads(block)
        except tomllib.TOMLDecodeError:
            continue
        item = parsed.get("tools", {}).get("mcp_servers", [{}])[0]
        if isinstance(item, Mapping) and item.get("name") == clean_name:
            remove_start, remove_end = start, end
            break
    if remove_start is None or remove_end is None:
        raise McpConfigError(f"could not locate MCP stanza for {clean_name}")
    updated = "".join(lines[:remove_start] + lines[remove_end:])
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise McpConfigError(f"could not write {path}: {exc}") from exc
    return McpConfigResult(name=clean_name, path=path, scope="global" if global_scope else "project")
