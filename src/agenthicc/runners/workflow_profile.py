"""Provider-profile selection for durable workflow recovery (PRD-251).

Workflow checkpoints retain only non-secret provider identity. This module
decides which current configuration should serve a resume before an agent
runner is constructed; it never contacts a provider.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agenthicc.config import AgenthiccConfig
    from agenthicc.config import ExecutionSettings

__all__ = [
    "ResumeProviderProfile",
    "WorkflowProfileResolutionError",
    "execution_profile_override",
    "provider_profile_fingerprint",
    "resolve_resume_provider_profile",
    "workflow_resume_agent_runner",
]


class WorkflowProfileResolutionError(ValueError):
    """Safe, stable profile-selection failure for a workflow resume."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message[:400]}")


@dataclass(frozen=True, slots=True)
class ResumeProviderProfile:
    """Resolved configuration choice for one resume attempt.

    ``config`` contains runtime-resolved credentials in its private execution
    settings. Do not serialize this value or include it in diagnostics.
    """

    config: AgenthiccConfig
    profile_name: str
    source: str
    binding_fingerprint: str
    saved_profile: str
    switched: bool


@runtime_checkable
class _ExecutionScopedRunner(Protocol):
    """A session runner able to create a runner for one workflow binding."""

    def for_execution(
        self,
        execution: ExecutionSettings,
        *,
        conversation_id: str,
    ) -> object: ...


def execution_profile_override(overrides: Sequence[str]) -> str | None:
    """Return the final invocation-level ``execution.profile`` override.

    ``None`` means the invocation did not explicitly choose a profile. The
    empty string is retained as an explicit choice of legacy execution fields.
    """

    selected: str | None = None
    for raw in overrides:
        key, separator, value = raw.partition("=")
        if separator and key.strip().casefold() == "execution.profile":
            selected = value.strip()
    return selected


def _endpoint_identity(value: str) -> str:
    """Normalize an endpoint without retaining userinfo, query, or fragment."""

    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
        if not parsed.scheme or not parsed.hostname:
            return "<invalid-endpoint>"
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname.casefold()
        try:
            port = parsed.port
        except ValueError:
            return "<invalid-endpoint>"
        default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
        host_part = f"[{hostname}]" if ":" in hostname else hostname
        netloc = host_part if port is None or default_port else f"{host_part}:{port}"
        path = parsed.path.rstrip("/")
        return urlunsplit((scheme, netloc, path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-endpoint>"


def provider_profile_fingerprint(config: AgenthiccConfig, profile_name: str) -> str:
    """Hash non-secret provider-routing identity for checkpoint comparison.

    The digest intentionally excludes all credential values, header/query
    values, client-option values, and resolved environment data. It covers the
    provider, effective model, endpoint authority/path, protocol, session
    header declaration, and the names of configured routing fields.
    """

    execution = config.execution
    profile = config.providers.get(profile_name) if profile_name else None
    if profile is None:
        provider = execution.provider
        model = execution.effective_model()
        base_url = execution.base_url
        protocol = ""
        session_header = execution.session_header
        header_names = sorted(execution.default_headers)
        query_names = sorted(execution.default_query)
    else:
        model = profile.model or execution.model or execution.effective_model()
        provider = profile.provider
        base_url = profile.base_url or execution.base_url
        protocol = profile.protocol
        session_header = profile.session_header or execution.session_header
        header_names = sorted(
            profile.default_headers if profile.default_headers else execution.default_headers
        )
        query_names = sorted(
            profile.default_query if profile.default_query else execution.default_query
        )
    identity = {
        "profile": profile_name,
        "provider": provider.casefold() if isinstance(provider, str) else "",
        "model": model,
        "endpoint": _endpoint_identity(base_url),
        "protocol": protocol,
        "session_header": session_header,
        "default_header_names": header_names,
        "default_query_names": query_names,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_resume_provider_profile(
    config: AgenthiccConfig,
    *,
    saved_profile: str,
    saved_binding_fingerprint: str = "",
    explicit_profile: str | None = None,
) -> ResumeProviderProfile:
    """Resolve and validate the provider profile for one workflow resume.

    A saved profile is inherited unless the caller supplied a resume-scoped
    override. Legacy checkpoints without a profile use current configuration.
    A changed non-secret binding for the inherited saved profile requires an
    explicit choice, preventing an unnoticed endpoint/model redirect.
    """

    from agenthicc.config import AgenthiccConfig

    saved = saved_profile.strip()
    if not isinstance(config, AgenthiccConfig):
        current = str(config.execution.profile or "").strip()
        if saved or explicit_profile is not None:
            raise WorkflowProfileResolutionError(
                "provider_profile_invalid",
                "This session cannot resolve named profiles; the saved workflow remains intact.",
            )
        return ResumeProviderProfile(
            config=config,
            profile_name=current,
            source="current config",
            binding_fingerprint="",
            saved_profile="",
            switched=False,
        )

    current = config.execution.profile.strip()
    explicit = explicit_profile is not None
    if explicit_profile is not None:
        selected = explicit_profile.strip()
        source = "explicit override"
    elif saved:
        selected = saved
        source = "saved"
    else:
        selected = current
        source = "current config"

    if selected and selected not in config.providers:
        code = (
            "provider_profile_override_invalid"
            if explicit
            else "provider_profile_unavailable"
            if saved
            else "provider_profile_invalid"
        )
        raise WorkflowProfileResolutionError(
            code,
            f"Provider profile {selected!r} is not configured. Restore it or explicitly resume "
            "with `/workflow resume [run-id] --profile <name|none>`; use `none` for the "
            "current legacy execution settings. Run `agenthicc config profiles` to list "
            "configured names. No provider request was sent and the saved workflow remains "
            "intact.",
        )
    # Clone only the mutable execution settings. Profile resolution stores its
    # late-bound secret snapshot there; it must not change normal session
    # configuration or credentials used by unrelated direct turns.
    selected_execution = replace(config.execution, profile=selected, _resolved_profile=None)
    selected_config = replace(config, execution=selected_execution)
    try:
        selected_config.resolve_provider_profile(strict=True, requires_tools=True)
    except Exception as exc:  # noqa: BLE001 - redact arbitrary resolver errors
        code = "provider_profile_override_invalid" if explicit else "provider_profile_invalid"
        raise WorkflowProfileResolutionError(
            code,
            f"Provider profile {selected or '<legacy execution settings>'!r} could not be "
            f"validated ({type(exc).__name__}); the saved workflow remains intact.",
        ) from None

    fingerprint = provider_profile_fingerprint(selected_config, selected)
    if (
        saved
        and saved_binding_fingerprint
        and not explicit
        and selected == saved
        and fingerprint != saved_binding_fingerprint
    ):
        raise WorkflowProfileResolutionError(
            "provider_profile_override_required",
            f"The saved profile {saved!r} now resolves to a different provider/model/endpoint "
            "binding. Explicitly select a profile for this resume to acknowledge the change; "
            "the checkpoint has not been modified.",
        )

    return ResumeProviderProfile(
        config=selected_config,
        profile_name=selected,
        source=source,
        binding_fingerprint=fingerprint,
        saved_profile=saved,
        switched=selected != saved and bool(saved),
    )


def workflow_resume_agent_runner(
    existing_runner: object | None,
    selection: ResumeProviderProfile,
    *,
    conversation_id: str,
) -> object:
    """Return a provider runner scoped to the selected workflow profile."""

    # The session may have had an invalid default provider and consequently no
    # lazy runner. Construct the standard lazy runtime only now, after the
    # selected checkpoint profile has passed strict config/secret validation.
    from functools import partial

    from agenthicc.config import build_llm_config
    from agenthicc.runners.tui_session import _LazyAgentRunner

    if isinstance(existing_runner, _ExecutionScopedRunner):
        return existing_runner.for_execution(
            selection.config.execution,
            conversation_id=conversation_id,
        )
    if (
        existing_runner is not None
        and not selection.switched
        and selection.source != "explicit override"
    ):
        # Preserve legacy/custom session runners when the selected checkpoint
        # profile is already the effective one. Agenthicc's built-in lazy
        # runner takes the fresh-resolution path above.
        return existing_runner

    runner = _LazyAgentRunner(
        partial(
            build_llm_config,
            selection.config.execution,
            conversation_id=conversation_id,
        ),
        cassette_dir=None,
        startup=None,
    )
    return runner
