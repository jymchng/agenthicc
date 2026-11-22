"""Guard the production validation boundary with an executable package."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from agenthicc.workflows.authoring.import_sandbox import inspect_generated_python
from agenthicc.workflows.create_workflow.validation import _inspect_workflow, validate_workflow_file

pytestmark = pytest.mark.integration
_needs_sandbox = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="Bubblewrap is required for the executable inspection",
)


def _package(workspace: Path, source: str) -> Path:
    package = workspace / "isolated_demo"
    package.mkdir()
    (package / "runner.py").write_text(
        source
        + """
from agenthicc.workflows.plugin import PhaseSpec, WorkflowPlugin

class IsolatedWorkflow(WorkflowPlugin):
    name = "isolated_demo"
    description = "Check the isolated authoring runtime."
    phases = [PhaseSpec(name="check")]

    @classmethod
    def build_params(cls, config):
        from .helpers import exercised
        assert exercised()
        return super().build_params(config)
""",
        encoding="utf-8",
    )
    (package / "helpers.py").write_text("def exercised():\n    return True\n", encoding="utf-8")
    return package


@_needs_sandbox
def test_validation_executes_only_inside_the_read_only_network_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTHORED_INSPECTION_TEST_SECRET", "fixture-only")
    sandbox_network = str(Path("/proc/self/ns/net").readlink())
    package = _package(
        tmp_path,
        f"""
import errno
import os
import socket
from pathlib import Path

assert "AUTHORED_INSPECTION_TEST_SECRET" not in os.environ
assert [name for _, name in socket.if_nameindex()] == ["lo"]
assert str(Path("/proc/self/ns/net").readlink()) != {sandbox_network!r}

try:
    Path("would-run-in-parent").write_text("bad")
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError("workspace was writable")
""",
    )
    modules = set(sys.modules)
    report = validate_workflow_file(str(package), expected_name="isolated_demo", root=tmp_path)

    assert report.ok, report.errors
    assert report.validation == "passed"
    assert report.phase_names == ("check",)
    assert not (tmp_path / "would-run-in-parent").exists()
    assert all("isolated_demo" not in name for name in set(sys.modules) - modules)


def test_without_bubblewrap_validation_is_static_and_does_not_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    package = _package(
        tmp_path,
        'raise RuntimeError("generated code must not execute without isolation")\n',
    )

    report = validate_workflow_file(str(package), expected_name="isolated_demo", root=tmp_path)

    assert report.ok, report.errors
    assert report.validation == "static_only"
    assert report.plugin_names == ("isolated_demo",)
    assert report.skipped_checks


@_needs_sandbox
@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("import time\ntime.sleep(60)\n", "timed out"),
        ('while True: print("x" * 16384)\n', "without a result"),
    ],
)
def test_executable_inspection_bounds_run_time_and_output(
    tmp_path: Path, source: str, message: str
) -> None:
    package = _package(tmp_path, source)
    check = inspect_generated_python(
        tmp_path,
        _inspect_workflow,
        {
            "path": f"/workspace/{package.name}",
            "expected_name": package.name,
            "strict_cache_contract": False,
        },
        timeout=4,
    )

    assert check.status == "failed"
    assert check.result is None
    assert message in check.reason
