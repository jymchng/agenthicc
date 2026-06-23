"""Tests for per-model context-window configuration (PRD-136).

``[memory.context_windows]`` maps model id → window (plus a ``default`` catch-all)
and is the **single** source for the context system:
``ExecutionSettings.effective_context_window`` resolves the active model's window
(explicit entry → registry → config default → hardcoded), and
``effective_usable_budget`` derives the live working window the session memory is
sized to — which the auto-compaction trigger and the hard pre-send guard share.
"""

from __future__ import annotations

import pytest

from agenthicc.config import ExecutionSettings, load_config

pytestmark = pytest.mark.unit


def _exec(model: str, **windows: int) -> ExecutionSettings:
    return ExecutionSettings(provider="openai", model=model, context_windows=dict(windows))


class TestResolutionOrder:
    def test_explicit_entry_wins(self) -> None:
        e = _exec("deepseek-v4-flash", **{"deepseek-v4-flash": 250_000, "default": 1_000_000})
        assert e.effective_context_window() == 250_000

    def test_explicit_overrides_registry(self) -> None:
        # The proxy only allows 200k even though it's named like a 1M Opus.
        e = _exec("claude-opus-4-8", **{"claude-opus-4-8": 200_000})
        assert e.effective_context_window() == 200_000

    def test_registry_beats_config_default(self) -> None:
        # A known model NOT listed must keep its accurate registry window — a
        # generic default=1M must NOT inflate gpt-4o (128k) and risk overflow.
        e = _exec("gpt-4o", default=1_000_000)
        assert e.effective_context_window() == 128_000

    def test_config_default_for_unknown_model(self) -> None:
        e = _exec("some-proxy-model-xyz", default=1_000_000)
        assert e.effective_context_window() == 1_000_000

    def test_hardcoded_default_when_unknown_and_no_config_default(self) -> None:
        e = _exec("some-proxy-model-xyz")
        assert e.effective_context_window() == 200_000  # lauren-ai DEFAULT_CONTEXT_WINDOW

    def test_empty_model_uses_provider_default(self) -> None:
        # provider anthropic, empty model → claude-opus-4-8 → 1M registry.
        assert ExecutionSettings(provider="anthropic", model="").effective_context_window() == 1_000_000

    def test_case_insensitive_model_key(self) -> None:
        e = _exec("Claude-Opus-4-8", **{"claude-opus-4-8": 5_000_000})
        assert e.effective_context_window() == 5_000_000


class TestUsableBudget:
    def test_usable_is_under_window(self) -> None:
        e = _exec("m", m=200_000)
        assert e.effective_usable_budget() == 200_000 - 4096 - max(4000, 200_000 // 25)
        assert 0 < e.effective_usable_budget() < 200_000

    def test_scales_to_large_window(self) -> None:
        e = _exec("big", big=10_000_000)
        # reserve scales (window // 25); always leaves head-room.
        assert e.effective_usable_budget() == 10_000_000 - 4096 - 400_000

    def test_never_negative_for_tiny_window(self) -> None:
        e = _exec("tiny", tiny=1_000)
        assert e.effective_usable_budget() == 1


class TestLoadConfig:
    def test_parses_memory_context_windows_table(self, tmp_path) -> None:
        project = tmp_path / "agenthicc.toml"
        project.write_text(
            "[execution]\nmodel = 'deepseek-v4-flash'\n\n"
            "[memory.context_windows]\n"
            "default = 1000000\n"
            "deepseek-v4-flash = 250000\n"
            '"gpt-4.1" = 1000000\n'
        )
        cfg = load_config(project_path=project, user_path=tmp_path / "missing.toml", env_overrides=False)
        assert cfg.execution.context_windows == {
            "default": 1_000_000,
            "deepseek-v4-flash": 250_000,
            "gpt-4.1": 1_000_000,  # quoted dotted id survives
        }
        assert cfg.execution.effective_context_window() == 250_000

    def test_no_table_means_registry_only(self, tmp_path) -> None:
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\nmodel = 'gpt-4o'\n")
        cfg = load_config(project_path=project, user_path=tmp_path / "missing.toml", env_overrides=False)
        assert cfg.execution.context_windows == {}
        assert cfg.execution.effective_context_window() == 128_000
