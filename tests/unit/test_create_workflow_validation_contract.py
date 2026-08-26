"""Regression coverage for structured generated-workflow validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agenthicc.workflows.create_workflow.inspection_tools import _RUNNER_EXAMPLE
from agenthicc.workflows.create_workflow.validation import validate_workflow_file

pytestmark = pytest.mark.unit


def test_strict_validation_rejects_a_second_agents_reader(tmp_path: Path) -> None:
    source = _RUNNER_EXAMPLE.replace(
        "    async def _plan(self, ctx: ReleaseContext, memory: object) -> ReleaseState:\n",
        "    async def _plan(self, ctx: ReleaseContext, memory: object) -> ReleaseState:\n"
        '        open("AGENTS.md", encoding="utf-8").read()\n',
        1,
    )
    path = tmp_path / "release_check" / "runner.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")

    report = validate_workflow_file(
        str(path.parent),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
    )

    assert not report.ok
    assert any("AGENTS.md" in error for error in report.errors)


def test_unrelated_plugin_error_does_not_corrupt_cache_contract_status(tmp_path: Path) -> None:
    source = _RUNNER_EXAMPLE.replace(
        '    description = "Plan release checks, run them, then report."',
        '    description = ""',
        1,
    )
    path = tmp_path / "release_check" / "runner.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")

    report = validate_workflow_file(
        str(path.parent),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
    )

    assert not report.ok
    assert report.cache_contract == "contract-native"
    assert report.categories["plugin"] == "fail"
    assert any("description is empty" in error for error in report.errors)


def test_malformed_phase_collection_is_reported_without_validator_crash(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text(
        """
from agenthicc.workflows.plugin import WorkflowPlugin

class Broken(WorkflowPlugin):
    name = "broken"
    description = "broken phase collection"
    mode_bindings = []
    phases = None
""",
        encoding="utf-8",
    )

    report = validate_workflow_file(str(path), expected_name="broken", root=tmp_path)

    assert not report.ok
    assert any("phases must be a list" in error for error in report.errors)


def test_nested_package_sources_are_validated(tmp_path: Path) -> None:
    package = tmp_path / "nested_workflow"
    package.mkdir()
    (package / "runner.py").write_text(
        """
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

class Nested(WorkflowPlugin):
    name = "nested_workflow"
    description = "nested source test"
    mode_bindings = []
    phases = [PhaseSpec(name="one")]
""",
        encoding="utf-8",
    )
    helpers = package / "helpers"
    helpers.mkdir()
    (helpers / "unsafe.py").write_text("import socket\n", encoding="utf-8")

    report = validate_workflow_file(
        str(package),
        expected_name="nested_workflow",
        root=tmp_path,
    )

    assert not report.ok
    assert any("socket" in error for error in report.errors)


def test_package_validation_rejects_a_symlinked_source(tmp_path: Path) -> None:
    package = tmp_path / "linked_workflow"
    package.mkdir()
    (package / "runner.py").write_text(
        "from agenthicc.workflows.plugin import WorkflowPlugin\n", encoding="utf-8"
    )
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    try:
        os.symlink(outside, package / "helper.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    report = validate_workflow_file(str(package), root=tmp_path)

    assert not report.ok
    assert report.categories["manifest"] == "fail"
    assert any("symlink" in error for error in report.errors)


def test_required_unavailable_optional_integration_is_actionable(tmp_path: Path) -> None:
    source = _RUNNER_EXAMPLE.replace(
        "    required_integrations: tuple[str, ...] = ()",
        '    required_integrations: tuple[str, ...] = ("cloakbrowser",)',
        1,
    )
    path = tmp_path / "release_check"
    path.mkdir()
    (path / "runner.py").write_text(source, encoding="utf-8")

    report = validate_workflow_file(
        str(path),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
        available_integrations={
            "browser": {
                "selected": True,
                "selected_backend": "cloakbrowser",
                "optional_dependency": "cloakbrowser",
                "dependency_status": "binary_missing",
            }
        },
    )

    assert not report.ok
    assert report.categories["optional_dependencies"] == "fail"
    assert any("uv sync --extra cloakbrowser" in error for error in report.errors)
    assert "binary_missing" in str(report.evidence["optional_integrations"])


def test_required_unavailable_optional_integration_can_use_declared_fallback(
    tmp_path: Path,
) -> None:
    source = _RUNNER_EXAMPLE.replace(
        "    required_integrations: tuple[str, ...] = ()",
        '    required_integrations: tuple[str, ...] = ("playwright",)',
        1,
    ).replace(
        "    integration_fallbacks: dict[str, str] = {}",
        '    integration_fallbacks: dict[str, str] = {"playwright": "text-only report"}',
        1,
    )
    path = tmp_path / "release_check"
    path.mkdir()
    (path / "runner.py").write_text(source, encoding="utf-8")

    report = validate_workflow_file(
        str(path),
        expected_name="release_check",
        root=tmp_path,
        strict_cache_contract=True,
        available_integrations={
            "browser": {
                "selected": True,
                "selected_backend": "playwright",
                "optional_dependency": "playwright",
                "dependency_status": "not_configured",
            }
        },
    )

    assert report.ok, report.render()
    assert report.categories["optional_dependencies"] == "degraded"
    assert any("degraded path" in warning for warning in report.warnings)
