"""Generate a complete, inert TOML configuration template.

The project initializer deliberately creates a configuration file containing
only comments.  Keeping the template derived from the typed configuration
objects makes it harder for a new setting to be omitted from the onboarding
surface.  Dynamic tables (provider profiles, agents, workflows, and similar
user-named records) are represented by commented examples because their keys
cannot be known ahead of time.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from agenthicc.config import (
    AgenthiccConfig,
    AgentSettings,
    CloakBrowserSettings,
    PlaywrightSettings,
    ProviderProfile,
    RequestOptionSettings,
)

__all__ = ["build_commented_config_template"]


_HEADER: Final[tuple[str, ...]] = (
    "agenthicc project configuration template",
    "",
    "Every line in this file is commented intentionally. Uncomment only the",
    'settings you need. Secrets should use { env = "ENV_VAR" } references,',
    "not literal credentials. See docs/guides/configuration.md for details.",
    'extends = "path/to/base.toml"  # optional root-level inheritance',
)


def _literal(value: object) -> str:
    """Return a TOML-compatible literal for a supported default value."""

    if value is None:
        # TOML has no null literal.  An empty string is a safe, editable
        # placeholder for optional string/number settings and remains inert
        # until the caller removes the leading comment marker.
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return repr(value)
        return '""'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Path):
        return json.dumps(str(value), ensure_ascii=False)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        pairs = ", ".join(
            f"{json.dumps(str(key), ensure_ascii=False)} = {_literal(item)}"
            for key, item in value.items()
        )
        return "{ " + pairs + " }"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    return '""'


def _comment(lines: list[str], text: str = "") -> None:
    lines.append(f"# {text}" if text else "#")


def _section(lines: list[str], name: str) -> None:
    if lines and lines[-1] != "#":
        _comment(lines)
    _comment(lines, f"[{name}]")


def _field(lines: list[str], name: str, value: object, note: str = "") -> None:
    suffix = f"  # {note}" if note else ""
    _comment(lines, f"{name} = {_literal(value)}{suffix}")


def _dataclass_fields(
    lines: list[str], value: object, *, skip: frozenset[str] = frozenset()
) -> None:
    """Emit scalar fields from a dataclass instance as commented TOML."""

    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return
    for item in dataclasses.fields(value):
        name = item.name
        if name in skip or name.startswith("_"):
            continue
        default = object.__getattribute__(value, name)
        if dataclasses.is_dataclass(default):
            continue
        if isinstance(default, dict) and not default:
            # Empty maps are still real configuration options.  The caller
            # adds useful keyed examples for the known dynamic maps.
            _field(lines, name, default)
            continue
        if isinstance(default, list) and not default:
            _field(lines, name, default)
            continue
        _field(lines, name, default)


def _request_options(lines: list[str], prefix: str) -> None:
    _section(lines, f"{prefix}.request_options")
    _field(lines, "timeout_s", RequestOptionSettings().timeout_s, "optional")
    _comment(lines, "timeout = 60.0  # legacy alias for timeout_s")
    _field(lines, "max_retries", RequestOptionSettings().max_retries, "optional")
    _comment(lines, "sdk_max_retries = 2  # legacy alias for max_retries")
    _field(lines, "include_raw_response", False)
    _comment(lines, 'extra_headers = { "X-Trace" = { env = "TRACE_HEADER" } }')
    _comment(lines, 'extra_query = { format = "json" }')
    _comment(lines, "extra_body = { vendor_option = true }")
    _comment(lines, 'provider = { reasoning_effort = "none" }')


def _provider_profile(lines: list[str], prefix: str) -> None:
    _section(lines, f'{prefix}."profile-name"')
    _field(lines, "provider", "openai", "anthropic | openai | ollama | litellm")
    _field(lines, "model", "", "empty uses the provider default")
    _field(lines, "base_url", "", "HTTP(S) endpoint for compatible/self-hosted providers")
    _comment(lines, 'api_key = { env = "OPENAI_API_KEY" }')
    _field(lines, "api_key_env", "", "alternative to api_key = { env = ... }")
    _comment(lines, 'default_headers = { "Authorization" = { env = "AUTH_TOKEN" } }')
    _comment(lines, "default_query = { }")
    _comment(lines, "client_options = { follow_redirects = true, http2 = true }")
    _field(
        lines, "timeout_s", ProviderProfile.__dataclass_fields__["timeout_s"].default, "optional"
    )
    _field(
        lines,
        "max_retries",
        ProviderProfile.__dataclass_fields__["max_retries"].default,
        "optional",
    )
    _field(
        lines,
        "temperature",
        ProviderProfile.__dataclass_fields__["temperature"].default,
        "optional",
    )
    _field(lines, "top_p", ProviderProfile.__dataclass_fields__["top_p"].default, "optional")
    _field(
        lines,
        "max_completion_tokens",
        ProviderProfile.__dataclass_fields__["max_completion_tokens"].default,
        "optional",
    )
    _field(lines, "protocol", "", "optional transport protocol hint")
    _comment(lines, "capabilities = { tools = true, streaming = true, thinking = true }")
    _request_options(lines, f'{prefix}."profile-name"')


def _mcp_server(lines: list[str]) -> None:
    _comment(lines, "[[tools.mcp_servers]]")
    _field(lines, "name", "server-name")
    _field(lines, "url", "https://example.invalid/mcp")
    _field(lines, "transport", "stdio", "stdio | ws | websocket | streamable | http")
    _field(lines, "token", "", "prefer ${ENV_VAR} or a secret-bearing environment")
    _field(lines, "auto_connect", True)
    _field(lines, "reconnect_attempts", 3)
    _field(lines, "reconnect_delay_seconds", 1.0)
    _field(lines, "metadata", {})


def _browser_section(lines: list[str], name: str, value: object) -> None:
    _section(lines, f"tools.{name}")
    if isinstance(value, (CloakBrowserSettings, PlaywrightSettings)):
        _dataclass_fields(lines, value)


def build_commented_config_template() -> str:
    """Return an exhaustive, fully commented project TOML template.

    The returned text is valid TOML with an empty data model because every
    section and assignment is prefixed with ``#``.  It includes all scalar
    settings represented by the typed configuration dataclasses plus examples
    for user-named tables and list-of-table configuration.
    """

    cfg = AgenthiccConfig()
    lines: list[str] = []
    for text in _HEADER:
        _comment(lines, text)

    _section(lines, "execution")
    _dataclass_fields(
        lines,
        cfg.execution,
        skip=frozenset(
            {
                "context_windows",
                "default_headers",
                "default_query",
                "client_options",
                "request_options",
                "provider_capabilities",
            }
        ),
    )
    _comment(lines, 'default_headers = { "Authorization" = { env = "AUTH_TOKEN" } }')
    _comment(lines, "default_query = { }")
    _comment(lines, "client_options = { follow_redirects = true, http2 = true }")
    _request_options(lines, "execution")

    _section(lines, "providers")
    _comment(lines, '# Add one [providers."profile-name"] table per endpoint.')
    _provider_profile(lines, "providers")

    for name in ("behaviour", "tools", "memory", "security", "api", "plugins", "skills", "storage"):
        value = object.__getattribute__(cfg, name)
        if name == "tools":
            _section(lines, "tools")
            _dataclass_fields(
                lines, value, skip=frozenset({"mcp_servers", "cloakbrowser", "playwright"})
            )
            _comment(lines, "allowed_tools = []  # legacy alias for allowed")
            _comment(lines, "denied_tools = []  # legacy alias for denied")
            _browser_section(lines, "cloakbrowser", value.cloakbrowser)
            _browser_section(lines, "playwright", value.playwright)
            _mcp_server(lines)
        elif name == "memory":
            _section(lines, "memory")
            _dataclass_fields(lines, value)
            _section(lines, "memory.context_windows")
            _comment(lines, "default = 200000  # fallback for unknown model IDs")
            _comment(lines, '"model-id" = 200000')
        elif name == "storage":
            _section(lines, "storage")
            _dataclass_fields(lines, value, skip=frozenset({"s3"}))
            _section(lines, "storage.s3")
            _dataclass_fields(lines, value.s3, skip=frozenset({"mounts"}))
            _section(lines, 'storage.s3.mounts."mount-name"')
            _comment(lines, 'bucket = "bucket-name"')
            _comment(lines, 'prefix = "optional/prefix"')
        else:
            _section(lines, name)
            _dataclass_fields(lines, value)

    _section(lines, "hooks")
    _comment(lines, '"intent.pre_validate" = ["module:function"]')

    _section(lines, "agents")
    _comment(lines, 'Use one [agents."agent-name"] table per configured agent role.')
    _section(lines, 'agents."agent-name"')
    _dataclass_fields(lines, AgentSettings())
    _comment(lines, 'skills = { allow = ["skill-name"], deny = ["other-skill"] }')
    _comment(lines, "skills_allow = []  # legacy alias for allowed_skills")
    _comment(lines, "skills_deny = []  # legacy alias for denied_skills")

    _section(lines, "workflows")
    _comment(lines, 'Use one [workflows."workflow-name"] table per workflow plugin.')
    _comment(lines, 'plan_model = ""')
    _comment(lines, 'execute_model = ""')
    _comment(lines, 'review_model = ""')
    _comment(lines, 'summary_model = ""')
    _comment(lines, "Workflow plugins may define additional typed parameters.")

    return "\n".join(lines) + "\n"
