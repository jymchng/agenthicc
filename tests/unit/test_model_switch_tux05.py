"""TUX-05 §2.5: what ``/model`` offers, what a choice runs with, and its notices (AC7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.config import (
    PROVIDER_API_KEY_ENVVAR,
    PROVIDER_DEFAULT_MODELS,
    SUPPORTED_PROVIDERS,
    AgenthiccConfig,
    ProviderProfile,
)
from agenthicc.runners.model_switch import (
    ModelChoice,
    execution_for,
    model_choice,
    model_choices,
    short_model,
)
from agenthicc.tui.input.command_usage import CommandUsage
from agenthicc.tui.preferences import save_config_value
from agenthicc.tui.workspace.notices import MODEL_CHANGED, model_changed_text

pytestmark = pytest.mark.unit

NO_ENV: dict[str, str] = {}


def _cfg() -> AgenthiccConfig:
    cfg = AgenthiccConfig()
    cfg.execution.provider = "anthropic"
    cfg.execution.model = "claude-test"
    return cfg


# ── the choices ───────────────────────────────────────────────────────────────


def test_every_supported_provider_is_offered_with_its_default_model() -> None:
    choices = model_choices(_cfg(), NO_ENV)
    offered = {(c.provider, c.model) for c in choices if not c.profile}
    for provider in SUPPORTED_PROVIDERS:
        assert (provider, PROVIDER_DEFAULT_MODELS.get(provider, "")) in offered


def test_the_current_model_is_listed_first_and_marked() -> None:
    choices = model_choices(_cfg(), NO_ENV)
    current = [c for c in choices if c.current]
    assert [(c.provider, c.model) for c in current] == [("anthropic", "claude-test")]
    assert choices[0] is current[0]


def test_a_provider_without_credentials_names_the_variable_to_set() -> None:
    choices = {c.provider: c for c in model_choices(_cfg(), NO_ENV) if not c.profile}
    for provider in ("anthropic", "openai"):
        variable = PROVIDER_API_KEY_ENVVAR[provider]
        assert choices[provider].missing_env == variable
        assert not choices[provider].available
    assert choices["ollama"].available and choices["ollama"].missing_env == ""


def test_credentials_in_the_environment_make_a_provider_available() -> None:
    env = {PROVIDER_API_KEY_ENVVAR["openai"]: "sk-not-a-real-key"}
    choices = {c.provider: c for c in model_choices(_cfg(), env) if not c.profile}
    assert choices["openai"].available
    assert not choices["anthropic"].available


def test_a_choice_never_carries_a_credential_value() -> None:
    secret = "sk-very-secret-value"
    env = {PROVIDER_API_KEY_ENVVAR["openai"]: secret}
    dump = repr(model_choices(_cfg(), env))
    assert secret not in dump


def test_context_window_comes_from_the_execution_settings() -> None:
    choice = model_choice(_cfg(), "anthropic", "claude-test", environ=NO_ENV)
    assert choice.context_window == _cfg().execution.effective_context_window()


def test_saved_profiles_follow_the_providers() -> None:
    cfg = _cfg()
    cfg.providers["work"] = ProviderProfile(
        name="work", provider="openai", model="gpt-work", api_key_env="WORK_KEY"
    )
    choices = model_choices(cfg, NO_ENV)
    profile = next(c for c in choices if c.profile == "work")
    assert (profile.provider, profile.model) == ("openai", "gpt-work")
    assert profile.missing_env == "WORK_KEY"
    assert choices.index(profile) > max(i for i, c in enumerate(choices) if not c.profile)
    assert model_choice(
        cfg, "openai", "gpt-work", profile="work", environ={"WORK_KEY": "x"}
    ).available


# ── what a choice runs with ───────────────────────────────────────────────────


def test_a_provider_choice_keeps_the_other_settings_and_drops_the_old_endpoint() -> None:
    cfg = _cfg()
    cfg.execution.base_url = "https://proxy.example/v1"
    cfg.execution.api_key = "sk-old"
    cfg.execution.transport_max_retries = 7
    execution = execution_for(cfg, model_choice(cfg, "openai", "gpt-x", environ=NO_ENV))
    assert (execution.provider, execution.effective_model()) == ("openai", "gpt-x")
    assert execution.base_url == "" and execution.api_key == ""
    assert execution.transport_max_retries == 7
    # The session's own settings are not edited in place.
    assert cfg.execution.provider == "anthropic" and cfg.execution.api_key == "sk-old"


def test_choosing_another_model_of_the_same_provider_keeps_its_endpoint_and_key() -> None:
    cfg = _cfg()
    cfg.execution.base_url = "https://proxy.example/v1"
    cfg.execution.api_key_env = "MY_KEY"
    execution = execution_for(cfg, model_choice(cfg, "anthropic", "claude-other", environ=NO_ENV))
    assert execution.base_url == "https://proxy.example/v1"
    assert execution.api_key_env == "MY_KEY"


def test_a_profile_choice_resolves_the_profile_like_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORK_KEY", "sk-work")
    cfg = _cfg()
    cfg.providers["work"] = ProviderProfile(
        name="work", provider="openai", model="gpt-work", api_key_env="WORK_KEY"
    )
    execution = execution_for(cfg, model_choice(cfg, "openai", "gpt-work", profile="work"))
    assert execution.profile == "work"
    assert execution.provider == "openai" and execution.effective_model() == "gpt-work"


def test_an_unresolvable_profile_is_a_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORK_KEY", raising=False)
    cfg = _cfg()
    cfg.providers["work"] = ProviderProfile(
        name="work", provider="openai", model="gpt-work", api_key_env="WORK_KEY"
    )
    with pytest.raises(ValueError):
        execution_for(cfg, model_choice(cfg, "openai", "gpt-work", profile="work"))


def test_short_model_drops_the_provider_prefix() -> None:
    assert short_model("anthropic/claude-x") == "claude-x"
    assert short_model("claude-x") == "claude-x"
    assert ModelChoice("p", "m").label == "p/m"


# ── the notice ────────────────────────────────────────────────────────────────


def test_the_model_changed_notice_is_one_line() -> None:
    text = model_changed_text(
        {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-6", "profile": ""}
    )
    assert text.endswith("claude-sonnet-4-6 (from next turn)") and "model" in text
    named = model_changed_text({"provider": "openai", "model": "gpt-w", "profile": "work"})
    assert "work (gpt-w)" in named
    assert "\n" not in text


async def test_the_scroll_buffer_renders_a_model_changed_event() -> None:
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.workspace.appender import ScrollBufferAppender

    state = AppState.create()
    console = Console(width=80, record=True, color_system=None)
    appender = ScrollBufferAppender(state, console)
    appender.mount()
    try:
        state.conversation.append_event(
            MODEL_CHANGED, {"provider": "openai", "model": "gpt-4o", "profile": ""}
        )
        await asyncio.sleep(0)
    finally:
        appender.unmount()
    text = console.export_text()
    assert "model" in text and "gpt-4o (from next turn)" in text


# ── saving as the default ─────────────────────────────────────────────────────


def test_save_config_value_sets_one_line_and_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# mine\n[ui]\ntheme = "dark"\n\n[execution]\nprovider = "anthropic"\n')
    save_config_value("execution", "model", "claude-x", path)
    save_config_value("execution", "provider", "openai", path)
    save_config_value("ui", "theme", "light", path)
    text = path.read_text()
    assert text.startswith("# mine\n")
    assert 'theme = "light"' in text and 'provider = "openai"' in text
    assert 'model = "claude-x"' in text
    assert text.count("[execution]") == 1 and text.count("[ui]") == 1


def test_save_config_value_appends_a_missing_table_and_rejects_bad_names(
    tmp_path: Path,
) -> None:
    path = tmp_path / "new" / "config.toml"
    save_config_value("execution", "model", 'a"b', path)
    assert path.read_text().splitlines() == ["[execution]", 'model = "a\\"b"']
    with pytest.raises(ValueError):
        save_config_value("exec ution", "model", "x", path)
    with pytest.raises(ValueError):
        save_config_value("execution", "mo del", "x", path)


# ── how often commands ran ────────────────────────────────────────────────────


def test_command_usage_survives_a_corrupt_file_and_bounds_its_size(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text("{not json")
    usage = CommandUsage(tmp_path, path)
    assert usage.counts() == {}
    for index in range(250):
        usage.record(f"/c{index}", now=float(index))
    counts = CommandUsage(tmp_path, path).counts()
    assert len(counts) == 200
    assert "/c0" not in counts and "/c249" in counts


def test_command_usage_most_used_orders_by_count_then_recency(tmp_path: Path) -> None:
    usage = CommandUsage(tmp_path, tmp_path / "usage.json")
    for name, when in (("/a", 1.0), ("/b", 2.0), ("/b", 3.0), ("/c", 4.0)):
        usage.record(name, now=when)
    assert usage.most_used(2) == ["/b", "/c"]


def test_command_usage_file_holds_only_names_counts_and_times(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    CommandUsage(tmp_path, path).record("/compact", now=5.0)
    (project,) = json.loads(path.read_text()).values()
    assert project == {"/compact": [1, 5.0]}
