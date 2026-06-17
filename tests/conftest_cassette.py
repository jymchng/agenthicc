"""Cassette replay fixtures — import explicitly in your test files.

These fixtures are NOT auto-loaded by conftest.py so they do not add overhead
to every test run.  Import only what you need::

    # In your test file:
    from tests.conftest_cassette import replay_cassette

    @pytest.mark.integration
    async def test_plan_mode(replay_cassette):
        result = await replay_cassette(
            "tests/fixtures/plan_mode/cassette.jsonl",
            approvals="tests/fixtures/plan_mode/approvals.jsonl",
            intent="enhance this repo",
        )
        assert result.status == "complete"
        assert "finalize_plan" in result.tools_called

Or import the lower-level primitives directly::

    from agenthicc.testing import SessionCassette, run_headless_replay

    cassette = SessionCassette.from_path(
        cassette_path="tests/fixtures/plan_mode/cassette.jsonl",
        approvals_path="tests/fixtures/plan_mode/approvals.jsonl",
        intent="enhance this repo",
    )
    result = await run_headless_replay(cassette)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agenthicc.testing import ReplayResult, SessionCassette, run_headless_replay


@pytest.fixture
def replay_cassette():
    """Fixture: run a cassette through CodePlanRunner and return a ReplayResult.

    Parameters
    ----------
    cassette_path:
        Path (str or Path) to the ``cassette.jsonl`` file.  Relative paths
        are resolved from the repository root (i.e. where pytest is invoked).
    approvals:
        Path to the ``approvals.jsonl`` file.  Pass ``None`` (default) to
        auto-approve all gates when no recordings are available.
    intent:
        The user intent string.  Required if ``meta.json`` next to
        ``cassette_path`` does not contain an ``intent`` key.
    cfg:
        Optional ``AgenthiccConfig`` override; loaded from disk when omitted.

    Returns
    -------
    Callable that returns ``Coroutine[ReplayResult]``.
    """

    async def _run(
        cassette_path: str | Path,
        *,
        approvals: str | Path | None = None,
        intent: str = "",
        cfg: Any = None,
    ) -> ReplayResult:
        cassette = SessionCassette.from_path(
            cassette_path=cassette_path,
            approvals_path=approvals,
            intent=intent,
        )
        return await run_headless_replay(cassette, intent=intent or None, cfg=cfg)

    return _run


@pytest.fixture
def load_cassette():
    """Fixture: load a :class:`SessionCassette` without running it.

    Useful when you want to inspect the cassette or customise it before
    passing it to :func:`~agenthicc.testing.run_headless_replay`::

        from tests.conftest_cassette import load_cassette

        async def test_inspect(load_cassette):
            cassette = load_cassette("tests/fixtures/plan_mode/cassette.jsonl")
            assert len(cassette.entries) > 0
            assert cassette.entries[0].response_stop_reason == "tool_use"
    """

    def _load(
        cassette_path: str | Path,
        *,
        approvals: str | Path | None = None,
        intent: str = "",
    ) -> SessionCassette:
        return SessionCassette.from_path(
            cassette_path=cassette_path,
            approvals_path=approvals,
            intent=intent,
        )

    return _load
