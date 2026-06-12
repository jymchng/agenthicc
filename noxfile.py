"""Nox sessions for agenthicc."""

from __future__ import annotations

import pathlib
import shutil
import sys

import nox

PRIMARY_PYTHON = "3.12"
SUPPORTED_PYTHONS = ["3.12", "3.13"]

nox.options.sessions = ["lint", "tests_unit", "tests_integration", "tests_e2e", "coverage"]
nox.options.reuse_venv = "yes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_dev(session: nox.Session) -> None:
    # All nox session venvs in this repo may be root-owned, preventing uv from
    # installing into them.  Sync into the project .venv (no --active) so all
    # tools (ruff, mypy, twine, …) are available in .venv/bin/.
    # Sessions use external=True to find tools there via PATH, and pytest
    # sessions use pythonpath=["src"] in pyproject.toml to import agenthicc.
    session.run("uv", "sync", "--extra", "dev", external=True)


def _install_all(session: nox.Session) -> None:
    session.run(
        "uv",
        "sync",
        "--extra",
        "dev",
        "--extra",
        "tui",
        "--extra",
        "api",
        external=True,
    )


# ---------------------------------------------------------------------------
# Test sessions
# ---------------------------------------------------------------------------


@nox.session(python=SUPPORTED_PYTHONS, name="tests")
def tests(session: nox.Session) -> None:
    """Run full test suite (parametrised across supported Python versions)."""
    _install_all(session)
    session.run("pytest", "-W", "ignore", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="tests_unit")
def tests_unit(session: nox.Session) -> None:
    """Run only unit tests on the primary Python version."""
    _install_dev(session)
    session.run("pytest", "tests/unit", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="tests_integration")
def tests_integration(session: nox.Session) -> None:
    """Run integration tests (installs tui + api extras)."""
    _install_all(session)
    session.run("pytest", "tests/integration", *session.posargs)


@nox.session(python=PRIMARY_PYTHON, name="tests_e2e")
def tests_e2e(session: nox.Session) -> None:
    """Run end-to-end tests (PTY renderer, FastAPI client)."""
    _install_all(session)
    session.run("pytest", "tests/e2e", *session.posargs)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="coverage")
def coverage(session: nox.Session) -> None:
    """Run full suite with coverage; require 85% minimum."""
    _install_all(session)
    session.run(
        "pytest",
        "--cov=agenthicc",
        "--cov-report=xml",
        "--cov-report=html",
        "--cov-report=term-missing",
        "--cov-fail-under=85",
        *session.posargs,
    )


# ---------------------------------------------------------------------------
# Linting / formatting / type-checking
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="lint")
def lint(session: nox.Session) -> None:
    """Run ruff linter and format checker (no auto-fix)."""
    _install_dev(session)
    session.run("ruff", "check", "src/", "tests/", *session.posargs, external=True)
    session.run("ruff", "format", "--check", "src/", "tests/", *session.posargs, external=True)


@nox.session(python=PRIMARY_PYTHON, name="format")
def format_(session: nox.Session) -> None:
    """Auto-format with ruff (applies changes in place)."""
    _install_dev(session)
    session.run("ruff", "format", "src/", "tests/", *session.posargs, external=True)


@nox.session(python=PRIMARY_PYTHON, name="typecheck")
def typecheck(session: nox.Session) -> None:
    """Run mypy type-checker over src/agenthicc (if mypy is available)."""
    _install_dev(session)
    session.run("mypy", "src/agenthicc", *session.posargs, external=True)


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
    session.run("twine", "check", "dist/*", *session.posargs, external=True)


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
