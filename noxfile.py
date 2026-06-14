"""Nox sessions for agenthicc."""

from __future__ import annotations

import pathlib
import shutil

import nox

PRIMARY_PYTHON = "3.12"
SUPPORTED_PYTHONS = ["3.11", "3.12", "3.13"]

nox.options.sessions = ["lint", "tests", "format", "build", "build_check", "llms_check"]
nox.options.reuse_venv = "yes"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_dev(session: nox.Session) -> None:
    # All nox session venvs in this repo may be root-owned, preventing uv from
    # installing into them.  Sync into the project .venv (no --active) so all
    # tools (ruff, mypy, twine, …) are available in .venv/bin/.
    # Sessions use external=True to find tools via PATH, and pytest sessions
    # use pythonpath=["src"] in pyproject.toml to import agenthicc.
    session.run("uv", "sync", "--extra", "dev", external=True)


def _install_all(session: nox.Session) -> None:
    session.run("uv", "sync", "--extra", "cloud", "--extra", "dev", external=True)


# ---------------------------------------------------------------------------
# Test sessions
# ---------------------------------------------------------------------------


@nox.session(python=SUPPORTED_PYTHONS, name="tests")
def tests(session: nox.Session) -> None:
    """Run full test suite (parametrised across supported Python versions)."""
    _install_dev(session)
    session.run("pytest", "-W", "ignore", *session.posargs, external=True)


@nox.session(python=PRIMARY_PYTHON, name="tests_unit")
def tests_unit(session: nox.Session) -> None:
    """Run only unit tests on the primary Python version."""
    _install_dev(session)
    session.run("pytest", "tests/unit", *session.posargs, external=True)


@nox.session(python=PRIMARY_PYTHON, name="tests_integration")
def tests_integration(session: nox.Session) -> None:
    """Run integration tests (installs cloud extra)."""
    _install_all(session)
    session.run("pytest", "tests/integration", *session.posargs, external=True)


@nox.session(python=PRIMARY_PYTHON, name="tests_e2e")
def tests_e2e(session: nox.Session) -> None:
    """Run end-to-end tests (requires live API keys in the environment)."""
    _install_all(session)
    session.run("pytest", "tests/e2e", *session.posargs, external=True)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="coverage")
def coverage(session: nox.Session) -> None:
    """Run full suite with coverage and produce a report."""
    _install_all(session)
    session.run(
        "pytest",
        "--cov=src/agenthicc",
        "--cov-report=term-missing",
        "--cov-report=html:htmlcov",
        "--cov-report=xml:coverage.xml",
        *session.posargs,
        external=True,
    )


# ---------------------------------------------------------------------------
# Linting / formatting / type-checking
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="lint")
def lint(session: nox.Session) -> None:
    """Run ruff linter (with auto-fix)."""
    _install_dev(session)
    session.run(
        "ruff",
        "check",
        "--fix",
        "src",
        "tests",
        "noxfile.py",
        *session.posargs,
        external=True,
    )


@nox.session(python=PRIMARY_PYTHON, name="format")
def format_(session: nox.Session) -> None:
    """Run ruff formatter."""
    _install_dev(session)
    session.run(
        "ruff",
        "format",
        "src",
        "tests",
        "noxfile.py",
        *session.posargs,
        external=True,
    )


@nox.session(python=PRIMARY_PYTHON, name="typecheck")
def typecheck(session: nox.Session) -> None:
    """Run mypy type-checker over src/agenthicc."""
    _install_dev(session)
    session.run("mypy", "src/agenthicc", *session.posargs, external=True)


# ---------------------------------------------------------------------------
# Auxiliary checks
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="llms_check")
def llms_check(session: nox.Session) -> None:
    """Verify that llms-full.txt covers all public symbols."""
    _install_dev(session)
    # Add src/ to PYTHONPATH so agenthicc is importable directly from source
    # without requiring an editable install in the session venv.
    session.run(
        "python",
        "scripts/check_llms.py",
        *session.posargs,
        env={"PYTHONPATH": "src"},
        external=True,
    )


# ---------------------------------------------------------------------------
# Build & release
# ---------------------------------------------------------------------------


@nox.session(python=PRIMARY_PYTHON, name="build")
def build(session: nox.Session) -> None:
    """Wipe dist/ and build wheel + sdist."""
    dist = pathlib.Path("dist")
    if dist.exists():
        shutil.rmtree(dist)
    _install_dev(session)
    out_dir = str(dist)
    session.run(
        "uv",
        "build",
        "--wheel",
        "--sdist",
        "--out-dir",
        out_dir,
        *session.posargs,
        external=True,
    )


@nox.session(python=PRIMARY_PYTHON, name="build_check")
def build_check(session: nox.Session) -> None:
    """Check the built distributions with twine."""
    _install_dev(session)
    session.run("twine", "check", "dist/*", *session.posargs, external=True)


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


@nox.session(python=False, name="clean")
def clean(session: nox.Session) -> None:
    """Remove all build/test/coverage artefacts."""
    artifacts = [
        "dist",
        "build",
        "htmlcov",
        ".coverage",
        "coverage.xml",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site",
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
