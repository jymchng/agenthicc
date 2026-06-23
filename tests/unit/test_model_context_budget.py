"""Tests for model-aware context budgeting in agenthicc (PRD-133 B).

``ExecutionSettings.effective_context_window`` resolves the active model's
window — from an explicit ``[execution] model_context_window`` override or the
lauren-ai registry — which feeds both the per-turn summarisation budget and the
hard pre-send guard (lauren-ai ``AgentConfig.context_window``).
"""

from __future__ import annotations

import pytest

from agenthicc.config import ExecutionSettings, load_config

pytestmark = pytest.mark.unit


class TestEffectiveContextWindow:
    def test_resolves_from_registry(self) -> None:
        assert ExecutionSettings(provider="anthropic", model="claude-opus-4-8").effective_context_window() == 1_000_000
        assert ExecutionSettings(provider="openai", model="gpt-4o").effective_context_window() == 128_000

    def test_default_model_per_provider(self) -> None:
        # empty model → PROVIDER_DEFAULT_MODELS (claude-opus-4-8) → 1M window.
        assert ExecutionSettings(provider="anthropic", model="").effective_context_window() == 1_000_000

    def test_override_wins_over_registry(self) -> None:
        # A proxied/unknown model behind a gateway: the override is authoritative.
        e = ExecutionSettings(provider="openai", model="deepseek-v4-flash", model_context_window=1_048_576)
        assert e.effective_context_window() == 1_048_576

    def test_unknown_model_without_override_uses_conservative_default(self) -> None:
        e = ExecutionSettings(provider="openai", model="deepseek-v4-flash")
        assert e.effective_context_window() == 200_000  # DEFAULT_CONTEXT_WINDOW


class TestLoadConfigParsesOverride:
    def test_model_context_window_parsed(self, tmp_path) -> None:
        project = tmp_path / "agenthicc.toml"
        project.write_text(
            "[execution]\nprovider = 'openai'\nmodel = 'deepseek-v4-flash'\nmodel_context_window = 65536\n"
        )
        cfg = load_config(
            project_path=project, user_path=tmp_path / "missing.toml", env_overrides=False
        )
        assert cfg.execution.model_context_window == 65536
        assert cfg.execution.effective_context_window() == 65536

    def test_default_is_zero_means_registry(self, tmp_path) -> None:
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\nmodel = 'gpt-4o'\n")
        cfg = load_config(
            project_path=project, user_path=tmp_path / "missing.toml", env_overrides=False
        )
        assert cfg.execution.model_context_window == 0
        assert cfg.execution.effective_context_window() == 128_000


class TestDerivedBudgetMath:
    """The window→usable derivation agent_turn applies before each run."""

    @staticmethod
    def _usable(window: int, max_out: int = 4096) -> int:
        reserve = max(4_000, window // 25)
        return max(1, window - max_out - reserve)

    def test_usable_under_window(self) -> None:
        for model, window in (("claude-opus-4-8", 1_000_000), ("gpt-4o", 128_000)):
            e = ExecutionSettings(provider="anthropic", model=model)
            w = e.effective_context_window()
            assert w == window
            usable = self._usable(w)
            assert 0 < usable < w  # always leaves head-room for output + framing
