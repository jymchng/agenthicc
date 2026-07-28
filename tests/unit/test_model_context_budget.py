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
        assert (
            ExecutionSettings(provider="anthropic", model="").effective_context_window()
            == 1_000_000
        )

    def test_case_insensitive_model_key(self) -> None:
        e = _exec("Claude-Opus-4-8", **{"claude-opus-4-8": 5_000_000})
        assert e.effective_context_window() == 5_000_000

    def test_legacy_lauren_resolver_without_default_keyword(self, monkeypatch) -> None:
        """A one-argument lauren-ai resolver must not abort TUI startup."""
        from lauren_ai import _config as lauren_config

        def legacy_context_window_for(model: str) -> int:
            return 321_000 if model == "legacy-model" else 200_000

        monkeypatch.setattr(
            lauren_config,
            "context_window_for",
            legacy_context_window_for,
            raising=False,
        )

        from agenthicc.config import _context_window_for

        assert _context_window_for("legacy-model", default=0) == 321_000
        assert _context_window_for("unknown-legacy-model", default=777_000) == 777_000
        assert (
            ExecutionSettings(provider="openai", model="legacy-model").effective_context_window()
            == 321_000
        )

    def test_lauren_resolver_with_default_keyword_still_gets_sentinel(self, monkeypatch) -> None:
        from lauren_ai import _config as lauren_config

        calls: list[int] = []

        def modern_context_window_for(model: str, *, default: int = 200_000) -> int:
            calls.append(default)
            return default

        monkeypatch.setattr(
            lauren_config,
            "context_window_for",
            modern_context_window_for,
            raising=False,
        )

        from agenthicc.config import _context_window_for

        assert _context_window_for("unknown-modern-model", default=0) == 0
        assert calls == [0]

    def test_unexpected_default_keyword_from_wrapper_is_retried(self, monkeypatch) -> None:
        from lauren_ai import _config as lauren_config

        class LegacyWrapper:
            def __call__(self, model: str, **kwargs: object) -> int:
                if kwargs:
                    raise TypeError(
                        "context_window_for() got an unexpected keyword argument 'default'"
                    )
                return 654_321

        monkeypatch.setattr(lauren_config, "context_window_for", LegacyWrapper(), raising=False)

        from agenthicc.config import _context_window_for

        assert _context_window_for("wrapped-legacy-model", default=0) == 654_321


class TestUsableBudget:
    def test_usable_is_under_window(self) -> None:
        # The completion reservation is execution.max_output_tokens, not a constant.
        e = _exec("m", m=200_000)
        reserved = e.max_output_tokens
        assert e.effective_usable_budget() == 200_000 - reserved - max(4000, 200_000 // 25)
        assert 0 < e.effective_usable_budget() < 200_000

    def test_scales_to_large_window(self) -> None:
        e = _exec("big", big=10_000_000)
        # reserve scales (window // 25); always leaves head-room.
        assert e.effective_usable_budget() == 10_000_000 - e.max_output_tokens - 400_000

    def test_tracks_the_configured_output_ceiling(self) -> None:
        e = _exec("m", m=200_000)
        before = e.effective_usable_budget()
        e.max_output_tokens += 10_000
        assert e.effective_usable_budget() == before - 10_000

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
        cfg = load_config(
            project_path=project, user_path=tmp_path / "missing.toml", env_overrides=False
        )
        assert cfg.execution.context_windows == {
            "default": 1_000_000,
            "deepseek-v4-flash": 250_000,
            "gpt-4.1": 1_000_000,  # quoted dotted id survives
        }
        assert cfg.execution.effective_context_window() == 250_000

    def test_no_table_means_registry_only(self, tmp_path) -> None:
        project = tmp_path / "agenthicc.toml"
        project.write_text("[execution]\nmodel = 'gpt-4o'\n")
        cfg = load_config(
            project_path=project, user_path=tmp_path / "missing.toml", env_overrides=False
        )
        assert cfg.execution.context_windows == {}
        assert cfg.execution.effective_context_window() == 128_000
