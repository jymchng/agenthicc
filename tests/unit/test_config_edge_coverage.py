"""Boundary coverage for typed configuration conversion and provider setup."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from agenthicc import config as config_module
from agenthicc.config import (
    AgentsSettings,
    CloakBrowserSettings,
    ExecutionSettings,
    PlaywrightSettings,
    StorageS3Settings,
    ToolSettings,
    build_llm_config,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (True, 9, 1),
        (3, 9, 3),
        (3.9, 9, 3),
        ("4", 9, 4),
        ("bad", 9, 9),
        (None, 9, 9),
    ],
)
def test_config_scalar_converters_accept_only_expected_shapes(
    value: object, default: int, expected: int
) -> None:
    assert config_module._as_int(value, default) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (True, 9.0, 1.0),
        (3, 9.0, 3.0),
        (3.9, 9.0, 3.9),
        ("4.5", 9.0, 4.5),
        ("bad", 9.0, 9.0),
        (None, 9.0, 9.0),
    ],
)
def test_config_float_converter(value: object, default: float, expected: float) -> None:
    assert config_module._as_float(value, default) == expected


def test_config_bool_and_list_converters_cover_fallbacks() -> None:
    assert config_module._as_bool(True, False) is True
    assert config_module._as_bool("YES", False) is True
    assert config_module._as_bool("off", True) is False
    assert config_module._as_bool("unknown", True) is True
    assert config_module._as_bool(None, False) is False
    assert config_module._as_string_list((1, 2)) == ["1", "2"]
    assert config_module._as_string_list("one") == ["one"]
    assert config_module._as_string_list(None, ("fallback",)) == ["fallback"]
    assert config_module._section(["not", "a", "table"]) == {}


def test_agent_settings_parse_legacy_skill_shapes_and_fallbacks() -> None:
    parsed = AgentsSettings.from_dict(
        {
            "default": {"skills": {"allow": "one", "deny": ["two"]}},
            "auto": {"allowed_skills": ["auto"], "max_turns": "4"},
            "ignored": "not a table",
        }
    )
    assert parsed.agents["default"].allowed_skills == ("one",)
    assert parsed.agents["default"].denied_skills == ("two",)
    assert parsed.agents["auto"].max_turns == 4
    assert parsed.skill_permissions_for("missing").allowed_skills == frozenset({"one"})
    assert AgentsSettings().skill_permissions_for("missing").allowed_skills is None
    assert parsed.skill_permissions_for("default").denied_skills == frozenset({"two"})


def test_storage_and_tool_settings_validate_configuration_boundaries() -> None:
    assert StorageS3Settings().configured is False
    assert StorageS3Settings(bucket="bucket").configured is True
    assert ToolSettings(browser_backend="NONE").browser_backend == "none"
    with pytest.raises(ValueError, match="browser_backend"):
        ToolSettings(browser_backend="unsupported")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transport": "remote"},
        {"navigation_timeout_s": 0},
        {"max_pages": 0},
        {"max_actions_per_turn": 0},
        {"max_snapshot_chars": 255},
        {"max_screenshot_bytes": 1023},
        {"profile_root": ""},
        {"profile_root": "/tmp/profile"},
        {"profile_root": "../profile"},
        {"license_key_env": ""},
    ],
)
def test_cloak_settings_fail_closed_for_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CloakBrowserSettings(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transport": "cdp", "cdp_endpoint": "ftp://127.0.0.1:9222"},
        {"transport": "cdp", "cdp_endpoint": "http://"},
        {"transport": "cdp", "cdp_endpoint": "http://user:pass@127.0.0.1:9222"},
        {"transport": "cdp", "cdp_endpoint": "http://127.0.0.1/path"},
        {"transport": "cdp", "cdp_endpoint": "http://127.0.0.1:70000"},
    ],
)
def test_cloak_cdp_endpoint_validation_rejects_unsafe_forms(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CloakBrowserSettings(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transport": "cdp"},
        {"navigation_timeout_s": 0},
        {"max_pages": 0},
        {"max_actions_per_turn": 0},
        {"max_snapshot_chars": 255},
        {"max_screenshot_bytes": 1023},
        {"profile_root": ""},
        {"profile_root": "/tmp/profile"},
        {"profile_root": "../profile"},
        {"browser_type": "firefox", "browser_channel": "chrome"},
    ],
)
def test_playwright_settings_fail_closed_for_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        PlaywrightSettings(**kwargs)  # type: ignore[arg-type]


def test_load_toml_safe_handles_missing_and_invalid_files(tmp_path: Path) -> None:
    assert config_module._load_toml_safe(tmp_path / "missing.toml") == {}
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[broken", encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert config_module._load_toml_safe(invalid) == {}
    assert any("Invalid TOML" in str(item.message) for item in caught)


def test_environment_overrides_prefer_explicit_agenthicc_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "shorthand-model")
    monkeypatch.setenv("AGENTHICC_EXECUTION_MODEL", "explicit-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example/v1")
    result = config_module._apply_env_overrides({})
    assert result["execution"] == {
        "provider": "openai",
        "model": "explicit-model",
        "base_url": "https://provider.example/v1",
    }


def test_invalid_extends_value_is_warned_and_ignored(tmp_path: Path) -> None:
    path = tmp_path / "invalid-extends.toml"
    path.write_text("extends = 42\n[execution]\nmodel = 'local'\n", encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = config_module._resolve_extends(path)
    assert result["execution"] == {"model": "local"}
    assert any("Invalid 'extends'" in str(item.message) for item in caught)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "ollama", "litellm"])
def test_build_llm_config_supports_all_providers(provider: str) -> None:
    execution = ExecutionSettings(
        provider=provider,
        model="test-model",
        api_key="test-key",
        base_url="https://provider.example/v1"
        if provider in {"anthropic", "openai", "ollama"}
        else "",
        prompt_cache=False,
    )
    built = build_llm_config(execution)
    assert built.provider == provider
    assert built.model == "test-model"


def test_build_llm_config_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_llm_config(ExecutionSettings(provider="unknown", model="model"))
