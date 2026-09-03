"""Shared fixtures for the unit test suite.

This file is auto-loaded by pytest for every test in ``tests/unit/`` and
its sub-directories.  It currently provides a single autouse fixture that
scrubs provider-shortcut environment variables so that the test suite is
deterministic regardless of the developer's shell environment.

Why this is needed
------------------

``agenthicc.config.load_config()`` consults several env vars at import
time to populate the active model / provider (see
``PROVIDER_ENV_SHORTCUTS`` and ``PROVIDER_API_KEY_ENVVAR`` in
``src/agenthicc/config.py``):

* ``OPENAI_MODEL``, ``OPENAI_BASE_URL``
* ``ANTHROPIC_MODEL``
* ``OLLAMA_MODEL``, ``OLLAMA_HOST``
* ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``  (provider auto-inference)

If any of these are exported in the developer's shell (which is common
when working with real LLM providers, e.g. ``export
OPENAI_MODEL=claude-opus-4-8``), they silently leak into the unit tests
and override the test fixtures' explicit ``[execution] model = '...'``
values.  The result is a flaky suite: tests pass on a "clean" shell but
fail when the developer has any provider env var exported.

The fixture below clears these env vars (restoring their original value
after the test) so that unit tests are fully isolated from the host
environment.  Integration and end-to-end tests are intentionally not
covered by this fixture — they often rely on real provider credentials.
"""

from __future__ import annotations

import pytest

# Names of env vars that ``agenthicc.config.load_config`` consults
# outside of the ``AGENTHICC_*`` namespace.  Clearing these at the start
# of every unit test makes ``load_config`` rely solely on test fixtures
# (TOML files, ``monkeypatch.setenv``, or programmatic defaults).
_PROVIDER_ENV_VARS: tuple[str, ...] = (
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    "OLLAMA_MODEL",
    "OLLAMA_HOST",
)


@pytest.fixture(autouse=True)
def _isolate_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear provider-shortcut env vars for the duration of each unit test.

    ``monkeypatch.delenv(name, raising=False)`` is a no-op if the var is
    already unset and is automatically restored at the end of the test.
    Tests that need one of these vars (e.g. ``tests/unit/test_config_edge_coverage.py::test_environment_overrides_prefer_explicit_agenthicc_values``)
    set them explicitly via ``monkeypatch.setenv`` so the autouse clear
    is harmless.
    """
    for name in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
