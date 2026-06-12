"""TOML configuration loading and merging for Agenthicc (PRD-07).

Merge order (later sources override earlier ones):

1. Hardcoded defaults (lowest priority)
2. ``agenthicc.toml`` (project root)
3. ``~/.agenthicc.toml`` (user home, highest priority)

Scalars are overwritten, lists are replaced, tables are merged recursively.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenthicc.kernel import SecurityPolicy, SystemSettings

__all__ = [
    "AgenthiccConfig",
    "ApiSettings",
    "ExecutionSettings",
    "MemorySettings",
    "SecuritySettings",
    "ToolSettings",
    "deep_merge",
    "load_config",
]

PROJECT_FILE = "agenthicc.toml"
USER_FILE = ".agenthicc.toml"


# ── settings dataclasses ─────────────────────────────────────────────────


@dataclass
class ExecutionSettings:
    max_concurrent_intents: int = 8
    max_parallel_tasks: int = 4
    agent_pool_size: int = 16


@dataclass
class ToolSettings:
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)


@dataclass
class MemorySettings:
    project_memory_path: str = ".agenthicc/memory"
    vector_db: str = "sqlite-vec"
    session_ttl_seconds: int = 86400


@dataclass
class SecuritySettings:
    sandbox_mode: bool = True
    allowed_paths: list[str] = field(default_factory=lambda: ["/workspace"])
    network_allow_list: list[str] = field(default_factory=list)
    max_tool_cpu_seconds: int = 30
    max_tool_memory_mb: int = 512


@dataclass
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    api_key_env: str = "AGENTHICC_API_KEY"


@dataclass
class AgenthiccConfig:
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    tools: ToolSettings = field(default_factory=ToolSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    api: ApiSettings = field(default_factory=ApiSettings)

    def to_system_settings(self) -> SystemSettings:
        """Reflect execution settings into the kernel ``SystemSettings``."""
        return SystemSettings(
            max_concurrent_intents=self.execution.max_concurrent_intents,
            max_parallel_tasks=self.execution.max_parallel_tasks,
            agent_pool_size=self.execution.agent_pool_size,
        )

    def to_security_policy(self) -> SecurityPolicy:
        """Build a kernel ``SecurityPolicy`` from this config (fail-closed)."""
        from agenthicc.security import build_policy_from_config

        return build_policy_from_config(self)


# ── merging ──────────────────────────────────────────────────────────────


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``.

    Nested dicts merge recursively; scalars and lists in ``override``
    replace the corresponding ``base`` values.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten_hooks(data: dict[str, Any], prefix: str = "") -> dict[str, list[str]]:
    """Flatten nested hook tables into ``{"intent.pre_validate": [...]}`` form."""
    flat: dict[str, list[str]] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_hooks(value, dotted))
        else:
            flat[dotted] = [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return flat


# ── loading ──────────────────────────────────────────────────────────────


def _read_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _build_config(data: dict[str, Any]) -> AgenthiccConfig:
    ex = data.get("execution", {})
    execution = ExecutionSettings(
        max_concurrent_intents=ex.get("max_concurrent_intents", 8),
        max_parallel_tasks=ex.get("max_parallel_tasks", 4),
        agent_pool_size=ex.get("agent_pool_size", 16),
    )

    hooks = _flatten_hooks(data.get("hooks", {}))

    to = data.get("tools", {})
    tools = ToolSettings(
        mcp_servers=list(to.get("mcp_servers", [])),
        plugins=list(to.get("plugins", [])),
        allowed=list(to.get("allowed", to.get("allowed_tools", []))),
        denied=list(to.get("denied", to.get("denied_tools", []))),
    )

    me = data.get("memory", {})
    memory = MemorySettings(
        project_memory_path=str(me.get("project_memory_path", ".agenthicc/memory")),
        vector_db=me.get("vector_db", "sqlite-vec"),
        session_ttl_seconds=me.get("session_ttl_seconds", 86400),
    )

    se = data.get("security", {})
    security = SecuritySettings(
        sandbox_mode=se.get("sandbox_mode", True),
        allowed_paths=[str(p) for p in se.get("allowed_paths", ["/workspace"])],
        network_allow_list=list(se.get("network_allow_list", [])),
        max_tool_cpu_seconds=se.get("max_tool_cpu_seconds", 30),
        max_tool_memory_mb=se.get("max_tool_memory_mb", 512),
    )

    ap = data.get("api", {})
    api = ApiSettings(
        host=ap.get("host", "127.0.0.1"),
        port=ap.get("port", 8000),
        api_key_env=ap.get("api_key_env", "AGENTHICC_API_KEY"),
    )

    return AgenthiccConfig(
        execution=execution,
        hooks=hooks,
        tools=tools,
        memory=memory,
        security=security,
        api=api,
    )


def load_config(
    project_path: str | Path | None = None,
    user_path: str | Path | None = None,
) -> AgenthiccConfig:
    """Load and merge configuration into a typed :class:`AgenthiccConfig`.

    ``project_path`` defaults to ``./agenthicc.toml`` and ``user_path`` to
    ``~/.agenthicc.toml``. Missing files are skipped; user settings override
    project settings, which override the hardcoded defaults. Invalid TOML
    raises :class:`tomllib.TOMLDecodeError`.
    """
    project_file = Path(project_path) if project_path is not None else Path(PROJECT_FILE)
    user_file = Path(user_path) if user_path is not None else Path.home() / USER_FILE

    merged: dict[str, Any] = {}
    for path in (project_file, user_file):
        if path.is_file():
            merged = deep_merge(merged, _read_toml(path))

    return _build_config(merged)
