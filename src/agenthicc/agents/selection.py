"""Which primary agent a session talks to (AGX-09 §5.3).

A new session starts with ``--agent NAME``, else the project's default (set
with ``*`` in the Library, kept per project in
``~/.agenthicc/state/agent-defaults.json`` so nothing is written into the
repository), else ``default``. A resumed session keeps the agent it last used
(recorded in its metadata).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.agents.definitions import AgentSpec
    from agenthicc.agents.registry import AgentsRegistry
    from agenthicc.config import AgenthiccConfig, ExecutionSettings

__all__ = [
    "DEFAULT_AGENT",
    "AgentUnavailable",
    "TurnAgent",
    "agent_defaults_path",
    "initial_agent",
    "project_default",
    "resolve_agent_model",
    "set_project_default",
    "turn_agent",
]

#: The built-in agent a session uses when nothing else is chosen.
DEFAULT_AGENT = "default"


def agent_defaults_path() -> Path:
    """``~/.agenthicc/state/agent-defaults.json``; ``AGENTHICC_AGENT_DEFAULTS`` overrides it."""
    override = os.environ.get("AGENTHICC_AGENT_DEFAULTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agenthicc" / "state" / "agent-defaults.json"


def _read() -> dict[str, str]:
    try:
        raw = json.loads(agent_defaults_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)
    }


def _key(project_root: Path) -> str:
    return str(project_root.expanduser().resolve())


def project_default(project_root: Path) -> str | None:
    """The default primary agent chosen for *project_root*, if any."""
    return _read().get(_key(project_root))


def set_project_default(project_root: Path, name: str | None) -> None:
    """Choose (or, with ``None``, forget) the project's default primary agent."""
    values = _read()
    if name is None:
        values.pop(_key(project_root), None)
    else:
        values[_key(project_root)] = name
    path = agent_defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def initial_agent(
    registry: AgentsRegistry,
    *,
    requested: str | None,
    persisted: str | None,
    project_root: Path,
) -> str:
    """The primary agent a session starts with.

    An explicit *requested* agent that is not a primary agent is an error; a
    persisted or project default that no longer exists falls back to
    ``default`` (the definition may have been deleted since).
    """
    if requested:
        spec = registry.get(requested)
        if spec is None or not spec.primary:
            names = ", ".join(sorted(item.name for item in registry.primary()))
            raise ValueError(f"Unknown primary agent {requested!r}. Choose one of: {names}.")
        return spec.name
    for candidate in (persisted, project_default(project_root)):
        if candidate:
            spec = registry.get(candidate)
            if spec is not None and spec.primary:
                return spec.name
    return DEFAULT_AGENT


class AgentUnavailable(ValueError):
    """The session's agent cannot run a turn; the message says what to do."""


@dataclass(frozen=True, slots=True)
class TurnAgent:
    """What a direct turn needs from the session's primary agent."""

    spec: AgentSpec
    exec_cfg: ExecutionSettings
    max_agent_turns: int


def resolve_agent_model(spec: AgentSpec, cfg: AgenthiccConfig) -> str:
    """The model a turn of *spec* uses; empty means the session's model.

    ``model`` may name a provider profile, ``provider/model`` or a model of
    the session's provider. A definition cannot switch the session to another
    provider or endpoint: that fails before any provider call.
    """
    from agenthicc.config import SUPPORTED_PROVIDERS

    if not spec.model:
        return ""
    session_profile = cfg.providers.get(cfg.execution.profile) if cfg.execution.profile else None
    session_provider = session_profile.provider if session_profile else cfg.execution.provider
    profile = cfg.providers.get(spec.model)
    if profile is not None:
        if session_profile is None or (profile.provider, profile.base_url) != (
            session_profile.provider,
            session_profile.base_url,
        ):
            raise AgentUnavailable(
                f"Agent {spec.name!r} uses the provider profile {spec.model!r}, which is not "
                "this session's endpoint. Start agenthicc with that profile or change the "
                "agent's model."
            )
        return profile.model or cfg.execution.model
    provider, _, model = spec.model.partition("/")
    if model and provider in SUPPORTED_PROVIDERS:
        if provider != session_provider:
            raise AgentUnavailable(
                f"Agent {spec.name!r} uses {spec.model}, but this session talks to "
                f"{session_provider}. Start agenthicc with that provider or change the "
                "agent's model."
            )
        return model
    return spec.model


def turn_agent(registry: AgentsRegistry, name: str, cfg: AgenthiccConfig) -> TurnAgent:
    """The session's primary agent *name*, ready for a direct turn.

    An unknown or non-primary agent, or a model it cannot use, is an
    :class:`AgentUnavailable` — never a silent fall back to another agent.
    """
    spec = registry.get(name)
    if spec is None or not spec.primary:
        available = ", ".join(sorted(item.name for item in registry.primary()))
        raise AgentUnavailable(
            f"Agent {name!r} is not a primary agent here. Choose one with /agent ({available})."
        )
    exec_cfg = cfg.execution
    model = resolve_agent_model(spec, cfg)
    if model:
        exec_cfg = replace(exec_cfg, model=model)
    limit = exec_cfg.max_agent_turns
    if spec.max_steps is not None:
        limit = min(limit, spec.max_steps)
    return TurnAgent(spec=spec, exec_cfg=exec_cfg, max_agent_turns=limit)
