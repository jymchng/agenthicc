"""Wall-clock bounds for tests that start a fresh ``agenthicc`` interpreter.

``python -m agenthicc`` imports its CLI command modules before it parses its
arguments, which costs several seconds of CPU even on an idle machine; a
detached worker or a ``python -c`` child that imports the package pays the same
start-up before it records its first durable state. ``nox -s coverage`` traces
every child interpreter (``[tool.coverage.run] patch = ["subprocess"]``) and
shared hosts are often heavily loaded, so the 15- and 30-second ceilings these
tests once set expired without any defect.

These bounds stop a hung process; they are not performance budgets.
``scripts/benchmark_startup.py`` and ``tests/performance`` measure start-up.
"""

from __future__ import annotations

import pytest

#: Ceiling for one wait on a fresh interpreter: a CLI command, or a child
#: process reaching its first durable state.
CLI_PROCESS_TIMEOUT_S = 120.0

#: Allowance for the in-process part of a test that also starts interpreters.
_IN_PROCESS_ALLOWANCE_S = 60.0


def cli_test_timeout(waits: int) -> pytest.MarkDecorator:
    """Return the ``pytest-timeout`` mark for a test with *waits* sequential interpreter waits.

    Each wait is bounded by :data:`CLI_PROCESS_TIMEOUT_S`, so the test-wide
    limit must exceed their sum or ``pytest-timeout`` fires first and the
    per-process ceilings never apply.
    """
    if waits < 1:
        raise ValueError("a test that starts an interpreter waits for it at least once")
    return pytest.mark.timeout(waits * CLI_PROCESS_TIMEOUT_S + _IN_PROCESS_ALLOWANCE_S)
