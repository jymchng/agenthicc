"""TUX-03 §2.3: the pre-flight check says whether a turn can succeed, from the config and environment alone."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from agenthicc.config import (
    AgenthiccConfig,
    ExecutionSettings,
    ProviderProfile,
    SecretReference,
)
from agenthicc.runners.preflight import (
    PreflightCheck,
    PreflightIssue,
    check_credentials,
    check_session,
)

pytestmark = pytest.mark.unit

#: A PATH holding fake ``rg`` and ``bwrap`` programs, so the tools check passes.
_TOOLS = ("rg", "bwrap")


@pytest.fixture
def path_with_tools(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in _TOOLS:
        program = bin_dir / name
        program.write_text("#!/bin/sh\n", encoding="utf-8")
        program.chmod(0o755)
    return str(bin_dir)


def _config(provider: str = "anthropic", **execution: object) -> AgenthiccConfig:
    config = AgenthiccConfig()
    config.execution = ExecutionSettings(provider=provider, **execution)  # type: ignore[arg-type]
    return config


def _environment(path: str, **extra: str) -> Mapping[str, str]:
    return {"PATH": path, **extra}


# ── credentials ───────────────────────────────────────────────────────────────


def test_a_missing_key_names_the_variable_and_the_way_out(path_with_tools: str) -> None:
    issues = check_session(_config(), _environment(path_with_tools), cwd=Path.cwd())

    assert [issue.check for issue in issues] == [PreflightCheck.CREDENTIALS]
    issue = issues[0]
    assert issue.blocking
    assert issue.text == (
        "No API key for anthropic. Set ANTHROPIC_API_KEY, or run /model to pick another provider."
    )


def test_the_providers_own_variable_passes(path_with_tools: str) -> None:
    env = _environment(path_with_tools, ANTHROPIC_API_KEY="sk-test")

    assert check_session(_config(), env, cwd=Path.cwd()) == ()


def test_an_anthropic_token_passes_without_a_key() -> None:
    assert check_credentials(_config(), {"ANTHROPIC_AUTH_TOKEN": "token"}) is None


def test_ollama_needs_no_key() -> None:
    assert check_credentials(_config("ollama"), {}) is None


def test_openai_names_its_own_variable() -> None:
    issue = check_credentials(_config("openai"), {})

    assert issue is not None
    assert "Set OPENAI_API_KEY" in issue.text


def test_a_key_from_a_saved_profiles_api_key_env_passes() -> None:
    config = _config()
    config.providers["work"] = ProviderProfile(
        name="work", provider="openai", api_key_env="WORK_OPENAI_KEY"
    )
    config.execution.profile = "work"

    assert check_credentials(config, {"WORK_OPENAI_KEY": "sk-work"}) is None
    missing = check_credentials(config, {"OPENAI_API_KEY": "the default variable is not used"})
    assert missing is not None
    assert missing.blocking
    assert missing.text == (
        "No API key for openai (profile work). "
        "Set WORK_OPENAI_KEY, or run /model to pick another provider."
    )


def test_a_secret_reference_names_its_variable() -> None:
    config = _config("openai", api_key=SecretReference("VAULT_KEY"))

    assert check_credentials(config, {"VAULT_KEY": "sk-vault"}) is None
    issue = check_credentials(config, {})
    assert issue is not None and "Set VAULT_KEY" in issue.text


def test_a_literal_key_in_the_configuration_passes() -> None:
    assert check_credentials(_config(api_key="sk-literal"), {}) is None


def test_the_execution_key_serves_a_profile_that_has_none() -> None:
    config = _config(api_key_env="SHARED_KEY")
    config.providers["bare"] = ProviderProfile(name="bare", provider="anthropic")
    config.execution.profile = "bare"

    assert check_credentials(config, {"SHARED_KEY": "sk"}) is None
    assert check_credentials(config, {}) is not None


def test_a_profile_that_does_not_exist_blocks() -> None:
    config = _config()
    config.execution.profile = "ghost"

    issue = check_credentials(config, {"ANTHROPIC_API_KEY": "sk"})

    assert issue is not None and issue.blocking
    assert "ghost" in issue.message


def test_the_check_never_returns_the_key() -> None:
    env = {"ANTHROPIC_API_KEY": "sk-secret-value"}
    config = _config(api_key_env="ANTHROPIC_API_KEY")

    assert check_credentials(config, env) is None
    issue = check_credentials(_config(), {})
    assert issue is not None
    assert "sk-secret-value" not in repr(issue)


# ── the model ─────────────────────────────────────────────────────────────────


def test_every_supported_provider_has_a_default_model_so_the_check_is_quiet(
    path_with_tools: str,
) -> None:
    for provider in ("anthropic", "openai", "ollama", "litellm"):
        env = _environment(path_with_tools, ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k")
        assert check_session(_config(provider), env, cwd=Path.cwd()) == (), provider


def test_a_provider_without_a_default_or_configured_model_is_reported(
    path_with_tools: str,
) -> None:
    config = _config("mystery")

    issues = check_session(config, _environment(path_with_tools), cwd=Path.cwd())

    assert [issue.check for issue in issues] == [PreflightCheck.MODEL]
    assert not issues[0].blocking
    assert (
        check_session(
            _config("mystery", model="my-model"), _environment(path_with_tools), cwd=Path.cwd()
        )
        == ()
    )


# ── the workspace ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="root ignores permissions")
def test_a_read_only_workspace_is_reported_unless_the_mode_is_plan(
    tmp_path: Path, path_with_tools: str
) -> None:
    workspace = tmp_path / "ro"
    workspace.mkdir()
    workspace.chmod(0o555)
    env = _environment(path_with_tools, ANTHROPIC_API_KEY="k")
    try:
        issues = check_session(_config(), env, cwd=workspace, mode="Safe")
        assert [issue.check for issue in issues] == [PreflightCheck.WORKSPACE]
        assert check_session(_config(), env, cwd=workspace, mode="Plan") == ()
    finally:
        workspace.chmod(0o755)


# ── tools ─────────────────────────────────────────────────────────────────────


def test_a_missing_ripgrep_is_an_info_finding(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    issues = check_session(_config("ollama"), {"PATH": str(empty)}, cwd=tmp_path)

    assert [issue.check for issue in issues] == [PreflightCheck.TOOLS]
    assert issues[0].severity == "info"
    assert "ripgrep" in issues[0].message
    assert not issues[0].blocking


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_bubblewrap_is_required_only_when_the_project_has_an_environment(
    tmp_path: Path, path_with_tools: str
) -> None:
    only_rg = tmp_path / "only-rg"
    only_rg.mkdir()
    program = only_rg / "rg"
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    env = {"PATH": str(only_rg)}

    assert check_session(_config("ollama"), env, cwd=project) == ()

    (project / ".agenthicc").mkdir()
    (project / ".agenthicc" / "environment.toml").write_text("", encoding="utf-8")
    issues = check_session(_config("ollama"), env, cwd=project)
    assert [issue.check for issue in issues] == [PreflightCheck.TOOLS]
    assert "bwrap" in issues[0].message
    assert check_session(_config("ollama"), {"PATH": path_with_tools}, cwd=project) == ()


# ── shape ─────────────────────────────────────────────────────────────────────


def test_findings_come_blocking_first_and_are_values() -> None:
    issues = check_session(_config(), {"PATH": ""}, cwd=Path.cwd())

    assert issues[0].blocking
    assert isinstance(issues[0], PreflightIssue)
    assert [issue.check for issue in issues] == [
        PreflightCheck.CREDENTIALS,
        PreflightCheck.TOOLS,
    ]


def test_the_check_is_fast() -> None:
    import time

    started = time.perf_counter()
    for _ in range(50):
        check_session(_config(), {"PATH": os.environ.get("PATH", "")}, cwd=Path.cwd())
    assert (time.perf_counter() - started) / 50 < 0.05


# ── one resolver for the pre-flight check and the /model picker ───────────────


def _profile_config() -> AgenthiccConfig:
    config = _config()
    config.providers["work"] = ProviderProfile(
        name="work", provider="openai", api_key_env="WORK_OPENAI_KEY"
    )
    config.providers["vault"] = ProviderProfile(
        name="vault", provider="openai", api_key=SecretReference("VAULT_KEY")
    )
    config.providers["bare"] = ProviderProfile(name="bare", provider="anthropic")
    return config


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"ANTHROPIC_API_KEY": "k"},
        {"ANTHROPIC_AUTH_TOKEN": "t"},
        {"OPENAI_API_KEY": "k"},
        {"WORK_OPENAI_KEY": "k"},
        {"VAULT_KEY": "k"},
    ],
    ids=["none", "anthropic-key", "anthropic-token", "openai-key", "work-key", "vault-key"],
)
def test_the_model_picker_offers_a_choice_exactly_when_a_turn_on_it_would_pass(
    environment: dict[str, str],
) -> None:
    """The picker and the start-up check ask `credential_variable` the same question."""
    from agenthicc.runners.model_switch import model_choices

    for profile in ("", "work", "vault", "bare"):
        config = _profile_config()
        config.execution.profile = profile
        active = check_credentials(config, environment) is None
        # The active choice: the current provider default, or the saved profile.
        chosen = next(
            choice
            for choice in model_choices(config, environment)
            if choice.profile == profile and (choice.current or profile)
        )
        assert chosen.available is active, (profile, environment)


def test_an_anthropic_token_makes_the_picker_offer_anthropic() -> None:
    from agenthicc.runners.model_switch import model_choice

    config = _config()

    assert model_choice(config, "anthropic", environ={}).missing_env == "ANTHROPIC_API_KEY"
    assert model_choice(config, "anthropic", environ={"ANTHROPIC_AUTH_TOKEN": "t"}).available


def test_the_execution_key_serves_only_the_provider_it_was_written_for() -> None:
    from agenthicc.runners.preflight import credential_variable

    config = _config("anthropic", api_key_env="MY_ANTHROPIC_KEY")

    assert credential_variable(config, "anthropic", "", {}) == "MY_ANTHROPIC_KEY"
    assert credential_variable(config, "openai", "", {}) == "OPENAI_API_KEY"
    assert credential_variable(config, "ollama", "", {}) == ""
