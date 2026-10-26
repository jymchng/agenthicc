"""Provider-profile selection and secret-safety tests for PRD-251."""

from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from agenthicc.config import load_config
from agenthicc.runners.workflow_profile import (
    WorkflowProfileResolutionError,
    execution_profile_override,
    provider_profile_fingerprint,
    resolve_resume_provider_profile,
    workflow_resume_agent_runner,
)

pytestmark = pytest.mark.unit


def _config(tmp_path: Path, *, saved_endpoint: str = "https://saved.example/v1"):
    path = tmp_path / "agenthicc.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [execution]
            provider = "openai"
            model = "legacy-model"
            profile = "current"
            api_key_env = "LEGACY_KEY"

            [providers.saved]
            provider = "openai"
            model = "saved-model"
            base_url = "{saved_endpoint}"
            api_key_env = "SAVED_KEY"

            [providers.current]
            provider = "openai"
            model = "current-model"
            base_url = "https://current.example/v1"
            api_key_env = "CURRENT_KEY"
            """
        ),
        encoding="utf-8",
    )
    return load_config(
        project_path=path,
        user_path=tmp_path / "missing-user.toml",
        env_overrides=False,
    )


def _set_keys(monkeypatch: pytest.MonkeyPatch, *, saved: str = "saved-secret") -> None:
    monkeypatch.setenv("SAVED_KEY", saved)
    monkeypatch.setenv("CURRENT_KEY", "current-secret")
    monkeypatch.setenv("LEGACY_KEY", "legacy-secret")


def test_saved_profile_is_inherited_instead_of_current_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    config = _config(tmp_path)

    selected = resolve_resume_provider_profile(config, saved_profile="saved")

    assert selected.profile_name == "saved"
    assert selected.source == "saved"
    assert selected.config.execution.profile == "saved"
    assert selected.config.execution._resolved_profile is not None
    assert selected.config.execution._resolved_profile.api_key == "saved-secret"
    assert selected.binding_fingerprint == provider_profile_fingerprint(config, "saved")
    assert "saved-secret" not in repr(selected)


def test_explicit_profile_override_selects_currently_resolved_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    selected = resolve_resume_provider_profile(
        _config(tmp_path), saved_profile="saved", explicit_profile="current"
    )

    assert selected.profile_name == "current"
    assert selected.source == "explicit override"
    assert selected.switched


def test_explicit_none_selects_legacy_execution_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    selected = resolve_resume_provider_profile(
        _config(tmp_path), saved_profile="saved", explicit_profile=""
    )

    assert selected.profile_name == ""
    assert selected.source == "explicit override"
    assert selected.config.execution._resolved_profile is None


def test_invalid_override_and_missing_secret_are_safe_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    config = _config(tmp_path)
    with pytest.raises(WorkflowProfileResolutionError, match="provider_profile_override_invalid"):
        resolve_resume_provider_profile(
            config, saved_profile="saved", explicit_profile="not-configured"
        )

    monkeypatch.delenv("SAVED_KEY")
    with pytest.raises(WorkflowProfileResolutionError, match="provider_profile_invalid") as error:
        resolve_resume_provider_profile(config, saved_profile="saved")
    assert "saved-test-key" not in str(error.value)
    assert "SAVED_KEY" not in str(error.value)


def test_missing_saved_profile_fails_without_mutating_base_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    config = _config(tmp_path)

    with pytest.raises(WorkflowProfileResolutionError, match="provider_profile_unavailable"):
        resolve_resume_provider_profile(config, saved_profile="removed")

    assert config.execution.profile == "current"


def test_missing_saved_profile_can_be_recovered_with_explicit_current_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    config = _config(tmp_path)
    config = replace(config, providers={"current": config.providers["current"]})

    with pytest.raises(
        WorkflowProfileResolutionError, match="provider_profile_unavailable"
    ) as error:
        resolve_resume_provider_profile(config, saved_profile="opencode_go")
    assert "/workflow resume [run-id] --profile <name|none>" in str(error.value)
    assert "No provider request was sent" in str(error.value)
    assert "agenthicc config profiles" in str(error.value)

    selected = resolve_resume_provider_profile(
        config,
        saved_profile="opencode_go",
        explicit_profile="current",
    )
    assert selected.profile_name == "current"
    assert selected.source == "explicit override"
    assert selected.switched
    assert selected.config.execution._resolved_profile is not None

    legacy = resolve_resume_provider_profile(
        config,
        saved_profile="opencode_go",
        explicit_profile="",
    )
    assert legacy.profile_name == ""
    assert legacy.source == "explicit override"
    assert legacy.config.execution._resolved_profile is None
    assert legacy.config.execution.effective_api_key() == "legacy-secret"


def test_same_name_binding_drift_requires_explicit_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch)
    previous = provider_profile_fingerprint(_config(tmp_path), "saved")
    changed = _config(tmp_path, saved_endpoint="https://replacement.example/v1")

    with pytest.raises(WorkflowProfileResolutionError, match="provider_profile_override_required"):
        resolve_resume_provider_profile(
            changed,
            saved_profile="saved",
            saved_binding_fingerprint=previous,
        )

    acknowledged = resolve_resume_provider_profile(
        changed,
        saved_profile="saved",
        saved_binding_fingerprint=previous,
        explicit_profile="saved",
    )
    assert acknowledged.profile_name == "saved"
    assert acknowledged.binding_fingerprint != previous
    assert not acknowledged.switched


def test_secret_rotation_does_not_change_binding_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_keys(monkeypatch, saved="first-secret")
    config = _config(tmp_path)
    before = provider_profile_fingerprint(config, "saved")
    first = resolve_resume_provider_profile(config, saved_profile="saved")

    monkeypatch.setenv("SAVED_KEY", "rotated-secret")
    after = resolve_resume_provider_profile(config, saved_profile="saved")

    assert after.binding_fingerprint == before
    assert first.config.execution._resolved_profile.api_key == "first-secret"
    assert after.config.execution._resolved_profile.api_key == "rotated-secret"
    assert "rotated-secret" not in repr(after.binding_fingerprint)


def test_invocation_profile_override_uses_last_value_and_preserves_explicit_empty() -> None:
    assert (
        execution_profile_override(
            ("execution.profile=first", "other.value=ignored", "execution.profile=second")
        )
        == "second"
    )
    assert execution_profile_override(("execution.profile=",)) == ""
    assert execution_profile_override(("execution.model=x",)) is None


def test_resume_runner_is_always_forked_for_resolved_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.runners.tui_session import _LazyAgentRunner

    _set_keys(monkeypatch)
    config = _config(tmp_path)
    selection = resolve_resume_provider_profile(config, saved_profile="saved")
    original = _LazyAgentRunner(
        lambda: pytest.fail("resume must not materialize the default profile"),
        cassette_dir=None,
    )

    resumed = workflow_resume_agent_runner(original, selection, conversation_id="session-id")

    assert resumed is not original
    assert isinstance(resumed, _LazyAgentRunner)
    assert original._delegate is None
    assert resumed._delegate is None
    assert original._signals is resumed._signals


def test_lazy_runner_fork_does_not_construct_the_old_provider(tmp_path: Path) -> None:
    from agenthicc.runners.tui_session import _LazyAgentRunner

    old = _LazyAgentRunner(
        lambda: pytest.fail("old provider config must not be materialized"),
        cassette_dir=None,
    )

    fork = old.for_execution(_config(tmp_path).execution, conversation_id="session")

    assert old._delegate is None
    assert fork._delegate is None
    assert old._signals is fork._signals
