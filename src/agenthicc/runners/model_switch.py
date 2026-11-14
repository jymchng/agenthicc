"""Choosing the provider and model during a session, without a restart (TUX-05 §2.5).

:func:`model_choices` lists what ``/model`` offers: each supported provider's
default model (and the current model), then the saved ``[providers.*]``
profiles. Each choice says whether its credentials resolve and, when they do
not, which environment variable to set.

:func:`execution_for` derives the :class:`ExecutionSettings` a choice runs
with; the session forks its runner from them through the same lazy factory it
used at start-up (``_LazyAgentRunner.for_execution``), so the change applies
from the next turn. Nothing here contacts a provider, and no choice carries
a credential value — only the *name* of a missing variable.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig, ExecutionSettings

__all__ = ["ModelChoice", "execution_for", "model_choice", "model_choices", "short_model"]

#: Providers that need no API key.
_KEYLESS = frozenset({"ollama"})


@dataclass(frozen=True)
class ModelChoice:
    """One row of the ``/model`` picker."""

    provider: str
    model: str
    #: A saved ``[providers.NAME]`` profile, or ``""`` for a provider default.
    profile: str = ""
    context_window: int = 0
    #: The variable to set when the credentials do not resolve; ``""`` when they do.
    missing_env: str = ""
    current: bool = False

    @property
    def available(self) -> bool:
        return not self.missing_env

    @property
    def label(self) -> str:
        """``provider/model``, as the status line shows the active model."""
        return f"{self.provider}/{self.model}"


def short_model(model: str) -> str:
    """The model id without a provider prefix (``anthropic/claude-x`` → ``claude-x``)."""
    return model.rsplit("/", 1)[-1]


def _context_window(cfg: AgenthiccConfig, provider: str, model: str) -> int:
    from agenthicc.config import ExecutionSettings

    probe = ExecutionSettings(
        provider=provider, model=model, context_windows=cfg.execution.context_windows
    )
    try:
        return probe.effective_context_window()
    except (ValueError, TypeError):
        return 0


def _missing_env(
    cfg: AgenthiccConfig, provider: str, profile: str, environ: Mapping[str, str]
) -> str:
    """The variable to set for *provider* (or *profile*), or ``""`` when credentials resolve."""
    from agenthicc.config import PROVIDER_API_KEY_ENVVAR, SecretReference

    if provider in _KEYLESS:
        return ""
    if profile:
        spec = cfg.providers.get(profile)
        if spec is not None:
            if isinstance(spec.api_key, str) and spec.api_key:
                return ""
            if isinstance(spec.api_key, SecretReference):
                return "" if environ.get(spec.api_key.env) else spec.api_key.env
            if spec.api_key_env:
                return "" if environ.get(spec.api_key_env) else spec.api_key_env
    execution = cfg.execution
    if execution.provider == provider and not profile:
        if isinstance(execution.api_key, str) and execution.api_key:
            return ""
        if isinstance(execution.api_key, SecretReference):
            return "" if environ.get(execution.api_key.env) else execution.api_key.env
        if execution.api_key_env:
            return "" if environ.get(execution.api_key_env) else execution.api_key_env
    variable = PROVIDER_API_KEY_ENVVAR.get(provider, "")
    if not variable or environ.get(variable):
        return ""
    return variable


def model_choice(
    cfg: AgenthiccConfig,
    provider: str,
    model: str = "",
    *,
    profile: str = "",
    environ: Mapping[str, str] | None = None,
) -> ModelChoice:
    """The choice for *provider*/*model* (empty → the provider's default model)."""
    from agenthicc.config import PROVIDER_DEFAULT_MODELS

    env = os.environ if environ is None else environ
    resolved_model = model or PROVIDER_DEFAULT_MODELS.get(provider, "")
    execution = cfg.execution
    current = (
        execution.profile == profile
        and execution.provider == provider
        and execution.effective_model() == resolved_model
    )
    return ModelChoice(
        provider=provider,
        model=resolved_model,
        profile=profile,
        context_window=_context_window(cfg, provider, resolved_model),
        missing_env=_missing_env(cfg, provider, profile, env),
        current=current,
    )


def model_choices(
    cfg: AgenthiccConfig, environ: Mapping[str, str] | None = None
) -> list[ModelChoice]:
    """Every choice ``/model`` offers: providers first, then saved profiles."""
    from agenthicc.config import PROVIDER_DEFAULT_MODELS, SUPPORTED_PROVIDERS

    execution = cfg.execution
    choices: list[ModelChoice] = []
    for provider in SUPPORTED_PROVIDERS:
        models = [PROVIDER_DEFAULT_MODELS.get(provider, "")]
        if execution.provider == provider and not execution.profile:
            current = execution.effective_model()
            if current not in models:
                models.insert(0, current)
        choices.extend(model_choice(cfg, provider, model, environ=environ) for model in models)
    for name, profile in sorted(cfg.providers.items()):
        model = profile.model or PROVIDER_DEFAULT_MODELS.get(profile.provider, "")
        choices.append(model_choice(cfg, profile.provider, model, profile=name, environ=environ))
    return choices


def execution_for(cfg: AgenthiccConfig, choice: ModelChoice) -> ExecutionSettings:
    """The execution settings a session runs *choice* with.

    A provider default keeps every non-provider setting (timeouts, retries,
    context windows) and drops the previous provider's endpoint and explicit
    key when the provider changes. A profile is resolved exactly as at
    start-up (``AgenthiccConfig.resolve_provider_profile``); this raises
    :class:`ValueError` when it cannot be.
    """
    current = cfg.execution
    if choice.profile:
        candidate = copy.copy(cfg)
        candidate.execution = replace(current, profile=choice.profile, _resolved_profile=None)
        candidate.resolve_provider_profile()
        return candidate.execution
    same_provider = current.provider == choice.provider and not current.profile
    return replace(
        current,
        profile="",
        provider=choice.provider,
        model=choice.model,
        _resolved_profile=None,
        api_key=current.api_key if same_provider else "",
        api_key_env=current.api_key_env if same_provider else "",
        base_url=current.base_url if same_provider else "",
        default_headers=dict(current.default_headers) if same_provider else {},
    )
