"""TOML configuration loading and merging for Agenthicc (PRD-07, PRD-21).

Merge order (later sources override earlier ones):

1. Hardcoded defaults (lowest priority)
2. ~/.agenthicc/agenthicc.toml  — user-global defaults (identity, credentials,
                                   preferred model, personal tool/mode plugins)
3. .agenthicc/agenthicc.toml    — per-project overrides (project model, paths,
                                   project-specific tools/modes/commands)
4. Environment variables AGENTHICC_* prefix  (CI / dev convenience)
5. CLI --set section.key=value and --set-secret section.key=ENV_VAR overrides
   (highest priority)

Project config always wins over user-global config.  User-global config supplies
shared defaults that any project can override.  This mirrors the Git
~/.gitconfig / .git/config layering model.

Scalars are overwritten, lists are replaced, tables are merged recursively.
"""

from __future__ import annotations

import tomllib
import re
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from agenthicc.kernel import SecurityPolicy, SystemSettings

if TYPE_CHECKING:
    from lauren_ai._config import LLMConfig
    from agenthicc.tools.mcp import McpServerConfig
    from agenthicc.skills.loader import SkillPermissionSet

__all__ = [
    "AgenthiccConfig",
    "AgentSettings",
    "AgentsSettings",
    "ApiSettings",
    "BehaviourSettings",
    "CloakBrowserSettings",
    "PlaywrightSettings",
    "ExecutionSettings",
    "MemorySettings",
    "PluginSettings",
    "ProviderProfile",
    "ResolvedProviderProfile",
    "RequestOptionSettings",
    "SecretReference",
    "PROVIDER_API_KEY_ENVVAR",
    "PROVIDER_DEFAULT_MODELS",
    "PROVIDER_ENV_SHORTCUTS",
    "SecuritySettings",
    "SkillsSettings",
    "StorageS3Settings",
    "StorageSettings",
    "SUPPORTED_PROVIDERS",
    "ToolSettings",
    "build_llm_config",
    "deep_merge",
    "load_config",
    "PROJECT_CONFIG_CANDIDATES",
    "USER_CONFIG_CANDIDATES",
    "_coerce_env",
    "_find_config_file",
    "_parse_mcp_servers",
    "_resolve_extends",
    "ConfigExtendsCycleError",
]

PROJECT_FILE = "agenthicc.toml"
USER_FILE = ".agenthicc.toml"

# Config file search order — first found wins
PROJECT_CONFIG_CANDIDATES = [
    Path(".agenthicc") / "agenthicc.toml",
    Path(".agenthicc") / ".agenthicc.toml",
    Path("agenthicc.toml"),
    Path(".agenthicc.toml"),
]

USER_CONFIG_CANDIDATES = [
    Path.home() / ".agenthicc" / "agenthicc.toml",
    Path.home() / ".agenthicc" / ".agenthicc.toml",
    Path.home() / ".agenthicc.toml",
]


# ── settings dataclasses ─────────────────────────────────────────────────


SUPPORTED_PROVIDERS = ("anthropic", "openai", "ollama", "litellm")

# PRD-136: live-window budget = context_window − completion reservation − head-room.
# Mirrors lauren-ai's AgentConfig.usable_context_budget so the live window (what
# the session memory trims to + compaction defends) agrees with the hard guard.
# The completion reservation is ExecutionSettings.max_output_tokens, so raising the
# output cap automatically shrinks the live window by the same amount.
_CONTEXT_RESERVE_MIN: int = 4_000
_DEFAULT_CONTEXT_WINDOW: int = 200_000
_DEFAULT_MAX_OUTPUT_TOKENS: int = 32_768

# lauren-ai 1.3.1 exposes provider configuration but not a context-window
# registry.  Keep the compatibility table at this integration boundary so a
# newer lauren-ai can provide the registry without changing agenthicc callers.
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
}


def _context_window_for(model: str, *, default: int = _DEFAULT_CONTEXT_WINDOW) -> int:
    """Resolve a model context window through lauren-ai when available.

    Older lauren-ai releases do not expose the registry that newer releases
    provide, so the local compatibility table is used only as a fallback.
    Some intermediate releases expose context_window_for(model) without the
    newer default parameter; that optional API mismatch must not break TUI
    startup. Prefix matching handles dated model ids and provider-qualified
    ids.
    """
    try:
        from lauren_ai import _config as lauren_config  # noqa: PLC0415

        context_window_for = getattr(lauren_config, "context_window_for", None)
    except ImportError:
        context_window_for = None
    if callable(context_window_for):
        resolved = _call_lauren_context_window(
            lauren_config,
            context_window_for,
            model,
            default,
        )
        if resolved is not None:
            return resolved

    normalized = model.lower()
    for prefix, window in sorted(
        _CONTEXT_WINDOWS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if normalized.startswith(prefix):
            return window
    return default


def _call_lauren_context_window(
    lauren_config: object,
    resolver: object,
    model: str,
    default: int,
) -> int | None:
    """Call lauren-ai's optional context resolver across API generations.

    lauren-ai first exposed a one-argument resolver and later added a default
    keyword. Calling the newer signature unconditionally raises TypeError in
    the older build used by some platform installations. When the legacy
    resolver cannot distinguish an unknown model from its own default, use its
    exported registry (when available), then the local compatibility table and
    the known library default to preserve Agenthicc's default=0 sentinel
    behaviour.
    """
    if not callable(resolver):
        return None

    import inspect  # noqa: PLC0415

    try:
        signature = inspect.signature(resolver)
    except (TypeError, ValueError):
        signature = None

    accepts_default = False
    if signature is not None:
        accepts_default = any(
            parameter.name == "default" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

    if accepts_default:
        try:
            resolved = resolver(model, default=default)
        except TypeError:
            # A wrapper can advertise a permissive signature while forwarding
            # to a legacy implementation. Retry the oldest supported call.
            pass
        else:
            return resolved if isinstance(resolved, int) else None

    try:
        resolved = resolver(model)
    except (TypeError, ValueError):
        # The optional registry is advisory; a broken or incompatible helper
        # must not prevent the local compatibility table from resolving it.
        return None
    if not isinstance(resolved, int):
        return None

    if default == _DEFAULT_CONTEXT_WINDOW:
        return resolved

    registry_value = _context_window_from_lauren_registry(lauren_config, model)
    if registry_value is not None:
        return registry_value

    library_default = vars(lauren_config).get("DEFAULT_CONTEXT_WINDOW", _DEFAULT_CONTEXT_WINDOW)
    if isinstance(library_default, int) and resolved == library_default:
        return None
    return resolved


def _context_window_from_lauren_registry(lauren_config: object, model: str) -> int | None:
    """Resolve a model from an optional lauren-ai registry mapping."""
    registry = vars(lauren_config).get("MODEL_CONTEXT_WINDOWS")
    if not isinstance(registry, Mapping):
        return None

    needle = model.lower()
    matches: list[tuple[int, int]] = []
    for key, value in registry.items():
        if isinstance(key, str) and isinstance(value, int) and key.lower() in needle:
            matches.append((len(key), value))
    return max(matches)[1] if matches else None


# Default models per provider
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o",
    "ollama": "llama3.2",
    "litellm": "anthropic/claude-opus-4-8",
}

# Environment variables read per provider (when api_key not explicit)
PROVIDER_API_KEY_ENVVAR: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "litellm": "ANTHROPIC_API_KEY",  # litellm can delegate to any backend
}

# Provider-specific shorthand env vars (read in addition to AGENTHICC_* vars).
# Setting e.g. OPENAI_MODEL automatically sets execution.model and infers provider=openai.
# Setting OPENAI_BASE_URL enables any OpenAI-compatible endpoint (poolside, Together, etc.)
PROVIDER_ENV_SHORTCUTS: dict[str, tuple[str, str]] = {
    # OpenAI and OpenAI-compatible endpoints
    "OPENAI_MODEL": ("execution", "model"),
    "OPENAI_BASE_URL": ("execution", "base_url"),
    # Anthropic
    "ANTHROPIC_MODEL": ("execution", "model"),
    # Ollama
    "OLLAMA_MODEL": ("execution", "model"),
    "OLLAMA_HOST": ("execution", "base_url"),  # e.g. http://remote:11434
    # LiteLLM
    "LITELLM_MODEL": ("execution", "model"),
}


# Provider profiles deliberately use a small, typed surface.  The profile
# remains a configuration object until ``resolve`` is called at session
# startup; this is what lets a resumed session pick up rotated environment
# secrets without writing those secrets into journals or checkpoints.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PROFILE_KEYS = {
    "provider",
    "model",
    "base_url",
    "api_key",
    "api_key_env",
    "default_headers",
    "default_query",
    "client_options",
    "request_options",
    "timeout_s",
    "max_retries",
    "sdk_max_retries",
    "temperature",
    "top_p",
    "max_completion_tokens",
    "protocol",
    "capabilities",
}
_CAPABILITY_NAMES = {
    "tools",
    "streaming",
    "structured_output",
    "thinking",
    "vision",
    "embeddings",
}
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "api_key",
)
_CANONICAL_REQUEST_KEYS = {
    "model",
    "messages",
    "stream",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "stop",
    "stop_sequences",
    "thinking",
    "options",
    "system",
}
_CLIENT_OPTION_KEYS = {
    "follow_redirects",
    "http2",
    "verify",
    "trust_env",
    "max_redirects",
    "local_address",
}


def _copy_option_value(value: object, *, path: str) -> object:
    """Copy and validate JSON-like provider option values.

    Provider SDKs accept mappings and scalar values.  Rejecting arbitrary
    Python objects at the configuration boundary prevents accidental object
    injection and makes the value safe to carry through a workflow context.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _copy_option_value(item, path=f"{path}.{key}") for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _copy_option_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise ValueError(f"{path} must contain only JSON-compatible values")


def _freeze_option_value(value: object) -> object:
    """Create an immutable snapshot of a validated option tree."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_option_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_option_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Freeze a validated mapping while retaining its static mapping type."""
    frozen = _freeze_option_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("internal error: expected a mapping")
    return cast(Mapping[str, object], frozen)


def _redact_value(value: object, *, key: str = "") -> object:
    """Recursively redact sensitive option keys for diagnostics."""
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, SecretReference):
        return value.redacted()
    if isinstance(value, Mapping):
        return {str(name): _redact_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, key=key) for item in value]
    return value


def _validate_request_option_collisions(
    *, provider: Mapping[str, object], extra_body: Mapping[str, object], path: str
) -> None:
    provider_keys = set(provider)
    body_keys = set(extra_body)
    overlap = provider_keys & body_keys
    if overlap:
        names = ", ".join(sorted(map(str, overlap)))
        raise ValueError(f"{path} duplicates option(s) in provider and extra_body: {names}")
    canonical_provider = provider_keys & _CANONICAL_REQUEST_KEYS
    canonical_body = body_keys & _CANONICAL_REQUEST_KEYS
    if canonical_provider or canonical_body:
        names = ", ".join(sorted(map(str, canonical_provider | canonical_body)))
        raise ValueError(f"{path} cannot redefine canonical request option(s): {names}")


def _validate_client_options(value: Mapping[str, object], *, path: str) -> dict[str, object]:
    unknown = sorted(set(value) - _CLIENT_OPTION_KEYS)
    if unknown:
        raise ValueError(
            f"{path} contains unsupported client option(s): {', '.join(map(str, unknown))}"
        )
    boolean_options = {"follow_redirects", "http2", "trust_env"}
    for name in boolean_options & set(value):
        if not isinstance(value[name], bool):
            raise ValueError(f"{path}.{name} must be boolean")
    if "max_redirects" in value and (
        not isinstance(value["max_redirects"], int) or isinstance(value["max_redirects"], bool)
    ):
        raise ValueError(f"{path}.max_redirects must be an integer")
    if "local_address" in value and not isinstance(value["local_address"], str):
        raise ValueError(f"{path}.local_address must be a string")
    if "verify" in value and not isinstance(value["verify"], (bool, str)):
        raise ValueError(f"{path}.verify must be boolean or a certificate path")
    return cast(dict[str, object], _copy_option_value(value, path=path))


def _resolve_header_value(
    value: str | SecretReference, *, environ: Mapping[str, str] | None, path: str
) -> str:
    resolved = value.resolve(environ) if isinstance(value, SecretReference) else value
    if not isinstance(resolved, str):
        raise ValueError(f"{path} must resolve to a string")
    return _validate_header_value(resolved, path=path)


def _resolve_headers(
    values: Mapping[str, str | SecretReference],
    *,
    environ: Mapping[str, str] | None,
    path: str,
) -> dict[str, str]:
    """Resolve a header mapping without retaining secret values in config."""
    return {
        name: _resolve_header_value(value, environ=environ, path=f"{path}.{name}")
        for name, value in values.items()
    }


def _validate_env_name(value: str, *, path: str) -> str:
    name = value.strip()
    if not _ENV_NAME_RE.fullmatch(name):
        raise ValueError(f"{path} must be a valid environment variable name")
    return name


def _validate_header_name(value: str, *, path: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{path} is not a valid HTTP header name")
    name = value.strip()
    if not _HEADER_NAME_RE.fullmatch(name):
        raise ValueError(f"{path} is not a valid HTTP header name")
    return name


def _validate_header_value(value: str, *, path: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{path} must not contain CR/LF characters")
    return value


def _validate_base_url(value: str, *, path: str = "base_url") -> str:
    url = value.strip()
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"{path} must be a valid HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{path} must be an HTTP(S) URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{path} must not contain URL credentials")
    if parsed.query:
        raise ValueError(f"{path} must not contain a query; use default_query instead")
    if parsed.fragment:
        raise ValueError(f"{path} must not contain a URL fragment")
    if any(ord(char) < 32 for char in url):
        raise ValueError(f"{path} must not contain control characters")
    return url.rstrip("/")


def _optional_float(value: object, path: str) -> float | None:
    if value is None:
        return None
    result = _as_float(value, float("nan"))
    if result != result:  # NaN is not a valid configuration value.
        raise ValueError(f"{path} must be a number")
    return result


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A symbolic reference to a secret held in the process environment."""

    env: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", _validate_env_name(self.env, path="secret.env"))

    @classmethod
    def from_value(cls, value: object, *, path: str) -> "SecretReference":
        if not isinstance(value, Mapping) or set(value) != {"env"}:
            raise ValueError(f'{path} must be a literal string or {{ env = "NAME" }}')
        env = value.get("env")
        if not isinstance(env, str):
            raise ValueError(f"{path}.env must be a string")
        return cls(_validate_env_name(env, path=f"{path}.env"))

    def resolve(self, environ: Mapping[str, str] | None = None) -> str:
        import os  # noqa: PLC0415

        value = (environ if environ is not None else os.environ).get(self.env, "")
        if not value:
            raise ValueError(f"Missing secret environment variable {self.env!r}")
        return value

    def redacted(self) -> dict[str, str]:
        return {"env": self.env}


def _parse_secret_or_string(value: object, *, path: str) -> str | SecretReference:
    if isinstance(value, str):
        return _validate_header_value(value, path=path)
    return SecretReference.from_value(value, path=path)


@dataclass(frozen=True, slots=True)
class RequestOptionSettings:
    """Provider request options accepted by lauren-ai 1.4.x."""

    extra_headers: dict[str, str | SecretReference] = field(default_factory=dict)
    extra_query: dict[str, object] = field(default_factory=dict)
    extra_body: dict[str, object] = field(default_factory=dict)
    provider: dict[str, object] = field(default_factory=dict)
    timeout_s: float | None = None
    max_retries: int | None = None
    include_raw_response: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], *, path: str) -> "RequestOptionSettings":
        allowed = {
            "extra_headers",
            "extra_query",
            "extra_body",
            "provider",
            "timeout_s",
            "timeout",
            "max_retries",
            "sdk_max_retries",
            "include_raw_response",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"{path} contains unknown option(s): {', '.join(map(str, unknown))}")

        raw_headers = raw.get("extra_headers", {})
        if not isinstance(raw_headers, Mapping):
            raise ValueError(f"{path}.extra_headers must be a table")
        headers: dict[str, str | SecretReference] = {}
        for name, value in raw_headers.items():
            header_name = _validate_header_name(str(name), path=f"{path}.extra_headers")
            headers[header_name] = _parse_secret_or_string(
                value, path=f"{path}.extra_headers.{header_name}"
            )

        def option_map(name: str) -> dict[str, object]:
            value = raw.get(name, {})
            if not isinstance(value, Mapping):
                raise ValueError(f"{path}.{name} must be a table")
            return cast(dict[str, object], _copy_option_value(value, path=f"{path}.{name}"))

        timeout_raw = raw.get("timeout_s", raw.get("timeout"))
        timeout = None if timeout_raw is None else _as_float(timeout_raw, -1.0)
        if timeout is not None and timeout <= 0:
            raise ValueError(f"{path}.timeout_s must be greater than zero")
        retries_raw = raw.get("sdk_max_retries", raw.get("max_retries"))
        retries = None if retries_raw is None else _as_int(retries_raw, -1)
        if retries is not None and retries < 0:
            raise ValueError(f"{path}.max_retries must be non-negative")
        include_raw = raw.get("include_raw_response", False)
        if not isinstance(include_raw, bool):
            raise ValueError(f"{path}.include_raw_response must be boolean")
        provider_options = option_map("provider")
        body_options = option_map("extra_body")
        _validate_request_option_collisions(
            provider=provider_options,
            extra_body=body_options,
            path=path,
        )
        return cls(
            extra_headers=headers,
            extra_query=option_map("extra_query"),
            extra_body=body_options,
            provider=provider_options,
            timeout_s=timeout,
            max_retries=retries,
            include_raw_response=include_raw,
        )

    def resolve(self, environ: Mapping[str, str] | None = None) -> object:
        from lauren_ai._transport import RequestOptions  # noqa: PLC0415

        headers = {
            name: _resolve_header_value(
                value,
                environ=environ,
                path=f"request_options.extra_headers.{name}",
            )
            for name, value in self.extra_headers.items()
        }
        return RequestOptions(
            extra_headers=headers,
            extra_query=self.extra_query,
            extra_body=self.extra_body,
            provider=self.provider,
            timeout=self.timeout_s,
            max_retries=self.max_retries,
            include_raw_response=self.include_raw_response,
        )

    def redacted(self) -> dict[str, object]:
        return {
            "extra_headers": {
                name: value.redacted() if isinstance(value, SecretReference) else "<redacted>"
                for name, value in self.extra_headers.items()
            },
            "extra_query": _redact_value(self.extra_query),
            "extra_body": _redact_value(self.extra_body),
            "provider": _redact_value(self.provider),
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "include_raw_response": self.include_raw_response,
        }


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Named connection profile for a native or OpenAI-compatible endpoint."""

    name: str
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str | SecretReference | None = None
    api_key_env: str = ""
    default_headers: dict[str, str | SecretReference] = field(default_factory=dict)
    default_query: dict[str, object] = field(default_factory=dict)
    client_options: dict[str, object] = field(default_factory=dict)
    request_options: RequestOptionSettings | None = None
    timeout_s: float | None = None
    max_retries: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_completion_tokens: int | None = None
    protocol: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, object]) -> "ProviderProfile":
        unknown = sorted(set(raw) - _PROFILE_KEYS)
        if unknown:
            raise ValueError(
                f"providers.{name} contains unknown option(s): {', '.join(map(str, unknown))}"
            )
        provider = str(raw.get("provider", "")).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(SUPPORTED_PROVIDERS)
            raise ValueError(f"providers.{name}.provider must be one of: {supported}")
        api_key_raw = raw.get("api_key")
        api_key: str | SecretReference | None
        if api_key_raw is None:
            api_key = None
        elif isinstance(api_key_raw, str):
            api_key = api_key_raw
        else:
            api_key = SecretReference.from_value(api_key_raw, path=f"providers.{name}.api_key")
        api_key_env = str(raw.get("api_key_env", "")).strip()
        if api_key_env:
            api_key_env = _validate_env_name(api_key_env, path=f"providers.{name}.api_key_env")
        headers_raw = raw.get("default_headers", {})
        if not isinstance(headers_raw, Mapping):
            raise ValueError(f"providers.{name}.default_headers must be a table")
        headers: dict[str, str | SecretReference] = {}
        for header, value in headers_raw.items():
            header_name = _validate_header_name(
                str(header), path=f"providers.{name}.default_headers"
            )
            headers[header_name] = _parse_secret_or_string(
                value, path=f"providers.{name}.default_headers.{header_name}"
            )

        def option_map(key: str) -> dict[str, object]:
            value = raw.get(key, {})
            if not isinstance(value, Mapping):
                raise ValueError(f"providers.{name}.{key} must be a table")
            return cast(
                dict[str, object], _copy_option_value(value, path=f"providers.{name}.{key}")
            )

        client_options = _validate_client_options(
            option_map("client_options"), path=f"providers.{name}.client_options"
        )

        timeout_raw = raw.get("timeout_s")
        timeout = None if timeout_raw is None else _as_float(timeout_raw, -1.0)
        if timeout is not None and timeout <= 0:
            raise ValueError(f"providers.{name}.timeout_s must be greater than zero")
        retries_raw = raw.get("sdk_max_retries", raw.get("max_retries"))
        retries = None if retries_raw is None else _as_int(retries_raw, -1)
        if retries is not None and retries < 0:
            raise ValueError(f"providers.{name}.max_retries must be non-negative")
        temperature = _optional_float(raw.get("temperature"), f"providers.{name}.temperature")
        top_p = _optional_float(raw.get("top_p"), f"providers.{name}.top_p")
        if temperature is not None and temperature < 0:
            raise ValueError(f"providers.{name}.temperature must be non-negative")
        if top_p is not None and not 0 <= top_p <= 1:
            raise ValueError(f"providers.{name}.top_p must be between 0 and 1")
        max_completion = raw.get("max_completion_tokens")
        if max_completion is not None:
            max_completion = _as_int(max_completion, -1)
            if max_completion < 0:
                raise ValueError(f"providers.{name}.max_completion_tokens must be non-negative")
        protocol = str(raw.get("protocol", "")).strip().lower()
        if protocol and protocol not in {
            "chat_completions",
            "messages",
            "ollama",
            "openai",
            "openai-compatible",
            "anthropic",
            "ollama",
            "litellm",
        }:
            raise ValueError(f"providers.{name}.protocol is not supported")
        capabilities_raw = raw.get("capabilities", {})
        if not isinstance(capabilities_raw, Mapping):
            raise ValueError(f"providers.{name}.capabilities must be a table")
        capabilities: dict[str, bool] = {}
        for capability, enabled in capabilities_raw.items():
            if str(capability) not in _CAPABILITY_NAMES:
                raise ValueError(
                    f"providers.{name}.capabilities.{capability} is not a known capability"
                )
            if not isinstance(enabled, bool):
                raise ValueError(f"providers.{name}.capabilities.{capability} must be boolean")
            capabilities[str(capability)] = enabled
        request_raw = raw.get("request_options")
        request_options = (
            RequestOptionSettings.from_mapping(
                request_raw, path=f"providers.{name}.request_options"
            )
            if isinstance(request_raw, Mapping)
            else None
        )
        if request_raw is not None and request_options is None:
            raise ValueError(f"providers.{name}.request_options must be a table")
        if provider == "ollama" and request_options is not None:
            unsupported = {
                field
                for field, value in (
                    ("extra_headers", request_options.extra_headers),
                    ("extra_query", request_options.extra_query),
                    ("provider", request_options.provider),
                )
                if value
            }
            if unsupported:
                names = ", ".join(sorted(unsupported))
                raise ValueError(
                    f"providers.{name}.request_options contains unsupported Ollama field(s): {names}; "
                    "use extra_body for Ollama payload extensions"
                )
        return cls(
            name=name,
            provider=provider,
            model=str(raw.get("model", "")).strip(),
            base_url=_validate_base_url(
                str(raw.get("base_url", "")), path=f"providers.{name}.base_url"
            ),
            api_key=api_key,
            api_key_env=api_key_env,
            default_headers=headers,
            default_query=option_map("default_query"),
            client_options=client_options,
            request_options=request_options,
            timeout_s=timeout,
            max_retries=retries,
            temperature=temperature,
            top_p=top_p,
            max_completion_tokens=max_completion,
            protocol=protocol,
            capabilities=capabilities,
        )

    def redacted(self) -> dict[str, object]:
        def redact(value: str | SecretReference) -> object:
            return value.redacted() if isinstance(value, SecretReference) else "<redacted>"

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": redact(self.api_key) if self.api_key is not None else None,
            "api_key_env": self.api_key_env,
            "default_headers": {
                name: redact(value) for name, value in self.default_headers.items()
            },
            "default_query": _redact_value(self.default_query),
            "client_options": _redact_value(self.client_options),
            "request_options": self.request_options.redacted() if self.request_options else None,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_completion_tokens": self.max_completion_tokens,
            "protocol": self.protocol,
            "capabilities": dict(self.capabilities),
        }

    def resolve(self, environ: Mapping[str, str] | None = None) -> "ResolvedProviderProfile":
        import os  # noqa: PLC0415

        env = environ if environ is not None else os.environ
        api_key: str | None
        if isinstance(self.api_key, SecretReference):
            api_key = self.api_key.resolve(env)
        elif self.api_key is not None:
            api_key = self.api_key
        elif self.api_key_env:
            api_key = SecretReference(self.api_key_env).resolve(env)
        else:
            api_key = env.get(PROVIDER_API_KEY_ENVVAR.get(self.provider, "")) or None
        headers = {
            name: _resolve_header_value(
                value,
                environ=env,
                path=f"providers.{self.name}.default_headers.{name}",
            )
            for name, value in self.default_headers.items()
        }
        request_options = self.request_options.resolve(env) if self.request_options else None
        return ResolvedProviderProfile(
            name=self.name,
            provider=self.provider,
            model=self.model or PROVIDER_DEFAULT_MODELS[self.provider],
            base_url=self.base_url,
            api_key=api_key,
            default_headers=MappingProxyType(dict(headers)),
            default_query=_freeze_mapping(self.default_query),
            client_options=_freeze_mapping(self.client_options),
            request_options=request_options,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
            temperature=self.temperature,
            top_p=self.top_p,
            max_completion_tokens=self.max_completion_tokens,
            protocol=self.protocol
            or ("openai-compatible" if self.provider == "openai" else self.provider),
            capabilities=MappingProxyType(dict(self.capabilities)),
        )


@dataclass(frozen=True, slots=True)
class ResolvedProviderProfile:
    """Runtime provider profile with secrets resolved just before an LLM call."""

    name: str
    provider: str
    model: str
    base_url: str
    api_key: str | None
    default_headers: Mapping[str, str]
    default_query: Mapping[str, object]
    client_options: Mapping[str, object]
    request_options: object | None
    timeout_s: float | None
    max_retries: int | None
    temperature: float | None
    top_p: float | None
    max_completion_tokens: int | None
    protocol: str
    capabilities: Mapping[str, bool]


@dataclass
class ExecutionSettings:
    max_concurrent_intents: int = 8
    max_parallel_tasks: int = 4
    agent_pool_size: int = 16
    max_agent_turns: int = 200  # max agentic-loop iterations per intent
    authoring_max_generation_attempts: int = 20
    """Maximum complete source-generation attempts for ``create_*`` workflows."""

    authoring_max_phase_turns: int = 20
    """Maximum agent sub-turns allowed in one ``create_*`` phase by default."""

    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS
    """Completion-token ceiling for a single LLM round-trip.

    This is the model's output budget per sub-turn, passed to lauren-ai as
    ``AgentConfig.max_tokens_per_turn``, and the amount reserved from the context
    window by :meth:`effective_usable_budget`.

    It must be large enough for the biggest single tool call a phase can make.
    lauren-ai defaults to 4096, which silently truncates a ``write_file`` call
    carrying a whole source file: the partial tool call is discarded, the turn
    produces nothing, and the phase retries forever. 32768 fits a larger
    file; raise it further for a model that supports it, and prefer chunked
    ``write_file`` + ``append_file`` writes for anything larger.
    """
    turn_timeout_s: float = 0.0  # per-turn watchdog; 0 = no limit
    # Conversation compaction.  auto_compact gates the proactive LLM compaction
    # ladder in lauren-ai's runner (PRD-135): when True, summarisation fires each
    # turn at ``summarize_at`` of the live window, before the hard pre-send guard
    # would lossily truncate.  There is no separate token threshold — the trigger
    # is the model-aware live-window budget, measured exactly (PRD-133/135).
    auto_compact: bool = True
    # Per-model context windows (PRD-136).  The SINGLE source of truth for the
    # context system — it replaces both the old scalar model_context_window and
    # session_memory_max_tokens.  Populated from the ``[memory.context_windows]``
    # TOML table: keys are model ids (lower-cased), values are token windows, and
    # the reserved key ``"default"`` is the fallback for unknown / proxied models.
    # The resolved window drives the hard pre-send guard, the summariser chunk
    # size, AND the live working window; ``summarize_at`` is the working-set dial.
    context_windows: dict[str, int] = field(default_factory=dict)
    # Context reuse (PRD-132).  prompt_cache: incremental prompt caching of the
    # system prompt, tools, and conversation prefix (Anthropic; no-op elsewhere)
    # so the file-heavy history is not re-billed every turn.  file_cache: a
    # durable, freshness-validated cache of file reads.
    prompt_cache: bool = True
    file_cache: bool = True
    # Transport retry on transient network errors (PRD-126)
    # Two independent layers (see prd-126):
    #   transport_max_retries — TURN-level retry with memory snapshot-rollback;
    #     the primary, memory-safe mechanism for mid-stream ReadTimeouts.
    #   llm_sdk_max_retries — SDK/transport internal retry inside LLMConfig;
    #     handles clean pre-stream 429/5xx.  Kept low to avoid a large
    #     multiplier with the turn-level retry.
    transport_max_retries: int = 3
    transport_retry_base_delay_s: float = 1.0
    transport_retry_max_total_s: float = 0.0  # wall-clock ceiling; 0 = no cap
    llm_sdk_max_retries: int = 2
    # LLM provider selection
    profile: str = ""
    provider: str = "anthropic"
    model: str = ""  # empty → use PROVIDER_DEFAULT_MODELS[provider]
    api_key: str | SecretReference = ""  # empty → read from provider env
    api_key_env: str = ""  # optional explicit environment variable for legacy execution config
    base_url: str = ""  # Ollama / self-hosted endpoint override
    default_headers: dict[str, str | SecretReference] = field(default_factory=dict, repr=False)
    default_query: dict[str, object] = field(default_factory=dict, repr=False)
    client_options: dict[str, object] = field(default_factory=dict, repr=False)
    request_options: object | None = field(default=None, repr=False)
    timeout_s: float = 60.0
    temperature: float = 1.0
    top_p: float | None = None
    max_completion_tokens: int | None = None
    provider_capabilities: dict[str, bool] = field(default_factory=dict, repr=False)
    _resolved_profile: ResolvedProviderProfile | None = field(
        default=None, repr=False, compare=False
    )

    def effective_model(self) -> str:
        return self.model or PROVIDER_DEFAULT_MODELS.get(self.provider, self.model)

    def effective_api_key(self) -> str | None:
        import os  # noqa: PLC0415

        if self._resolved_profile is not None:
            return self._resolved_profile.api_key
        if isinstance(self.api_key, SecretReference):
            return self.api_key.resolve()
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "") or None
        env_var = PROVIDER_API_KEY_ENVVAR.get(self.provider)
        return os.environ.get(env_var, "") or None if env_var else None

    def effective_context_window(self) -> int:
        """Total context-window size (in tokens) of the active model (PRD-136).

        Resolution order (most specific wins):

        1. explicit ``[memory.context_windows]`` entry for the model (exact id);
        2. lauren-ai's built-in :data:`~lauren_ai._config.MODEL_CONTEXT_WINDOWS`
           registry (accurate for known models);
        3. the config ``default`` key (the user's catch-all for unknown /
           proxied models);
        4. lauren-ai's hardcoded :data:`~lauren_ai._config.DEFAULT_CONTEXT_WINDOW`.

        :return: Context-window size in tokens.
        :rtype: int
        """
        model = self.effective_model().lower()
        if model in self.context_windows:
            return self.context_windows[model]
        known = _context_window_for(model, default=0)  # 0 → registry doesn't know it
        if known:
            return known
        if "default" in self.context_windows:
            return self.context_windows["default"]
        return _context_window_for(model)  # hardcoded library default

    def effective_usable_budget(self) -> int:
        """The live-window token budget derived from the model's context window.

        ``window − completion reservation − head-room``.  This is what the
        session :class:`~lauren_ai._memory.ShortTermMemory` is sized to (the
        budget ``messages()`` trims to and that auto-compaction defends), and it
        matches lauren-ai's ``AgentConfig.usable_context_budget`` so the live
        window and the hard pre-send guard agree.  PRD-136: there is no separate
        live-window setting — this is derived entirely from the window.

        :return: Usable input-token budget for the live conversation window.
        :rtype: int
        """
        window = self.effective_context_window()
        reserve = max(_CONTEXT_RESERVE_MIN, window // 25)
        completion = max(1, self.max_output_tokens)
        return max(1, window - completion - reserve)


@dataclass
class CloakBrowserSettings:
    """Optional browser automation settings.

    The Python package is deliberately not imported here.  This dataclass is
    safe to construct in a base installation where the ``cloakbrowser`` extra
    is not installed.  ``BrowserSessionManager`` performs the lazy dependency
    check when a browser tool is actually used.
    """

    # Browser automation is enabled and unrestricted by default for the
    # operator's local sandbox/VPS.  ``enabled`` remains an independent
    # switch, so deployments that need a deny-by-default browser can disable
    # the backend explicitly.
    enabled: bool = True
    transport: str = "local"
    cdp_endpoint: str = "http://127.0.0.1:9222"
    allowed_domains: list[str] = field(default_factory=list)
    headless: bool = True
    navigation_timeout_s: float = 15.0
    action_timeout_s: float = 10.0
    max_pages: int = 4
    max_actions_per_turn: int = 20
    max_snapshot_chars: int = 20_000
    max_screenshot_bytes: int = 10_000_000
    allow_persistent_profiles: bool = False
    profile_root: str = ".agenthicc/browser-profiles"
    license_key_env: str = "CLOAKBROWSER_LICENSE_KEY"
    allow_all_domains: bool = True

    def __post_init__(self) -> None:
        """Validate bounds before the settings reach a browser adapter."""
        self.transport = self.transport.strip().lower()
        if self.transport not in {"local", "cdp"}:
            raise ValueError("tools.cloakbrowser.transport must be 'local' or 'cdp'")
        if self.transport == "cdp":
            try:
                parsed_endpoint = urlsplit(self.cdp_endpoint.strip())
                endpoint_port = parsed_endpoint.port
            except ValueError as exc:
                raise ValueError(
                    "tools.cloakbrowser.cdp_endpoint must be an HTTP(S) loopback URL"
                ) from exc
            if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
                raise ValueError("tools.cloakbrowser.cdp_endpoint must be an HTTP(S) loopback URL")
            if (
                parsed_endpoint.username is not None
                or parsed_endpoint.password is not None
                or parsed_endpoint.path not in {"", "/"}
                or parsed_endpoint.query
                or parsed_endpoint.fragment
            ):
                raise ValueError(
                    "tools.cloakbrowser.cdp_endpoint must not contain credentials, a path, "
                    "a query, or a fragment"
                )
            if parsed_endpoint.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("tools.cloakbrowser.cdp_endpoint must use a loopback host")
            if endpoint_port is not None and not 1 <= endpoint_port <= 65535:
                raise ValueError("tools.cloakbrowser.cdp_endpoint has an invalid port")
        if self.navigation_timeout_s <= 0 or self.action_timeout_s <= 0:
            raise ValueError("CloakBrowser timeouts must be greater than zero")
        if self.max_pages < 1:
            raise ValueError("tools.cloakbrowser.max_pages must be at least 1")
        if self.max_actions_per_turn < 1:
            raise ValueError("tools.cloakbrowser.max_actions_per_turn must be at least 1")
        if self.max_snapshot_chars < 256:
            raise ValueError("tools.cloakbrowser.max_snapshot_chars must be at least 256")
        if self.max_screenshot_bytes < 1024:
            raise ValueError("tools.cloakbrowser.max_screenshot_bytes must be at least 1024")
        self.allowed_domains = sorted(
            {
                domain.strip().lower().lstrip(".")
                for domain in self.allowed_domains
                if domain.strip()
            }
        )
        profile_path = Path(self.profile_root)
        if (
            not self.profile_root.strip()
            or profile_path.is_absolute()
            or ".." in profile_path.parts
        ):
            raise ValueError(
                "tools.cloakbrowser.profile_root must be a non-empty relative path "
                "inside the workspace"
            )
        if not self.license_key_env.strip():
            raise ValueError("tools.cloakbrowser.license_key_env must not be empty")


@dataclass
class PlaywrightSettings:
    """Optional Playwright browser automation settings.

    Playwright is deliberately an optional dependency.  The settings object
    is safe to construct without the package or its browser binaries; the
    transport reports a structured dependency/runtime diagnostic only when it
    is selected and used.
    """

    enabled: bool = True
    transport: str = "local"
    browser_type: str = "chromium"
    browser_channel: str = ""
    executable_path: str = ""
    allowed_domains: list[str] = field(default_factory=list)
    headless: bool = True
    navigation_timeout_s: float = 15.0
    action_timeout_s: float = 10.0
    max_pages: int = 4
    max_actions_per_turn: int = 20
    max_snapshot_chars: int = 20_000
    max_screenshot_bytes: int = 10_000_000
    allow_persistent_profiles: bool = False
    profile_root: str = ".agenthicc/browser-profiles/playwright"
    allow_all_domains: bool = True

    def __post_init__(self) -> None:
        self.transport = self.transport.strip().lower()
        if self.transport != "local":
            raise ValueError("tools.playwright.transport must be 'local'")
        self.browser_type = self.browser_type.strip().lower()
        if self.browser_type not in {"chromium", "firefox", "webkit"}:
            raise ValueError(
                "tools.playwright.browser_type must be 'chromium', 'firefox', or 'webkit'"
            )
        if self.navigation_timeout_s <= 0 or self.action_timeout_s <= 0:
            raise ValueError("Playwright timeouts must be greater than zero")
        if self.max_pages < 1:
            raise ValueError("tools.playwright.max_pages must be at least 1")
        if self.max_actions_per_turn < 1:
            raise ValueError("tools.playwright.max_actions_per_turn must be at least 1")
        if self.max_snapshot_chars < 256:
            raise ValueError("tools.playwright.max_snapshot_chars must be at least 256")
        if self.max_screenshot_bytes < 1024:
            raise ValueError("tools.playwright.max_screenshot_bytes must be at least 1024")
        self.allowed_domains = sorted(
            {
                domain.strip().lower().lstrip(".")
                for domain in self.allowed_domains
                if domain.strip()
            }
        )
        profile_path = Path(self.profile_root)
        if (
            not self.profile_root.strip()
            or profile_path.is_absolute()
            or ".." in profile_path.parts
        ):
            raise ValueError(
                "tools.playwright.profile_root must be a non-empty relative path "
                "inside the workspace"
            )
        if self.browser_channel and self.browser_type != "chromium":
            raise ValueError("tools.playwright.browser_channel is supported only for chromium")


@dataclass
class ToolSettings:
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    max_live_tool_calls: int = 5
    group_exploratory_calls: bool = True
    """Render marked contiguous read-only calls as one ``Explored`` block."""
    http_timeout_s: float = 30.0
    cloakbrowser: CloakBrowserSettings = field(default_factory=CloakBrowserSettings)
    playwright: PlaywrightSettings = field(default_factory=PlaywrightSettings)
    browser_backend: str = "cloakbrowser"
    """Read timeout in seconds for all outbound HTTP tool calls (PRD-108).
    Set via ``[tools] http_timeout_s = N`` in agenthicc.toml.
    Use ``0.0`` to disable the read timeout (unbounded)."""
    """Maximum tool completions rendered individually in the scroll buffer
    before collapsing the rest into a live "…and N more tool calls" indicator.
    Set via [tools] max_live_tool_calls = N in agenthicc.toml."""

    def __post_init__(self) -> None:
        self.browser_backend = self.browser_backend.strip().lower()
        if self.browser_backend not in {"cloakbrowser", "playwright", "none"}:
            raise ValueError(
                "tools.browser_backend must be 'cloakbrowser', 'playwright', or 'none'"
            )


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
class PluginSettings:
    """[plugins] section — tool plugin security and dependency settings."""

    auto_trust: bool = False
    auto_install: bool = False
    install_target: str = "venv"
    allowed_modules: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    disabled: list[str] = field(default_factory=list)
    trust_file: str = ".agenthicc/trusted_plugins.json"
    audit_file: str = ".agenthicc/plugin_audit.jsonl"
    strict_cli_shadow: bool = False


@dataclass
class BehaviourSettings:
    """[behaviour] section — non-security developer convenience defaults.

    These MAY live in TOML.  Security-bypassing flags must NOT live here —
    they belong in CLIFlags (cli/context.py) so they can never be silently
    persisted across invocations.
    """

    verbose: bool = False
    confirm_exits: bool = True
    resume_transcript_turns: int = 20
    """Number of newest turns to replay when opening an existing session.

    This bounds only the visual transcript and input history.  ``0`` keeps
    the complete-history behavior; durable provider memory and session logs
    are never deleted by this setting.
    """


@dataclass
class AgentSettings:
    """Per-agent TOML metadata and skill activation policy."""

    description: str = ""
    model: str = ""
    max_turns: int = 200
    allowed_skills: tuple[str, ...] | None = None
    denied_skills: tuple[str, ...] = ()

    def skill_permissions(self) -> "SkillPermissionSet":
        """Return the loader-owned permission value without a module cycle."""

        from agenthicc.skills.loader import SkillPermissionSet

        allowed = None if self.allowed_skills is None else frozenset(self.allowed_skills)
        return SkillPermissionSet(
            allowed_skills=allowed, denied_skills=frozenset(self.denied_skills)
        )


@dataclass
class AgentsSettings:
    """[agents] section — keyed by agent slug."""

    agents: dict[str, AgentSettings] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "AgentsSettings":
        """Parse agent metadata while accepting legacy skill permission keys."""

        def strings(value: object) -> tuple[str, ...] | None:
            if value is None:
                return None
            if isinstance(value, str):
                return (value,)
            if isinstance(value, list):
                return tuple(str(item) for item in value)
            return None

        parsed: dict[str, AgentSettings] = {}
        for name, raw_cfg in d.items():
            if not isinstance(raw_cfg, Mapping):
                continue
            raw_skill_policy = raw_cfg.get("skills")
            skill_policy = raw_skill_policy if isinstance(raw_skill_policy, Mapping) else {}
            allowed_value = raw_cfg.get(
                "allowed_skills",
                raw_cfg.get("skills_allow", skill_policy.get("allow")),
            )
            denied_value = raw_cfg.get(
                "denied_skills",
                raw_cfg.get("skills_deny", skill_policy.get("deny")),
            )
            allowed = strings(allowed_value)
            denied = strings(denied_value) or ()
            parsed[str(name)] = AgentSettings(
                description=str(raw_cfg.get("description", "")),
                model=str(raw_cfg.get("model", "")),
                max_turns=int(raw_cfg.get("max_turns", 200)),
                allowed_skills=allowed,
                denied_skills=denied,
            )
        return cls(agents=parsed)

    def skill_permissions_for(self, agent_type: str) -> "SkillPermissionSet":
        """Return configured skill permissions for *agent_type*."""

        settings = self.agents.get(agent_type) or self.agents.get("default")
        if settings is None and agent_type == "default":
            settings = self.agents.get("auto")
        return (
            settings.skill_permissions() if settings is not None else _default_skill_permissions()
        )


def _default_skill_permissions() -> "SkillPermissionSet":
    from agenthicc.skills.loader import SkillPermissionSet

    return SkillPermissionSet()


@dataclass
class SkillsSettings:
    """[skills] section — default skill bootstrap configuration."""

    install_default_skills: bool = True
    default_skill_directory: str = ""  # empty = ~/.agenthicc/skills


@dataclass
class StorageS3Settings:
    """S3/S3-compatible storage credentials and configuration."""

    bucket: str = ""
    region: str = "us-east-1"
    prefix: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    endpoint_url: str = ""
    profile: str = ""
    path_style: bool = False
    mounts: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.bucket)


@dataclass
class StorageSettings:
    """Top-level storage configuration (S3 and future backends)."""

    s3: StorageS3Settings = field(default_factory=StorageS3Settings)
    default_backend: str = "linux"


@dataclass
class AgenthiccConfig:
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    providers: dict[str, ProviderProfile] = field(default_factory=dict)
    behaviour: BehaviourSettings = field(default_factory=BehaviourSettings)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    tools: ToolSettings = field(default_factory=ToolSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    plugins: PluginSettings = field(default_factory=PluginSettings)
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    agents: AgentsSettings = field(default_factory=AgentsSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    workflows: dict[str, dict[str, object]] = field(default_factory=dict)
    """Per-workflow tunable parameter overrides loaded from ``[workflows.<name>]``
    TOML sections (PRD-111).  E.g. ``cfg.workflows["code_plan"]["execute_model"]``."""

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

    def resolve_provider_profile(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        strict: bool = True,
        requires_tools: bool = False,
        requires_streaming: bool = True,
    ) -> ResolvedProviderProfile | None:
        """Resolve and activate the selected profile for this config.

        Resolution is intentionally explicit and late.  A profile name and
        all non-secret settings can safely remain in a workflow/session
        object; the environment is consulted again when a session is resumed.
        """
        profile_name = self.execution.profile.strip()
        import os  # noqa: PLC0415

        env = environ if environ is not None else os.environ
        if not profile_name:
            if strict:
                provider = self.execution.provider.lower()
                if provider not in SUPPORTED_PROVIDERS:
                    supported = ", ".join(SUPPORTED_PROVIDERS)
                    raise ValueError(f"execution.provider must be one of: {supported}")
                _validate_base_url(self.execution.base_url, path="execution.base_url")
                if isinstance(self.execution.api_key, SecretReference):
                    self.execution.api_key.resolve(env)
                _resolve_headers(
                    self.execution.default_headers,
                    environ=env,
                    path="execution.default_headers",
                )
                if isinstance(self.execution.request_options, RequestOptionSettings):
                    self.execution.request_options.resolve(env)
            return None
        profile = self.providers.get(profile_name)
        if profile is None:
            available = ", ".join(sorted(self.providers)) or "none"
            raise ValueError(
                f"Unknown provider profile {profile_name!r}; available profiles: {available}"
            )
        resolved = profile.resolve(environ)
        from dataclasses import replace  # noqa: PLC0415

        if profile.api_key is None and not profile.api_key_env:
            if isinstance(self.execution.api_key, SecretReference):
                resolved = replace(resolved, api_key=self.execution.api_key.resolve(env))
            elif self.execution.api_key:
                resolved = replace(resolved, api_key=self.execution.api_key)
            elif self.execution.api_key_env:
                resolved = replace(
                    resolved,
                    api_key=SecretReference(self.execution.api_key_env).resolve(env),
                )
        resolved = replace(
            resolved,
            model=profile.model or self.execution.model or resolved.model,
            base_url=profile.base_url
            or _validate_base_url(self.execution.base_url, path="execution.base_url"),
            default_headers=MappingProxyType(
                dict(resolved.default_headers)
                if profile.default_headers
                else _resolve_headers(
                    self.execution.default_headers,
                    environ=env,
                    path="execution.default_headers",
                )
            ),
            default_query=_freeze_mapping(
                profile.default_query if profile.default_query else self.execution.default_query
            ),
            client_options=_freeze_mapping(
                profile.client_options if profile.client_options else self.execution.client_options
            ),
            request_options=(
                resolved.request_options
                if profile.request_options is not None
                else (
                    self.execution.request_options.resolve(env)
                    if isinstance(self.execution.request_options, RequestOptionSettings)
                    else self.execution.request_options
                )
            ),
            timeout_s=profile.timeout_s
            if profile.timeout_s is not None
            else self.execution.timeout_s,
            max_retries=profile.max_retries
            if profile.max_retries is not None
            else self.execution.llm_sdk_max_retries,
            temperature=profile.temperature
            if profile.temperature is not None
            else self.execution.temperature,
            top_p=profile.top_p if profile.top_p is not None else self.execution.top_p,
            max_completion_tokens=(
                profile.max_completion_tokens
                if profile.max_completion_tokens is not None
                else self.execution.max_completion_tokens
            ),
        )
        if resolved.timeout_s is None or resolved.timeout_s <= 0:
            raise ValueError(f"providers.{profile_name}.timeout_s must be greater than zero")
        if requires_tools and resolved.capabilities.get("tools") is False:
            raise ValueError(
                f"providers.{profile_name}.capabilities.tools=false cannot run a tool-enabled session"
            )
        if requires_streaming and resolved.capabilities.get("streaming") is False:
            raise ValueError(
                f"providers.{profile_name}.capabilities.streaming=false cannot run the streaming session"
            )
        self.execution.provider = resolved.provider
        self.execution.model = resolved.model
        self.execution.base_url = resolved.base_url
        self.execution._resolved_profile = resolved
        if resolved.timeout_s is not None:
            self.execution.timeout_s = resolved.timeout_s
        if resolved.temperature is not None:
            self.execution.temperature = resolved.temperature
        if resolved.top_p is not None:
            self.execution.top_p = resolved.top_p
        if resolved.max_completion_tokens is not None:
            self.execution.max_completion_tokens = resolved.max_completion_tokens
        if resolved.max_retries is not None:
            self.execution.llm_sdk_max_retries = resolved.max_retries
        self.execution.default_headers = dict(resolved.default_headers)
        self.execution.default_query = dict(resolved.default_query)
        self.execution.client_options = dict(resolved.client_options)
        self.execution.provider_capabilities = dict(resolved.capabilities)
        self.execution.request_options = resolved.request_options
        if strict and not resolved.model:
            raise ValueError(f"Provider profile {profile_name!r} did not resolve a model")
        return resolved

    def redacted_dict(self) -> dict[str, object]:
        """Return a CLI-safe representation without resolved secret values."""
        execution = {
            key: _redact_value(value, key=key)
            for key, value in vars(self.execution).items()
            if not key.startswith("_") and key not in {"api_key", "default_headers"}
        }
        execution["api_key"] = (
            self.execution.api_key.redacted()
            if isinstance(self.execution.api_key, SecretReference)
            else ("<redacted>" if self.execution.api_key else None)
        )
        execution["default_headers"] = {
            name: value.redacted() if isinstance(value, SecretReference) else "<redacted>"
            for name, value in self.execution.default_headers.items()
        }
        if isinstance(self.execution.request_options, RequestOptionSettings):
            execution["request_options"] = self.execution.request_options.redacted()
        elif self.execution.request_options is not None:
            # lauren-ai's immutable RequestOptions owns its own redacted
            # diagnostic representation; do not call repr on it here.
            execution["request_options"] = "<configured>"
        tool_values = vars(self.tools).copy()
        tool_values["mcp_servers"] = [
            server.redacted() if hasattr(server, "redacted") else "<configured>"
            for server in self.tools.mcp_servers
        ]
        return {
            "execution": execution,
            "providers": {name: profile.redacted() for name, profile in self.providers.items()},
            "behaviour": vars(self.behaviour).copy(),
            "memory": vars(self.memory).copy(),
            "security": vars(self.security).copy(),
            "api": vars(self.api).copy(),
            "tools": tool_values,
            "plugins": vars(self.plugins).copy(),
        }


def _parse_mcp_servers(raw_list: list[dict[str, object]]) -> list[McpServerConfig]:
    """Convert raw TOML dicts to McpServerConfig objects (graceful if mcp.py unavailable)."""
    try:
        from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415

        servers = [McpServerConfig.from_dict(d) for d in raw_list]
        names = [server.name for server in servers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError("duplicate MCP server name(s): " + ", ".join(duplicates))
        return servers
    except ImportError:
        return []


# ── merging ──────────────────────────────────────────────────────────────


def deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Recursively merge ``override`` into ``base``.

    Nested dicts merge recursively; scalars and lists in ``override``
    replace the corresponding ``base`` values.
    """
    result = dict(base)
    for key, value in override.items():
        base_value = result.get(key)
        if key == "mcp_servers" and isinstance(base_value, list) and isinstance(value, list):
            result[key] = _merge_named_mcp_servers(base_value, value)
            continue
        if isinstance(base_value, dict) and isinstance(value, dict):
            result[key] = deep_merge(
                _section(base_value),
                _section(value),
            )
        else:
            result[key] = value
    return result


def _merge_named_mcp_servers(
    base: list[object], override: list[object]
) -> list[object]:
    """Merge MCP arrays by stable server name instead of array position."""
    merged: list[object] = list(base)
    positions = {
        item.get("name"): index
        for index, item in enumerate(merged)
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    for item in override:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            merged.append(item)
            continue
        name = item["name"]
        index = positions.get(name)
        if index is None:
            positions[name] = len(merged)
            merged.append(dict(item))
        else:
            merged[index] = dict(item)
    return merged


def _section(value: object) -> dict[str, object]:
    """Return a string-keyed copy of an untrusted TOML section."""
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_string_list(value: object, default: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return list(default)


def _flatten_hooks(data: dict[str, object], prefix: str = "") -> dict[str, list[str]]:
    """Flatten nested hook tables into ``{"intent.pre_validate": [...]}`` form."""
    flat: dict[str, list[str]] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_hooks(value, dotted))
        else:
            flat[dotted] = [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return flat


# ── config file discovery helpers ─────────────────────────────────────────


def _find_config_file(candidates: list[Path]) -> Path | None:
    """Return the first candidate path that exists, or None."""
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_toml_safe(path: Path) -> dict[str, object]:
    """Load TOML file; return {} on error, warn on invalid syntax."""
    import warnings  # noqa: PLC0415

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, PermissionError):
        return {}
    except tomllib.TOMLDecodeError as exc:
        warnings.warn(f"Invalid TOML in {path}: {exc}", stacklevel=3)
        return {}


# ── environment / CLI override helpers ───────────────────────────────────


def _coerce_env(value: str) -> bool | int | float | str:
    """Coerce an env var string to int / bool / float / str."""
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _apply_env_overrides(config: dict[str, object]) -> dict[str, object]:
    """Apply AGENTHICC_<SECTION>_<KEY> environment variables and provider shortcuts.

    Env vars always override both user-global and per-project config files.
    Within env vars, ``AGENTHICC_*`` takes priority over provider shorthands.
    """
    import os  # noqa: PLC0415

    # 1. AGENTHICC_* namespace — highest env priority, always overrides config files.
    agenthicc_set: set[tuple[str, str]] = set()
    for key, value in os.environ.items():
        if not key.startswith("AGENTHICC_"):
            continue
        remainder = key[len("AGENTHICC_") :].lower()
        parts = remainder.split("_", 1)
        if len(parts) != 2:
            continue
        section, field_name = parts
        section_config = _section(config.get(section))
        section_config[field_name] = _coerce_env(value)
        config[section] = section_config
        agenthicc_set.add((section, field_name))

    # 2. Provider-specific shorthand env vars (OPENAI_MODEL, OPENAI_BASE_URL, etc.)
    #    These override per-project config — env vars win over config files.
    #    They yield only to an explicit AGENTHICC_* var (already applied above).
    explicit_value = _section(config.get("execution")).get("provider")
    explicit_provider = explicit_value if isinstance(explicit_value, str) else None
    inferred_provider: str | None = None

    for env_var, (section, field_name) in PROVIDER_ENV_SHORTCUTS.items():
        env_value = os.environ.get(env_var)
        if not env_value:
            continue
        # Skip only if an AGENTHICC_* var already set this exact field.
        if (section, field_name) in agenthicc_set:
            continue
        section_config = _section(config.get(section))
        section_config[field_name] = env_value
        config[section] = section_config
        # Infer provider from which shorthand var was set (e.g. OPENAI_MODEL → openai)
        if inferred_provider is None:
            prefix = env_var.split("_")[0].lower()  # "OPENAI_MODEL" → "openai"
            if prefix in SUPPORTED_PROVIDERS:
                inferred_provider = prefix

    # 3. Auto-infer provider from API key env vars if still unset
    if inferred_provider is None and not explicit_provider:
        for provider, api_key_var in PROVIDER_API_KEY_ENVVAR.items():
            if os.environ.get(api_key_var):
                inferred_provider = provider
                break  # first match wins (ANTHROPIC_API_KEY checked first)

    # Apply inferred provider only when no explicit provider was set
    if inferred_provider and not explicit_provider:
        execution = _section(config.get("execution"))
        execution.setdefault("provider", inferred_provider)
        config["execution"] = execution

    return config


def _apply_cli_overrides(config: dict[str, object], overrides: list[str]) -> dict[str, object]:
    """Apply dotted ``--set`` overrides (highest priority)."""
    for override in overrides:
        if "=" not in override:
            continue
        key_path, _, value_str = override.partition("=")
        parts = [part.strip() for part in key_path.strip().split(".") if part.strip()]
        if len(parts) < 2:
            continue
        target = config
        for part in parts[:-1]:
            nested = target.get(part)
            child = (
                {str(key): item for key, item in nested.items()}
                if isinstance(nested, Mapping)
                else {}
            )
            target[part] = child
            target = child
        target[parts[-1]] = _coerce_env(value_str)
    return config


def _apply_cli_secret_overrides(
    config: dict[str, object], overrides: list[str]
) -> dict[str, object]:
    """Apply ``--set-secret PATH=ENV_VAR`` overrides as symbolic references."""
    for override in overrides:
        if "=" not in override:
            raise ValueError("--set-secret expects KEY=ENV_VAR")
        key_path, _, env_name = override.partition("=")
        parts = [part.strip() for part in key_path.strip().split(".") if part.strip()]
        if len(parts) < 2:
            raise ValueError("--set-secret KEY must be a dotted configuration path")
        env_name = _validate_env_name(env_name, path=f"--set-secret {key_path}.ENV_VAR")
        target = config
        for part in parts[:-1]:
            nested = target.get(part)
            child = (
                {str(key): item for key, item in nested.items()}
                if isinstance(nested, Mapping)
                else {}
            )
            target[part] = child
            target = child
        target[parts[-1]] = {"env": env_name}
    return config


# ── loading ──────────────────────────────────────────────────────────────


class ConfigExtendsCycleError(Exception):
    """Raised when an ``extends`` chain contains a cycle."""


def _read_toml(path: Path) -> dict[str, object]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _resolve_extends(
    path: Path,
    _seen: frozenset[Path] | None = None,
) -> dict[str, object]:
    """Read *path* and recursively resolve its ``extends`` chain (PRD-113).

    Returns a fully-merged dict: parents first (left-to-right), the current
    file's values applied on top.  The ``extends`` key is stripped from the
    returned dict so it never reaches ``_dict_to_config``.

    Parameters
    ----------
    path:
        Absolute or relative path to a TOML config file.
    _seen:
        Accumulated set of resolved absolute paths — used for cycle detection.
        Callers should not pass this; it is threaded through recursion.

    Raises
    ------
    ConfigExtendsCycleError
        When the extends chain forms a cycle.
    FileNotFoundError
        When a file named in ``extends`` does not exist.
    """
    resolved = path.resolve()
    seen = _seen if _seen is not None else frozenset()
    if resolved in seen:
        raise ConfigExtendsCycleError(
            f"Circular extends detected: {path} is already in the inheritance chain"
        )
    seen = seen | {resolved}

    data = _read_toml(path)
    extends_raw = data.pop("extends", None)

    if not extends_raw:
        return data

    # Normalise to list of strings
    if isinstance(extends_raw, str):
        parents = [extends_raw]
    elif isinstance(extends_raw, list):
        parents = [str(e) for e in extends_raw]
    else:
        import warnings  # noqa: PLC0415

        warnings.warn(
            f"Invalid 'extends' value in {path}: expected str or list, "
            f"ignoring (got {type(extends_raw).__name__})",
            stacklevel=2,
        )
        return data

    base_dir = path.parent
    merged: dict[str, object] = {}

    for parent_str in parents:
        parent_path = (base_dir / Path(parent_str).expanduser()).resolve()
        if not parent_path.is_file():
            raise FileNotFoundError(
                f"Config 'extends' refers to a non-existent file: {parent_path}"
                f" (referenced from {path})"
            )
        parent_data = _resolve_extends(parent_path, seen)
        merged = deep_merge(merged, parent_data)

    return deep_merge(merged, data)


def _load_toml_with_extends(path: Path) -> dict[str, object]:
    """Like ``_load_toml_safe`` but also resolves ``extends`` chains.

    Returns ``{}`` on ``FileNotFoundError`` / ``PermissionError``; warns on
    invalid TOML syntax.  Propagates ``ConfigExtendsCycleError``.
    """
    import warnings  # noqa: PLC0415

    try:
        return _resolve_extends(path)
    except (FileNotFoundError, PermissionError):
        return {}
    except ConfigExtendsCycleError:
        raise
    except tomllib.TOMLDecodeError as exc:
        warnings.warn(f"Invalid TOML in {path}: {exc}", stacklevel=3)
        return {}


def _dict_to_config(data: dict[str, object]) -> AgenthiccConfig:
    """Build an AgenthiccConfig from a merged dict."""
    ex = _section(data.get("execution"))
    me = _section(data.get("memory"))
    # PRD-136: per-model context windows live under [memory.context_windows] but
    # are resolved by ExecutionSettings (the model is an execution concern).  Keys
    # are lower-cased model ids (plus the reserved "default"); values are windows.
    _context_windows = {
        str(k).lower(): _as_int(v, 0) for k, v in _section(me.get("context_windows")).items()
    }
    execution_headers_raw = ex.get("default_headers", {})
    if not isinstance(execution_headers_raw, Mapping):
        raise ValueError("execution.default_headers must be a table")
    execution_headers: dict[str, str | SecretReference] = {}
    for header, value in execution_headers_raw.items():
        header_name = _validate_header_name(str(header), path="execution.default_headers")
        execution_headers[header_name] = _parse_secret_or_string(
            value, path=f"execution.default_headers.{header_name}"
        )
    execution_request_raw = ex.get("request_options")
    execution_request_options = (
        RequestOptionSettings.from_mapping(execution_request_raw, path="execution.request_options")
        if isinstance(execution_request_raw, Mapping)
        else None
    )
    if execution_request_raw is not None and execution_request_options is None:
        raise ValueError("execution.request_options must be a table")
    execution = ExecutionSettings(
        max_concurrent_intents=_as_int(ex.get("max_concurrent_intents"), 8),
        max_parallel_tasks=_as_int(ex.get("max_parallel_tasks"), 4),
        agent_pool_size=_as_int(ex.get("agent_pool_size"), 16),
        max_agent_turns=_as_int(ex.get("max_agent_turns"), 200),
        authoring_max_generation_attempts=_as_int(ex.get("authoring_max_generation_attempts"), 20),
        authoring_max_phase_turns=_as_int(ex.get("authoring_max_phase_turns"), 20),
        max_output_tokens=_as_int(ex.get("max_output_tokens"), _DEFAULT_MAX_OUTPUT_TOKENS),
        turn_timeout_s=_as_float(ex.get("turn_timeout_s"), 0.0),
        auto_compact=_as_bool(ex.get("auto_compact"), True),
        context_windows=_context_windows,
        prompt_cache=_as_bool(ex.get("prompt_cache"), True),
        file_cache=_as_bool(ex.get("file_cache"), True),
        transport_max_retries=_as_int(ex.get("transport_max_retries"), 3),
        transport_retry_base_delay_s=_as_float(ex.get("transport_retry_base_delay_s"), 1.0),
        transport_retry_max_total_s=_as_float(ex.get("transport_retry_max_total_s"), 0.0),
        llm_sdk_max_retries=_as_int(ex.get("llm_sdk_max_retries"), 2),
        profile=_as_str(ex.get("profile"), ""),
        provider=_as_str(ex.get("provider"), "anthropic"),
        model=_as_str(ex.get("model"), ""),
        api_key=(
            _parse_secret_or_string(ex["api_key"], path="execution.api_key")
            if "api_key" in ex
            else ""
        ),
        api_key_env=_as_str(ex.get("api_key_env"), ""),
        base_url=_as_str(ex.get("base_url"), ""),
        default_headers=execution_headers,
        default_query=cast(
            dict[str, object],
            _copy_option_value(_section(ex.get("default_query")), path="execution.default_query"),
        ),
        client_options=_validate_client_options(
            _section(ex.get("client_options")), path="execution.client_options"
        ),
        request_options=execution_request_options,
        timeout_s=_as_float(ex.get("timeout_s"), 60.0),
        temperature=_as_float(ex.get("temperature"), 1.0),
        top_p=_optional_float(ex.get("top_p"), "execution.top_p"),
        max_completion_tokens=(
            None
            if ex.get("max_completion_tokens") is None
            else _as_int(ex.get("max_completion_tokens"), -1)
        ),
    )
    if execution.timeout_s <= 0:
        raise ValueError("execution.timeout_s must be greater than zero")
    if execution.temperature < 0:
        raise ValueError("execution.temperature must be non-negative")
    if execution.top_p is not None and not 0 <= execution.top_p <= 1:
        raise ValueError("execution.top_p must be between 0 and 1")
    if execution.max_completion_tokens is not None and execution.max_completion_tokens < 0:
        raise ValueError("execution.max_completion_tokens must be non-negative")

    hooks = _flatten_hooks(_section(data.get("hooks")))

    to = _section(data.get("tools"))
    tools_raw = to.get("mcp_servers")
    raw_mcp = (
        [item for item in tools_raw if isinstance(item, dict)]
        if isinstance(tools_raw, list)
        else []
    )
    tools = ToolSettings(
        mcp_servers=_parse_mcp_servers(raw_mcp),
        plugins=_as_string_list(to.get("plugins")),
        allowed=_as_string_list(to.get("allowed", to.get("allowed_tools"))),
        denied=_as_string_list(to.get("denied", to.get("denied_tools"))),
        max_live_tool_calls=_as_int(to.get("max_live_tool_calls"), 5),
        group_exploratory_calls=_as_bool(to.get("group_exploratory_calls"), True),
        http_timeout_s=_as_float(to.get("http_timeout_s"), 30.0),
        browser_backend=_as_str(to.get("browser_backend"), "cloakbrowser"),
        cloakbrowser=CloakBrowserSettings(
            enabled=_as_bool(_section(to.get("cloakbrowser")).get("enabled"), True),
            transport=_as_str(_section(to.get("cloakbrowser")).get("transport"), "local"),
            cdp_endpoint=_as_str(
                _section(to.get("cloakbrowser")).get("cdp_endpoint"),
                "http://127.0.0.1:9222",
            ),
            allowed_domains=_as_string_list(
                _section(to.get("cloakbrowser")).get("allowed_domains")
            ),
            allow_all_domains=_as_bool(
                _section(to.get("cloakbrowser")).get("allow_all_domains"), True
            ),
            headless=_as_bool(_section(to.get("cloakbrowser")).get("headless"), True),
            navigation_timeout_s=_as_float(
                _section(to.get("cloakbrowser")).get("navigation_timeout_s"), 15.0
            ),
            action_timeout_s=_as_float(
                _section(to.get("cloakbrowser")).get("action_timeout_s"), 10.0
            ),
            max_pages=_as_int(_section(to.get("cloakbrowser")).get("max_pages"), 4),
            max_actions_per_turn=_as_int(
                _section(to.get("cloakbrowser")).get("max_actions_per_turn"), 20
            ),
            max_snapshot_chars=_as_int(
                _section(to.get("cloakbrowser")).get("max_snapshot_chars"), 20_000
            ),
            max_screenshot_bytes=_as_int(
                _section(to.get("cloakbrowser")).get("max_screenshot_bytes"), 10_000_000
            ),
            allow_persistent_profiles=_as_bool(
                _section(to.get("cloakbrowser")).get("allow_persistent_profiles"), False
            ),
            profile_root=_as_str(
                _section(to.get("cloakbrowser")).get("profile_root"),
                ".agenthicc/browser-profiles",
            ),
            license_key_env=_as_str(
                _section(to.get("cloakbrowser")).get("license_key_env"),
                "CLOAKBROWSER_LICENSE_KEY",
            ),
        ),
        playwright=PlaywrightSettings(
            enabled=_as_bool(_section(to.get("playwright")).get("enabled"), True),
            transport=_as_str(_section(to.get("playwright")).get("transport"), "local"),
            browser_type=_as_str(_section(to.get("playwright")).get("browser_type"), "chromium"),
            browser_channel=_as_str(_section(to.get("playwright")).get("browser_channel"), ""),
            executable_path=_as_str(_section(to.get("playwright")).get("executable_path"), ""),
            allowed_domains=_as_string_list(_section(to.get("playwright")).get("allowed_domains")),
            allow_all_domains=_as_bool(
                _section(to.get("playwright")).get("allow_all_domains"), True
            ),
            headless=_as_bool(_section(to.get("playwright")).get("headless"), True),
            navigation_timeout_s=_as_float(
                _section(to.get("playwright")).get("navigation_timeout_s"), 15.0
            ),
            action_timeout_s=_as_float(
                _section(to.get("playwright")).get("action_timeout_s"), 10.0
            ),
            max_pages=_as_int(_section(to.get("playwright")).get("max_pages"), 4),
            max_actions_per_turn=_as_int(
                _section(to.get("playwright")).get("max_actions_per_turn"), 20
            ),
            max_snapshot_chars=_as_int(
                _section(to.get("playwright")).get("max_snapshot_chars"), 20_000
            ),
            max_screenshot_bytes=_as_int(
                _section(to.get("playwright")).get("max_screenshot_bytes"), 10_000_000
            ),
            allow_persistent_profiles=_as_bool(
                _section(to.get("playwright")).get("allow_persistent_profiles"), False
            ),
            profile_root=_as_str(
                _section(to.get("playwright")).get("profile_root"),
                ".agenthicc/browser-profiles/playwright",
            ),
        ),
    )

    memory = MemorySettings(
        project_memory_path=_as_str(me.get("project_memory_path"), ".agenthicc/memory"),
        vector_db=_as_str(me.get("vector_db"), "sqlite-vec"),
        session_ttl_seconds=_as_int(me.get("session_ttl_seconds"), 86400),
    )

    se = _section(data.get("security"))
    security = SecuritySettings(
        sandbox_mode=_as_bool(se.get("sandbox_mode"), True),
        allowed_paths=_as_string_list(se.get("allowed_paths"), ("/workspace",)),
        network_allow_list=_as_string_list(se.get("network_allow_list")),
        max_tool_cpu_seconds=_as_int(se.get("max_tool_cpu_seconds"), 30),
        max_tool_memory_mb=_as_int(se.get("max_tool_memory_mb"), 512),
    )

    ap = _section(data.get("api"))
    api = ApiSettings(
        host=_as_str(ap.get("host"), "127.0.0.1"),
        port=_as_int(ap.get("port"), 8000),
        api_key_env=_as_str(ap.get("api_key_env"), "AGENTHICC_API_KEY"),
    )

    # Parse [storage] and [storage.s3] sections
    raw_storage = _section(data.get("storage"))
    raw_s3 = _section(raw_storage.pop("s3", {}))
    raw_mounts = _section(raw_s3.pop("mounts", {}))
    s3_settings = StorageS3Settings(
        bucket=_as_str(raw_s3.get("bucket"), ""),
        region=_as_str(raw_s3.get("region"), "us-east-1"),
        prefix=_as_str(raw_s3.get("prefix"), ""),
        access_key_id=_as_str(raw_s3.get("access_key_id"), ""),
        secret_access_key=_as_str(raw_s3.get("secret_access_key"), ""),
        endpoint_url=_as_str(raw_s3.get("endpoint_url"), ""),
        profile=_as_str(raw_s3.get("profile"), ""),
        path_style=_as_bool(raw_s3.get("path_style"), False),
        mounts={
            str(name): {
                str(field_name): _as_str(field_value, "")
                for field_name, field_value in _section(mount).items()
            }
            for name, mount in raw_mounts.items()
            if isinstance(mount, Mapping)
        },
    )
    storage_settings = StorageSettings(
        s3=s3_settings,
        default_backend=_as_str(raw_storage.get("default_backend"), "linux"),
    )

    beh = _section(data.get("behaviour"))
    behaviour = BehaviourSettings(
        verbose=_as_bool(beh.get("verbose"), False),
        confirm_exits=_as_bool(beh.get("confirm_exits"), True),
        resume_transcript_turns=max(0, _as_int(beh.get("resume_transcript_turns"), 20)),
    )

    plugin_data = _section(data.get("plugins"))
    plugins = PluginSettings(
        auto_trust=_as_bool(plugin_data.get("auto_trust"), False),
        auto_install=_as_bool(plugin_data.get("auto_install"), False),
        install_target=_as_str(plugin_data.get("install_target"), "venv"),
        allowed_modules=_as_string_list(plugin_data.get("allowed_modules")),
        timeout_seconds=_as_float(plugin_data.get("timeout_seconds"), 30.0),
        disabled=_as_string_list(plugin_data.get("disabled")),
        trust_file=_as_str(plugin_data.get("trust_file"), ".agenthicc/trusted_plugins.json"),
        audit_file=_as_str(plugin_data.get("audit_file"), ".agenthicc/plugin_audit.jsonl"),
        strict_cli_shadow=_as_bool(plugin_data.get("strict_cli_shadow"), False),
    )

    skill_data = _section(data.get("skills"))
    skills = SkillsSettings(
        install_default_skills=bool(skill_data.get("install_default_skills", True)),
        default_skill_directory=str(skill_data.get("default_skill_directory", "")),
    )

    agents = AgentsSettings.from_dict(_section(data.get("agents")))

    # [workflows] section — dict[workflow_name, dict[str, Any]] (PRD-111)
    workflows: dict[str, dict[str, object]] = {
        str(name): _section(params)
        for name, params in _section(data.get("workflows")).items()
        if isinstance(params, Mapping)
    }

    raw_providers = data.get("providers")
    if raw_providers is None:
        providers_raw: dict[str, object] = {}
    elif isinstance(raw_providers, Mapping):
        providers_raw = _section(raw_providers)
    else:
        raise ValueError("providers must be a TOML table")
    providers: dict[str, ProviderProfile] = {}
    for name, params in providers_raw.items():
        if not isinstance(params, Mapping):
            raise ValueError(f"providers.{name} must be a TOML table")
        providers[str(name)] = ProviderProfile.from_mapping(str(name), params)

    return AgenthiccConfig(
        execution=execution,
        providers=providers,
        behaviour=behaviour,
        hooks=hooks,
        tools=tools,
        memory=memory,
        security=security,
        api=api,
        plugins=plugins,
        storage=storage_settings,
        skills=skills,
        agents=agents,
        workflows=workflows,
    )


# Keep _build_config as an alias for backward compatibility
_build_config = _dict_to_config


def load_config(
    project_path: str | Path | None = None,
    user_path: str | Path | None = None,
    env_overrides: bool = True,
    cli_overrides: list[str] | None = None,
    config_path: str | Path | None = None,
    cli_secret_overrides: list[str] | None = None,
) -> AgenthiccConfig:
    """Load and merge configuration into a typed :class:`AgenthiccConfig`.

    Precedence (lowest → highest):

    1. Hardcoded defaults (lowest)
    2. User-global: ``~/.agenthicc/agenthicc.toml``   — identity, credentials,
                    preferred model, personal plugins/modes
    3. Per-project: ``.agenthicc/agenthicc.toml``      — project-specific overrides
    4a. Provider shorthand env vars (``OPENAI_MODEL``, ``ANTHROPIC_API_KEY``, …)
    4b. ``AGENTHICC_<SECTION>_<KEY>`` env vars         — always override config files;
                                                          win over 4a shorthands
    5. CLI ``--set section.key=value`` and
       ``--set-secret section.key=ENV_VAR`` overrides (highest)

    Environment variables always override both config files (user-global and
    per-project).  This lets CI/CD and shell profiles reliably control
    credentials and model selection without touching checked-in config.

    When ``project_path`` or ``user_path`` are given explicitly, the file must
    exist and be valid TOML (raises :class:`tomllib.TOMLDecodeError` on invalid
    syntax).  When paths are auto-discovered, bad files produce a warning and
    are skipped.
    """
    merged: dict[str, object] = {}

    # PRD-113: --config / AGENTHICC_CONFIG override the auto-discovered project file.
    # Priority: explicit config_path arg > AGENTHICC_CONFIG env var > auto-discovery.
    if config_path is None:
        import os as _os  # noqa: PLC0415

        _env_cfg = _os.environ.get("AGENTHICC_CONFIG", "").strip()
        if _env_cfg:
            config_path = _env_cfg

    # 2. User-global config (~/.agenthicc/agenthicc.toml) — shared defaults.
    # extends chains in the user-global file are also resolved.
    if user_path is not None:
        user_file = Path(user_path)
        if user_file.is_file():
            merged = deep_merge(merged, _resolve_extends(user_file))
    else:
        found_user_file = _find_config_file(USER_CONFIG_CANDIDATES)
        if found_user_file is not None:
            merged = deep_merge(merged, _load_toml_with_extends(found_user_file))

    # 3. Per-project config — overrides user-global.
    # config_path (from --config or AGENTHICC_CONFIG) takes priority over project_path.
    effective_project = config_path or project_path
    if effective_project is not None:
        project_file = Path(effective_project)
        if project_file.is_file():
            merged = deep_merge(merged, _resolve_extends(project_file))
    else:
        found_project_file = _find_config_file(PROJECT_CONFIG_CANDIDATES)
        if found_project_file is not None:
            merged = deep_merge(merged, _load_toml_with_extends(found_project_file))

    # 4. Environment variable overrides (AGENTHICC_*) — override both config files
    if env_overrides:
        merged = _apply_env_overrides(merged)

    # 5. CLI overrides (highest of all). Secret overrides are applied last so
    # they cannot be accidentally replaced by a plaintext --set value.
    if cli_overrides:
        merged = _apply_cli_overrides(merged, cli_overrides)
    if cli_secret_overrides:
        merged = _apply_cli_secret_overrides(merged, cli_secret_overrides)

    return _dict_to_config(merged)


# ── LLM transport builder ─────────────────────────────────────────────────


def build_llm_config(execution: ExecutionSettings) -> LLMConfig:
    """Build a :class:`~lauren_ai._config.LLMConfig` from agenthicc execution settings.

    Supports all providers that lauren-ai knows about:
    ``anthropic``, ``openai``, ``ollama``, ``litellm``.

    :param execution: The resolved execution settings (provider, model, api_key, base_url).
    :raises ValueError: When the provider string is not recognised.
    :returns: A ``LLMConfig`` instance ready to pass to ``_build_transport()``.
    """
    import dataclasses  # noqa: PLC0415
    import os  # noqa: PLC0415
    from lauren_ai._config import LLMConfig  # noqa: PLC0415

    llm_fields = {field.name for field in dataclasses.fields(LLMConfig)}
    required_fields = {
        "default_headers",
        "default_query",
        "client_options",
        "request_options",
        "top_p",
        "max_completion_tokens",
    }
    missing_fields = sorted(required_fields - llm_fields)
    if missing_fields:
        raise ValueError(
            "Provider profiles require lauren-ai >= 1.4.0; the installed version "
            f"is missing: {', '.join(missing_fields)}"
        )

    def _cache(cfg: "LLMConfig") -> "LLMConfig":
        # PRD-132 L0: enable incremental prompt caching (system + tools +
        # conversation prefix).  Read only by the Anthropic transport — a clean
        # no-op for OpenAI/Ollama/litellm.
        fields = getattr(cfg, "__dataclass_fields__", {})
        replace_kwargs: dict[str, object] = {
            "cache_system_prompt": execution.prompt_cache,
            "cache_tools": execution.prompt_cache,
        }
        if "cache_conversation" in fields:
            replace_kwargs["cache_conversation"] = execution.prompt_cache
        replace_config = cast(Callable[..., LLMConfig], dataclasses.replace)
        result = replace_config(cfg, **replace_kwargs)
        # Keep this compatibility branch for downstream installations that
        # import agenthicc with an older lauren-ai wheel despite the declared
        # dependency constraint.
        if "cache_conversation" not in fields:
            object.__setattr__(result, "cache_conversation", execution.prompt_cache)
        return result

    provider = execution.provider.lower()
    model = execution.effective_model()
    api_key = execution.effective_api_key()
    # base_url: explicit config wins; then OPENAI_BASE_URL / OLLAMA_HOST env vars
    base_url = (
        execution.base_url
        or (os.environ.get("OPENAI_BASE_URL") if provider == "openai" else None)
        or (os.environ.get("OLLAMA_HOST") if provider == "ollama" else None)
        or None
    )
    if base_url:
        base_url = _validate_base_url(base_url, path="execution.base_url")

    # SDK-level retries only — turn-level retry (transport_max_retries) is the
    # memory-safe primary mechanism and is applied by the runners (PRD-126).
    max_retries = execution.llm_sdk_max_retries
    resolved = execution._resolved_profile
    default_headers = (
        dict(resolved.default_headers)
        if resolved
        else _resolve_headers(
            execution.default_headers,
            environ=os.environ,
            path="execution.default_headers",
        )
    )
    default_query = dict(resolved.default_query) if resolved else dict(execution.default_query)
    client_options = dict(resolved.client_options) if resolved else dict(execution.client_options)
    request_options = resolved.request_options if resolved else execution.request_options
    if isinstance(request_options, RequestOptionSettings):
        request_options = request_options.resolve()
    timeout = (
        resolved.timeout_s if resolved and resolved.timeout_s is not None else execution.timeout_s
    )
    temperature = (
        resolved.temperature
        if resolved and resolved.temperature is not None
        else execution.temperature
    )
    top_p = resolved.top_p if resolved and resolved.top_p is not None else execution.top_p
    max_completion_tokens = (
        resolved.max_completion_tokens
        if resolved and resolved.max_completion_tokens is not None
        else execution.max_completion_tokens
    )
    common: dict[str, object] = {
        "max_tokens": execution.max_output_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "max_completion_tokens": max_completion_tokens,
        "timeout": timeout,
        "max_retries": max_retries,
        "default_headers": default_headers,
        "default_query": default_query,
        "client_options": client_options,
        "request_options": request_options,
    }
    # Optional fields are omitted rather than sent as None to older lauren
    # compatibility shims and to avoid changing their default behavior.
    common = {key: value for key, value in common.items() if value is not None}

    if provider == "anthropic":
        if base_url:
            common["base_url"] = base_url
        for_anthropic = cast(Callable[..., LLMConfig], LLMConfig.for_anthropic)
        return _cache(for_anthropic(model=model, api_key=api_key, **common))

    if provider == "openai":
        # ``for_openai`` accepts any OpenAI-compatible endpoint, including
        # Modal deployments, vLLM, Together, Groq, and private gateways.
        if base_url:
            common["base_url"] = base_url
        for_openai = cast(Callable[..., LLMConfig], LLMConfig.for_openai)
        return _cache(for_openai(model=model, api_key=api_key, **common))

    if provider == "ollama":
        if base_url:
            common["base_url"] = base_url
        for_ollama = cast(Callable[..., LLMConfig], LLMConfig.for_ollama)
        return _cache(for_ollama(model=model, **common))

    if provider == "litellm":
        llm_constructor = cast(Callable[..., LLMConfig], LLMConfig)
        return _cache(
            llm_constructor(
                provider="litellm",
                model=model,
                api_key=api_key,
                base_url=base_url,
                **common,
            )
        )

    supported = ", ".join(f"'{p}'" for p in SUPPORTED_PROVIDERS)
    raise ValueError(
        f"Unknown LLM provider: {provider!r}. Supported: {supported}. "
        f'Set in config: [execution] provider = "openai"'
    )
