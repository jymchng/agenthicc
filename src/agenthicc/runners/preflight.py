"""The pre-flight check: can a turn succeed? (TUX-03)

Before the first prompt, :func:`check_session` looks at what a turn needs and
cannot provide on its own: a credential for the active provider, a model, a
workspace the agent may write to, and the tools agenthicc shells out to. It
answers from the configuration and the environment only: no request is sent,
no process is started, and a key is never read into a message.

The check is a pure function of its inputs (the working directory and ``PATH``
are inputs too), so ``agenthicc config show``, ``/status`` and the TUI's
start-up header call the same code and cannot disagree. Only a missing
credential is *blocking*: the TUI will not send a prompt the provider cannot
accept (see ``TUISession``); the other findings are warnings.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agenthicc.config import AgenthiccConfig

__all__ = [
    "PreflightCheck",
    "PreflightIssue",
    "check_credentials",
    "check_session",
    "credential_variable",
]

#: Credentials a provider's SDK accepts besides its API key.
_TOKEN_ENVVARS: Mapping[str, tuple[str, ...]] = {"anthropic": ("ANTHROPIC_AUTH_TOKEN",)}
#: Present in a project that runs commands in a sandboxed worktree environment.
_ENVIRONMENT_MANIFEST = Path(".agenthicc") / "environment.toml"


class PreflightCheck(StrEnum):
    """What a finding is about."""

    CREDENTIALS = "credentials"
    MODEL = "model"
    WORKSPACE = "workspace"
    TOOLS = "tools"


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    """One thing that would make a turn fail or a feature degrade."""

    check: PreflightCheck
    #: What is wrong, as a sentence.
    message: str
    #: The one thing to do about it.
    fix: str
    #: A turn cannot succeed until this is fixed.
    blocking: bool = False
    severity: Literal["warning", "info"] = "warning"

    @property
    def text(self) -> str:
        """The notice's title: what is wrong, then what to do."""
        return f"{self.message} {self.fix}"


def check_session(
    config: AgenthiccConfig,
    env: Mapping[str, str],
    *,
    cwd: Path | None = None,
    mode: str = "",
) -> tuple[PreflightIssue, ...]:
    """Every finding for a session in *cwd* under *mode*, most urgent first.

    *env* supplies the credentials and ``PATH``; *mode* is the active mode's
    name (a read-only workspace is fine in ``Plan``). An empty tuple means
    nothing needs saying.
    """
    directory = cwd if cwd is not None else Path.cwd()
    found = (
        check_credentials(config, env),
        _check_model(config),
        _check_workspace(directory, mode),
        *_check_tools(directory, env),
    )
    return tuple(issue for issue in found if issue is not None)


def check_credentials(config: AgenthiccConfig, env: Mapping[str, str]) -> PreflightIssue | None:
    """A finding when the active provider needs a key or token that *env* lacks.

    See :func:`credential_variable` for where a key may come from. A provider
    with no such variable, such as Ollama, needs none.
    """
    execution = config.execution
    profile_name = execution.profile.strip()
    profile = config.providers.get(profile_name) if profile_name else None
    if profile_name and profile is None:
        return PreflightIssue(
            PreflightCheck.CREDENTIALS,
            f"No provider profile named {profile_name}.",
            "Fix [execution] profile, or run /model to pick another provider.",
            blocking=True,
        )
    provider = profile.provider if profile is not None else execution.provider
    variable = credential_variable(config, provider, profile_name, env)
    if not variable:
        return None
    suffix = f" (profile {profile_name})" if profile_name else ""
    return PreflightIssue(
        PreflightCheck.CREDENTIALS,
        f"No API key for {provider}{suffix}.",
        f"Set {variable}, or run /model to pick another provider.",
        blocking=True,
    )


def credential_variable(
    config: AgenthiccConfig, provider: str, profile_name: str, env: Mapping[str, str]
) -> str:
    """The variable to set for *provider* (under saved profile *profile_name*), or ``""``.

    ``""`` means the credential resolves in *env* (or is a literal key in the
    configuration), or the provider needs none. The sources are the ones
    :meth:`ProviderProfile.resolve` and :meth:`ExecutionSettings.effective_api_key`
    apply: the profile's own ``api_key`` (a literal or a ``SecretReference``) or
    ``api_key_env``; then the ``[execution]`` ones, for the active provider or
    for a profile that has none; then the provider's own variable
    (``PROVIDER_API_KEY_ENVVAR``, and ``ANTHROPIC_AUTH_TOKEN`` for anthropic).

    The ``/model`` picker (``runners.model_switch``) asks the same question of
    each choice, so a provider is offered as available exactly when a turn on it
    would pass this check.
    """
    from agenthicc.config import PROVIDER_API_KEY_ENVVAR, SecretReference

    default_variable = PROVIDER_API_KEY_ENVVAR.get(provider)
    if default_variable is None:
        return ""
    execution = config.execution
    profile = config.providers.get(profile_name) if profile_name else None
    sources: list[tuple[str | SecretReference | None, str]] = []
    if profile is not None:
        sources.append((profile.api_key, profile.api_key_env))
    keyless_profile = profile is not None and profile.api_key is None and not profile.api_key_env
    if keyless_profile or (profile is None and provider == execution.provider):
        sources.append((execution.api_key or None, execution.api_key_env))
    for key, key_env in sources:
        if isinstance(key, SecretReference):
            return "" if env.get(key.env) else key.env
        if key:
            return ""  # a literal key in the configuration
        if key_env:
            return "" if env.get(key_env) else key_env
    if env.get(default_variable) or any(
        env.get(token) for token in _TOKEN_ENVVARS.get(provider, ())
    ):
        return ""
    return default_variable


def _check_model(config: AgenthiccConfig) -> PreflightIssue | None:
    from agenthicc.config import PROVIDER_DEFAULT_MODELS

    execution = config.execution
    profile = config.providers.get(execution.profile.strip()) if execution.profile.strip() else None
    provider = profile.provider if profile is not None else execution.provider
    model = (
        (profile.model if profile is not None else "")
        or execution.model
        or PROVIDER_DEFAULT_MODELS.get(provider, "")
    )
    if model:
        return None
    return PreflightIssue(
        PreflightCheck.MODEL,
        f"No model is configured for {provider}.",
        "Set [execution] model, or run /model to pick one.",
    )


def _check_workspace(cwd: Path, mode: str) -> PreflightIssue | None:
    if mode.casefold() == "plan" or os.access(cwd, os.W_OK | os.X_OK):
        return None
    return PreflightIssue(
        PreflightCheck.WORKSPACE,
        f"{cwd} is not writable.",
        "Use /mode plan, or start agenthicc in a writable directory.",
    )


def _check_tools(cwd: Path, env: Mapping[str, str]) -> tuple[PreflightIssue | None, ...]:
    path = env.get("PATH", "")
    bubblewrap: PreflightIssue | None = None
    if (
        sys.platform.startswith("linux")
        and (cwd / _ENVIRONMENT_MANIFEST).is_file()
        and shutil.which("bwrap", path=path) is None
    ):
        bubblewrap = PreflightIssue(
            PreflightCheck.TOOLS,
            "Bubblewrap (bwrap) is not installed, so this project's environment commands cannot run.",
            "Install the bubblewrap package.",
        )
    ripgrep: PreflightIssue | None = None
    if shutil.which("rg", path=path) is None:
        ripgrep = PreflightIssue(
            PreflightCheck.TOOLS,
            "ripgrep (rg) is not installed, so file search uses the slower built-in scanner.",
            "Install ripgrep.",
            severity="info",
        )
    return bubblewrap, ripgrep
