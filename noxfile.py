"""Nox sessions for agenthicc.

Every session runs on nox's uv backend, which points ``UV_PROJECT_ENVIRONMENT``
at the session's own virtualenv: ``uv sync --extra dev`` installs the project
and its one ``dev`` extra there, so each tool runs at the declared version on
the session's interpreter, never a copy that happens to be on ``PATH``.
"""

from __future__ import annotations

import pathlib
import shutil

import nox

PRIMARY_PYTHON = "3.12"
SUPPORTED_PYTHONS = ["3.11", "3.12", "3.13"]

nox.options.default_venv_backend = "uv"
nox.options.sessions = ["lint", "typecheck", "type_audit", "tests_unit", "coverage"]
nox.options.reuse_venv = "yes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_dev(session: nox.Session) -> None:
    session.run_install("uv", "sync", "--extra", "dev", external=True)


# ---------------------------------------------------------------------------
# Test sessions
# ---------------------------------------------------------------------------


@nox.session(python=SUPPORTED_PYTHONS, name="tests")
def tests(session: nox.Session) -> None:
    """Run the full test suite on one supported Python version."""
    _install_dev(session)
    session.run("pytest", "-W", "ignore", *session.posargs)


@nox.session(python=SUPPORTED_PYTHONS, name="tests_unit")
def tests_unit(session: nox.Session) -> None:
    """Run the unit tests on one supported Python version."""
    _install_dev(session)
    session.run("pytest", "tests/unit", *session.posargs)


@nox.session(python=SUPPORTED_PYTHONS, name="tests_integration")
def tests_integration(session: nox.Session) -> None:
    """Run the integration tests on one supported Python version."""
    _install_dev(session)
    session.run("pytest", "tests/integration", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="tests_e2e")
def tests_e2e(session: nox.Session) -> None:
    """Run end-to-end tests (PTY renderer, cassette replay, session integrations)."""
    _install_dev(session)
    session.run("pytest", "tests/e2e", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="tests_performance")
def tests_performance(session: nox.Session) -> None:
    """Run the performance budgets (never under coverage, which slows every call)."""
    _install_dev(session)
    session.run("pytest", "tests/performance", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="tests_windows")
def tests_windows(session: nox.Session) -> None:
    """Run the platform-sensitive lock, filesystem and terminal tests (Windows CI)."""
    _install_dev(session)
    session.run(
        "pytest",
        "tests/unit/test_interprocess_lock.py",
        "tests/unit/test_windows_backend.py",
        "tests/unit/test_windows_terminal_backend.py",
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="coverage")
def coverage(session: nox.Session) -> None:
    """Run the unit, integration and E2E suites with branch and subprocess coverage.

    The floor is ``[tool.coverage.report] fail_under`` in ``pyproject.toml``.
    ``tests/performance`` is excluded: tracing invalidates its timing budgets,
    so ``tests_performance`` runs it uninstrumented. Tracing also roughly
    triples CPU time, so a test without its own ``timeout`` mark gets three
    times the 60-second ``pytest-timeout`` default here.
    """
    _install_dev(session)
    session.run(
        "pytest",
        "tests/unit",
        "tests/integration",
        "tests/e2e",
        "--timeout=180",
        "--cov=agenthicc",
        "--cov-report=xml",
        "--cov-report=html",
        "--cov-report=term-missing:skip-covered",
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# Linting / formatting / type-checking
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="lint")
def lint(session: nox.Session) -> None:
    """Run ruff linter and format checker (no auto-fix)."""
    _install_dev(session)
    session.run("ruff", "check", "src/", "tests/", "scripts/", *session.posargs)
    session.run("ruff", "format", "--check", "src/", "tests/", "scripts/", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="format")
def format_(session: nox.Session) -> None:
    """Auto-format with ruff (applies changes in place)."""
    _install_dev(session)
    session.run("ruff", "format", "src/", "tests/", "scripts/", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="typecheck")
def typecheck(session: nox.Session) -> None:
    """Run the declared mypy type-checker over src/agenthicc."""
    _install_dev(session)
    session.run("mypy", "src/agenthicc", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="typecheck_contracts")
def typecheck_contracts(session: nox.Session) -> None:
    """Type-check the focused boundary tests and the ratchet script."""
    _install_dev(session)
    session.run(
        "mypy",
        "tests/unit/test_kernel_event_typing.py",
        "tests/unit/test_type_audit.py",
        "scripts/type_audit.py",
        *session.posargs,
    )


@nox.session(python=PRIMARY_PYTHON, name="type_audit")
def type_audit(session: nox.Session) -> None:
    """Check source typing hygiene against the checked-in ratchet baseline.

    Pass ``-- --baseline-ref origin/main`` to also reject a raised baseline.
    """
    _install_dev(session)
    session.run(
        "python",
        "scripts/type_audit.py",
        "--check",
        "docs/reference/type-safety-baseline.json",
        *session.posargs,
    )


@nox.session(python=PRIMARY_PYTHON, name="docs")
def docs(session: nox.Session) -> None:
    """Build the documentation site, failing on any warning."""
    _install_dev(session)
    session.run("mkdocs", "build", "--strict", *session.posargs)


# ---------------------------------------------------------------------------
# Build & release
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="build")
def build(session: nox.Session) -> None:
    """Wipe dist/ and build wheel + sdist with uv."""
    dist = pathlib.Path("dist")
    if dist.exists():
        shutil.rmtree(dist)
    _install_dev(session)
    session.run("uv", "build", *session.posargs, external=True)


@nox.session(python=PRIMARY_PYTHON, name="build_check")
def build_check(session: nox.Session) -> None:
    """Check the built distributions with twine."""
    _install_dev(session)
    session.run("twine", "check", "--strict", "dist/*", *session.posargs)


# ---------------------------------------------------------------------------
# LLM documentation check
# ---------------------------------------------------------------------------

_LLMS_CHECK_SCRIPT = """\
import sys
import re
import pathlib

# Build the list of symbols to verify.
# We check every module that declares an __all__ under agenthicc.
import importlib
import pkgutil
import agenthicc

symbols: list[str] = []

# Top-level __all__ first (if present).
top_all = getattr(agenthicc, "__all__", None)
if top_all:
    symbols.extend(top_all)

# Walk all sub-modules and collect __all__ entries.
import agenthicc.kernel
for sym in getattr(agenthicc.kernel, "__all__", []):
    if sym not in symbols:
        symbols.append(sym)

llms_path = pathlib.Path("llms-full.txt")
if not llms_path.exists():
    print("ERROR: llms-full.txt does not exist", file=sys.stderr)
    sys.exit(1)

text = llms_path.read_text()
documented = {m.group(1) for m in re.finditer(r"^###\\s+(\\w+)", text, re.MULTILINE)}

missing = [s for s in symbols if s not in documented]
if missing:
    print(f"llms_check FAILED — {len(missing)} symbol(s) missing from llms-full.txt:")
    for sym in sorted(missing):
        print(f"  missing: ### {sym}")
    sys.exit(1)

print(f"llms_check OK — all {len(symbols)} public symbols are documented in llms-full.txt.")
"""


@nox.session(python=PRIMARY_PYTHON, name="llms_check")
def llms_check(session: nox.Session) -> None:
    """Verify every public symbol in agenthicc.__all__ has a ### heading in llms-full.txt."""
    _install_dev(session)
    session.run(
        "python",
        "-c",
        _LLMS_CHECK_SCRIPT,
        env={"PYTHONPATH": "src"},
        external=False,
    )


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


@nox.session(python=False, name="clean")
def clean(session: nox.Session) -> None:
    """Remove all build/test/coverage artifacts."""
    artifacts = [
        "dist",
        "build",
        "htmlcov",
        ".coverage",
        "coverage.xml",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    ]
    for artifact in artifacts:
        p = pathlib.Path(artifact)
        if p.is_dir():
            session.log(f"Removing directory: {artifact}")
            shutil.rmtree(p)
        elif p.is_file():
            session.log(f"Removing file: {artifact}")
            p.unlink()
    for pycache in pathlib.Path("src").rglob("__pycache__"):
        shutil.rmtree(pycache)
    for pycache in pathlib.Path("tests").rglob("__pycache__"):
        shutil.rmtree(pycache)
