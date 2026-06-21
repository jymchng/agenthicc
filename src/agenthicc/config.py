"""TOML configuration loading and merging for Agenthicc (PRD-07, PRD-21).

Merge order (later sources override earlier ones):

1. Hardcoded defaults (lowest priority)
2. ~/.agenthicc/agenthicc.toml  — user-global defaults (identity, credentials,
                                   preferred model, personal tool/mode plugins)
3. .agenthicc/agenthicc.toml    — per-project overrides (project model, paths,
                                   project-specific tools/modes/commands)
4. Environment variables AGENTHICC_* prefix  (CI / dev convenience)
5. CLI --set section.key=value overrides (highest priority)

Project config always wins over user-global config.  User-global config supplies
shared defaults that any project can override.  This mirrors the Git
~/.gitconfig / .git/config layering model.

Scalars are overwritten, lists are replaced, tables are merged recursively.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agenthicc.kernel import SecurityPolicy, SystemSettings

if TYPE_CHECKING:
    from lauren_ai._config import LLMConfig
    from agenthicc.tools.mcp import McpServerConfig

__all__ = [
    "AgenthiccConfig",
    "AgentSettings",
    "AgentsSettings",
    "ApiSettings",
    "BehaviourSettings",
    "ExecutionSettings",
    "MemorySettings",
    "PluginSettings",
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
    "openai":    "OPENAI_API_KEY",
    "litellm":   "ANTHROPIC_API_KEY",   # litellm can delegate to any backend
}

# Provider-specific shorthand env vars (read in addition to AGENTHICC_* vars).
# Setting e.g. OPENAI_MODEL automatically sets execution.model and infers provider=openai.
# Setting OPENAI_BASE_URL enables any OpenAI-compatible endpoint (poolside, Together, etc.)
PROVIDER_ENV_SHORTCUTS: dict[str, tuple[str, str]] = {
    # OpenAI and OpenAI-compatible endpoints
    "OPENAI_MODEL":    ("execution", "model"),
    "OPENAI_BASE_URL": ("execution", "base_url"),
    # Anthropic
    "ANTHROPIC_MODEL": ("execution", "model"),
    # Ollama
    "OLLAMA_MODEL":    ("execution", "model"),
    "OLLAMA_HOST":     ("execution", "base_url"),    # e.g. http://remote:11434
    # LiteLLM
    "LITELLM_MODEL":   ("execution", "model"),
}


@dataclass
class ExecutionSettings:
    max_concurrent_intents: int = 8
    max_parallel_tasks: int = 4
    agent_pool_size: int = 16
    max_agent_turns: int = 200      # max agentic-loop iterations per intent
    turn_timeout_s: float = 0.0    # per-turn watchdog; 0 = no limit
    # Conversation compaction (PRD-119)
    auto_compact: bool = True
    compact_threshold_tokens: int = 1_000_000
    # LLM provider selection
    provider: str = "anthropic"
    model: str = ""            # empty → use PROVIDER_DEFAULT_MODELS[provider]
    api_key: str = ""          # empty → read from PROVIDER_API_KEY_ENVVAR
    base_url: str = ""         # Ollama / self-hosted endpoint override

    def effective_model(self) -> str:
        return self.model or PROVIDER_DEFAULT_MODELS.get(self.provider, self.model)

    def effective_api_key(self) -> str | None:
        import os  # noqa: PLC0415
        if self.api_key:
            return self.api_key
        env_var = PROVIDER_API_KEY_ENVVAR.get(self.provider)
        return os.environ.get(env_var, "") or None if env_var else None


@dataclass
class ToolSettings:
    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    max_live_tool_calls: int = 5
    http_timeout_s: float = 30.0
    """Read timeout in seconds for all outbound HTTP tool calls (PRD-108).
    Set via ``[tools] http_timeout_s = N`` in agenthicc.toml.
    Use ``0.0`` to disable the read timeout (unbounded)."""
    """Maximum tool completions rendered individually in the scroll buffer
    before collapsing the rest into a live "…and N more tool calls" indicator.
    Set via [tools] max_live_tool_calls = N in agenthicc.toml."""


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


@dataclass
class AgentSettings:
    """Per-agent TOML metadata (supplementary to filesystem discovery)."""
    description: str = ""
    model: str = ""
    max_turns: int = 200


@dataclass
class AgentsSettings:
    """[agents] section — keyed by agent slug."""
    agents: dict[str, AgentSettings] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, dict[str, object]]) -> "AgentsSettings":
        fields = {f for f in AgentSettings.__dataclass_fields__}
        return cls(agents={
            name: AgentSettings(**{k: v for k, v in cfg.items() if k in fields})
            for name, cfg in d.items()
        })


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
    execution: ExecutionSettings  = field(default_factory=ExecutionSettings)
    behaviour: BehaviourSettings  = field(default_factory=BehaviourSettings)
    hooks:     dict[str, list[str]] = field(default_factory=dict)
    tools:     ToolSettings       = field(default_factory=ToolSettings)
    memory:    MemorySettings     = field(default_factory=MemorySettings)
    security:  SecuritySettings   = field(default_factory=SecuritySettings)
    api:       ApiSettings        = field(default_factory=ApiSettings)
    plugins:   PluginSettings     = field(default_factory=PluginSettings)
    skills:    SkillsSettings     = field(default_factory=SkillsSettings)
    agents:    AgentsSettings      = field(default_factory=AgentsSettings)
    storage:   StorageSettings    = field(default_factory=StorageSettings)
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


def _parse_mcp_servers(raw_list: list[dict[str, object]]) -> list[McpServerConfig]:
    """Convert raw TOML dicts to McpServerConfig objects (graceful if mcp.py unavailable)."""
    try:
        from agenthicc.tools.mcp import McpServerConfig  # noqa: PLC0415
        return [McpServerConfig.from_dict(d) for d in raw_list]
    except ImportError:
        return list(raw_list)  # type: ignore[return-value]  # fall back to raw dicts


# ── merging ──────────────────────────────────────────────────────────────


def deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
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
        remainder = key[len("AGENTHICC_"):].lower()
        parts = remainder.split("_", 1)
        if len(parts) != 2:
            continue
        section, field_name = parts
        config.setdefault(section, {})[field_name] = _coerce_env(value)
        agenthicc_set.add((section, field_name))

    # 2. Provider-specific shorthand env vars (OPENAI_MODEL, OPENAI_BASE_URL, etc.)
    #    These override per-project config — env vars win over config files.
    #    They yield only to an explicit AGENTHICC_* var (already applied above).
    explicit_provider = config.get("execution", {}).get("provider")
    inferred_provider: str | None = None

    for env_var, (section, field_name) in PROVIDER_ENV_SHORTCUTS.items():
        value = os.environ.get(env_var)
        if not value:
            continue
        # Skip only if an AGENTHICC_* var already set this exact field.
        if (section, field_name) in agenthicc_set:
            continue
        config.setdefault(section, {})[field_name] = value
        # Infer provider from which shorthand var was set (e.g. OPENAI_MODEL → openai)
        if inferred_provider is None:
            prefix = env_var.split("_")[0].lower()   # "OPENAI_MODEL" → "openai"
            if prefix in SUPPORTED_PROVIDERS:
                inferred_provider = prefix

    # 3. Auto-infer provider from API key env vars if still unset
    if inferred_provider is None and not explicit_provider:
        for provider, api_key_var in PROVIDER_API_KEY_ENVVAR.items():
            if os.environ.get(api_key_var):
                inferred_provider = provider
                break   # first match wins (ANTHROPIC_API_KEY checked first)

    # Apply inferred provider only when no explicit provider was set
    if inferred_provider and not explicit_provider:
        config.setdefault("execution", {}).setdefault("provider", inferred_provider)

    return config


def _apply_cli_overrides(config: dict[str, object], overrides: list[str]) -> dict[str, object]:
    """Apply --set section.key=value overrides (highest priority)."""
    for override in overrides:
        if "=" not in override:
            continue
        key_path, _, value_str = override.partition("=")
        parts = key_path.strip().split(".", 1)
        if len(parts) != 2:
            continue
        section, field_name = parts
        config.setdefault(section, {})[field_name] = _coerce_env(value_str)
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
    ex = data.get("execution", {})
    execution = ExecutionSettings(
        max_concurrent_intents=ex.get("max_concurrent_intents", 8),
        max_parallel_tasks=ex.get("max_parallel_tasks", 4),
        agent_pool_size=ex.get("agent_pool_size", 16),
        max_agent_turns=int(ex.get("max_agent_turns", 200)),
        auto_compact=bool(ex.get("auto_compact", True)),
        compact_threshold_tokens=int(ex.get("compact_threshold_tokens", 1_000_000)),
        provider=str(ex.get("provider", "anthropic")),
        model=str(ex.get("model", "")),
        api_key=str(ex.get("api_key", "")),
        base_url=str(ex.get("base_url", "")),
    )

    hooks = _flatten_hooks(data.get("hooks", {}))

    to = data.get("tools", {})
    tools_raw = to
    tools = ToolSettings(
        mcp_servers=_parse_mcp_servers(tools_raw.get("mcp_servers", [])),
        plugins=list(to.get("plugins", [])),
        allowed=list(to.get("allowed", to.get("allowed_tools", []))),
        denied=list(to.get("denied", to.get("denied_tools", []))),
        max_live_tool_calls=int(to.get("max_live_tool_calls", 5)),
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

    # Parse [storage] and [storage.s3] sections
    raw_storage = dict(data.get("storage", {}))
    raw_s3 = dict(raw_storage.pop("s3", {}))
    raw_mounts = raw_s3.pop("mounts", {})
    s3_settings = StorageS3Settings(
        bucket=raw_s3.get("bucket", ""),
        region=raw_s3.get("region", "us-east-1"),
        prefix=raw_s3.get("prefix", ""),
        access_key_id=raw_s3.get("access_key_id", ""),
        secret_access_key=raw_s3.get("secret_access_key", ""),
        endpoint_url=raw_s3.get("endpoint_url", ""),
        profile=raw_s3.get("profile", ""),
        path_style=raw_s3.get("path_style", False),
        mounts=raw_mounts,
    )
    storage_settings = StorageSettings(
        s3=s3_settings,
        default_backend=raw_storage.get("default_backend", "linux"),
    )

    beh = data.get("behaviour", {})
    behaviour = BehaviourSettings(
        verbose=bool(beh.get("verbose", False)),
        confirm_exits=bool(beh.get("confirm_exits", True)),
    )

    # [workflows] section — dict[workflow_name, dict[str, Any]] (PRD-111)
    workflows: dict[str, dict[str, object]] = {
        name: dict(params)
        for name, params in data.get("workflows", {}).items()
        if isinstance(params, dict)
    }

    return AgenthiccConfig(
        execution=execution,
        behaviour=behaviour,
        hooks=hooks,
        tools=tools,
        memory=memory,
        security=security,
        api=api,
        storage=storage_settings,
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
    5. CLI ``--set section.key=value`` overrides (highest)

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
        user_file: Path | None = Path(user_path)
        if user_file.is_file():
            merged = deep_merge(merged, _resolve_extends(user_file))
    else:
        user_file = _find_config_file(USER_CONFIG_CANDIDATES)
        if user_file is not None:
            merged = deep_merge(merged, _load_toml_with_extends(user_file))

    # 3. Per-project config — overrides user-global.
    # config_path (from --config or AGENTHICC_CONFIG) takes priority over project_path.
    effective_project = config_path or project_path
    if effective_project is not None:
        project_file: Path | None = Path(effective_project)
        if project_file.is_file():
            merged = deep_merge(merged, _resolve_extends(project_file))
    else:
        project_file = _find_config_file(PROJECT_CONFIG_CANDIDATES)
        if project_file is not None:
            merged = deep_merge(merged, _load_toml_with_extends(project_file))

    # 4. Environment variable overrides (AGENTHICC_*) — override both config files
    if env_overrides:
        merged = _apply_env_overrides(merged)

    # 5. CLI --set overrides (highest of all)
    if cli_overrides:
        merged = _apply_cli_overrides(merged, cli_overrides)

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
    import os  # noqa: PLC0415
    from lauren_ai._config import LLMConfig  # noqa: PLC0415

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

    if provider == "anthropic":
        kwargs: dict[str, str | None] = {"model": model, "api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return LLMConfig.for_anthropic(**kwargs)

    if provider == "openai":
        # LLMConfig.for_openai passes base_url to OpenAI client, enabling any
        # OpenAI-compatible endpoint (poolside, Together, Groq, local vLLM, etc.)
        kwargs = {"model": model, "api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return LLMConfig.for_openai(**kwargs)

    if provider == "ollama":
        kwargs: dict[str, str | None] = {"model": model}
        if base_url:
            kwargs["base_url"] = base_url
        return LLMConfig.for_ollama(**kwargs)

    if provider == "litellm":
        kwargs = {"provider": "litellm", "model": model, "api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return LLMConfig(**kwargs)

    supported = ", ".join(f"'{p}'" for p in SUPPORTED_PROVIDERS)
    raise ValueError(
        f"Unknown LLM provider: {provider!r}. Supported: {supported}. "
        f"Set in config: [execution] provider = \"openai\""
    )
