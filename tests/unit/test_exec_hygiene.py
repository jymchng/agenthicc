"""TOOLR-02: child processes are isolated, bounded and use the project's Python."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenthicc.config import ExecSettings
from agenthicc.tools.exec import RunBashTool, RunCommandTool, RunPythonTool, RunTestsTool
from agenthicc.tools.exec import child as child_module
from agenthicc.tools.exec.child import child_env, configure, project_python

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def default_exec_settings() -> Iterator[None]:
    yield
    configure(ExecSettings())


def _ctx(root: Path) -> dict[str, object]:
    return {"workspace_root": str(root)}


async def test_a_child_never_reads_the_parents_stdin(tmp_path: Path) -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"PARENT-STDIN-DATA\n")
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result = await RunBashTool().execute(
            {"command": "head -c 40", "timeout": 5}, _ctx(tmp_path)
        )
    finally:
        os.dup2(saved, 0)
        for descriptor in (saved, read_end, write_end):
            os.close(descriptor)

    assert result["state"] == "exited"
    assert "PARENT-STDIN-DATA" not in str(result["stdout"])


async def test_huge_output_keeps_head_tail_and_true_size(tmp_path: Path) -> None:
    size = 20_000_000
    result = await RunBashTool().execute(
        {"command": f"yes | head -c {size}; echo LAST-LINE-ERROR >&2", "timeout": 60},
        _ctx(tmp_path),
    )

    assert result["stdout_bytes"] == size and result["truncated"] is True
    assert len(str(result["stdout"])) < 70_000
    assert "bytes omitted" in str(result["stdout"])
    assert "LAST-LINE-ERROR" in str(result["stderr"])


async def test_a_runaway_writer_is_stopped_at_the_output_limit(tmp_path: Path) -> None:
    configure(ExecSettings(max_output_bytes=1_000_000))

    result = await RunBashTool().execute({"command": "yes", "timeout": 60}, _ctx(tmp_path))

    assert result["termination_reason"] == "output_limit"
    assert result["state"] == "failed" and result["timed_out"] is False
    assert "output exceeded 1000000 bytes" in str(result["stderr"])


async def test_the_tail_of_a_failure_is_in_the_result(tmp_path: Path) -> None:
    result = await RunBashTool().execute(
        {
            "command": "head -c 1000000 /dev/zero | tr '\\0' x; echo; echo FINAL-ERROR",
            "timeout": 30,
        },
        _ctx(tmp_path),
    )

    assert str(result["stdout"]).rstrip().endswith("FINAL-ERROR")


async def test_credentials_are_scrubbed_unless_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-value")
    monkeypatch.setenv("ORDINARY_SETTING", "kept")

    scrubbed = await RunCommandTool().execute({"argv": ["env"], "timeout": 10}, _ctx(tmp_path))
    names = {line.split("=", 1)[0] for line in str(scrubbed["stdout"]).splitlines()}
    assert "ANTHROPIC_API_KEY" not in names and "GITHUB_TOKEN" not in names
    assert "ORDINARY_SETTING" in names and "GIT_TERMINAL_PROMPT" in names

    configure(ExecSettings(env_passthrough=["GITHUB_TOKEN"]))
    allowed = await RunCommandTool().execute({"argv": ["env"], "timeout": 10}, _ctx(tmp_path))
    assert "GITHUB_TOKEN=" in str(allowed["stdout"])


def test_an_explicit_overlay_supplies_its_own_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "parent")
    env = child_env({"SERVICE_TOKEN": "explicit"})
    assert env["SERVICE_TOKEN"] == "explicit"
    assert "SERVICE_TOKEN" not in child_env()


@pytest.mark.skipif(os.name != "posix", reason="RLIMIT_AS is POSIX-only")
async def test_a_memory_limit_stops_an_oversized_allocation(tmp_path: Path) -> None:
    configure(ExecSettings(max_memory_mb=256))

    result = await RunCommandTool().execute(
        {"argv": [sys.executable, "-c", "x = bytearray(1024 * 1024 * 1024)"], "timeout": 30},
        _ctx(tmp_path),
    )

    assert result["state"] == "failed"
    assert "MemoryError" in str(result["stderr"])


def test_the_projects_virtualenv_is_preferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n")
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)

    found = project_python(str(nested), str(tmp_path))

    assert found.argv == (str(interpreter),) and found.source == ".venv"


def test_a_venv_outside_the_workspace_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    workspace = tmp_path / "project"
    workspace.mkdir()

    assert project_python(str(workspace), str(workspace)).source == "agenthicc"


def test_a_configured_interpreter_wins(tmp_path: Path) -> None:
    configure(ExecSettings(python="/opt/py/bin/python -X utf8"))
    found = project_python(str(tmp_path), str(tmp_path))
    assert found.argv == ("/opt/py/bin/python", "-X", "utf8")
    assert child_module.exec_settings().python


async def test_run_python_reports_the_interpreter_it_used(tmp_path: Path) -> None:
    result = await RunPythonTool().execute({"code": "print(6 * 7)"}, _ctx(tmp_path))
    assert str(result["stdout"]).strip() == "42"
    assert result["interpreter_source"] in {"agenthicc", "virtual_env", ".venv", "uv"}


async def test_run_tests_reports_counts_without_any_pytest_plugin(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n"
    )

    result = await RunTestsTool().execute({"path": "tests/", "timeout": 120}, _ctx(tmp_path))

    assert result["passed"] == 1 and result["failed"] == 1, result
    assert result["failures"][0]["test"].endswith("test_bad")


async def test_extra_args_cannot_redirect_the_report(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_ok():\n    assert True\n")

    result = await RunTestsTool().execute(
        {"path": "tests/", "args": ["--junitxml=/tmp/elsewhere.xml"], "timeout": 120},
        _ctx(tmp_path),
    )

    assert result["passed"] == 1
    assert "/tmp/elsewhere.xml" not in " ".join(str(item) for item in result.get("argv") or ())


async def test_unittest_counts_are_parsed(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        pass\n\n    def test_bad(self):\n        self.fail()\n"
    )

    result = await RunTestsTool().execute(
        {"path": "tests/", "framework": "unittest", "timeout": 120}, _ctx(tmp_path)
    )

    assert result["passed"] == 1 and result["failed"] == 1, result
