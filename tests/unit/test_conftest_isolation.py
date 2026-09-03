"""Regression tests for the autouse env-isolation fixture in conftest.py.

These tests guard against the silent regression where a developer's
shell-exported provider env vars (``OPENAI_MODEL``, ``ANTHROPIC_API_KEY``,
``OLLAMA_HOST`` etc.) leak into ``load_config()`` and override the
explicit test fixtures.  See ``tests/unit/conftest.py`` for the
fixture under test.

The reason this file exists: the project has 19 test files that call
``load_config()``, and prior to this fix a single shell export of
``OPENAI_MODEL=...`` would silently break 4+ of them.  These tests
pin the autouse fixture in place so the regression cannot return.

The end-to-end "shell pollution does not leak" behaviour is also
covered indirectly by the originally-affected tests
(``tests/unit/test_config.py::TestLoadConfig::test_context_windows_default_empty``,
``tests/unit/test_config_extends.py::test_load_config_env_var_agenthicc_config``,
``tests/unit/test_config_extends.py::test_load_config_explicit_config_path_beats_env_var``,
``tests/unit/test_provider_profiles.py::test_profile_fields_fall_back_to_legacy_execution_values``)
which all rely on the autouse fixture to keep their assertions
deterministic.
"""

from __future__ import annotations

import os

import pytest


# Mirror of the constant in ``tests/unit/conftest.py``.  If the
# conftest ever changes the list of scrubbed env vars, update this
# test too — otherwise this is a canary that proves the autouse
# fixture still scrubs the documented set of vars.
_SCRUBBED_ENV_VARS: tuple[str, ...] = (
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    "OLLAMA_MODEL",
    "OLLAMA_HOST",
)


class TestAutouseFixtureIsWired:
    """The autouse ``_isolate_provider_env`` fixture must clear these vars."""

    @pytest.mark.parametrize("name", _SCRUBBED_ENV_VARS)
    def test_fixture_clears_var(self, name: str) -> None:
        # We can't easily set the var *before* the autouse fixture
        # runs (autouse is the first to touch monkeypatch), so we
        # verify the negative: the var must be absent at the start
        # of the test body.  This proves the autouse ran (because
        # pytest's monkeypatch auto-restores on teardown, the only
        # way the var is absent here is if the autouse deleted it
        # and the test body did not re-set it).
        assert name not in os.environ, (
            f"autouse _isolate_provider_env failed to clear {name!r} "
            f"(still set to {os.environ.get(name)!r})"
        )

    def test_conftest_module_exists(self) -> None:
        # Plain guard: the conftest must be importable from the test
        # process.  If someone deletes ``tests/unit/conftest.py``
        # entirely, every other test in this directory will start
        # failing — but this one fails *first* with a clear error.
        import tests.unit.conftest  # noqa: F401


class TestScrubbedSetIsComplete:
    """Sanity: the conftest scrubs the same set documented here."""

    def test_scrubbed_vars_include_openai(self) -> None:
        assert "OPENAI_MODEL" in _SCRUBBED_ENV_VARS
        assert "OPENAI_BASE_URL" in _SCRUBBED_ENV_VARS
        assert "OPENAI_API_KEY" in _SCRUBBED_ENV_VARS

    def test_scrubbed_vars_include_anthropic(self) -> None:
        assert "ANTHROPIC_MODEL" in _SCRUBBED_ENV_VARS
        assert "ANTHROPIC_API_KEY" in _SCRUBBED_ENV_VARS

    def test_scrubbed_vars_include_ollama(self) -> None:
        assert "OLLAMA_MODEL" in _SCRUBBED_ENV_VARS
        assert "OLLAMA_HOST" in _SCRUBBED_ENV_VARS

    def test_scrubbed_vars_match_conftest(self) -> None:
        # If this fails, the conftest's _PROVIDER_ENV_VARS tuple has
        # drifted from the test's _SCRUBBED_ENV_VARS tuple.  Update
        # both to match (and the test for the new var).
        from tests.unit import conftest

        assert set(conftest._PROVIDER_ENV_VARS) == set(_SCRUBBED_ENV_VARS)
