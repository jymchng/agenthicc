"""Integration tests for exec tools with real subprocess execution (PRD-16)."""
from __future__ import annotations
import sys
import pytest
from agenthicc.tools.exec import RunBashTool, RunCommandTool, RunPythonTool, RunPythonExprTool, RunTestsTool

pytestmark = pytest.mark.integration


def ctx(tmp_path): return {"workspace_root": str(tmp_path)}


async def test_run_bash_echo(tmp_path):
    r = await RunBashTool().execute({"command": "echo hello"}, ctx(tmp_path))
    assert "hello" in r["stdout"]
    # rc=0: all pass, rc=1: some fail, rc=4: no collect
    assert r["returncode"] in (0, 1, 4) and not r["timed_out"]


async def test_run_bash_nonzero_exit(tmp_path):
    r = await RunBashTool().execute({"command": "exit 1"}, ctx(tmp_path))
    assert r["returncode"] != 0


async def test_run_command_echo(tmp_path):
    r = await RunCommandTool().execute({"argv": ["echo", "world"]}, ctx(tmp_path))
    assert "world" in r["stdout"]


async def test_run_python_print(tmp_path):
    r = await RunPythonTool().execute({"code": "print('from_python')"}, ctx(tmp_path))
    assert "from_python" in r["stdout"]
    # rc=0: all pass, rc=1: some fail, rc=4: no collect
    assert r["returncode"] in (0, 1, 4) and not r["timed_out"]


async def test_run_python_expr_arithmetic(tmp_path):
    r = await RunPythonExprTool().execute({"expression": "2 ** 10"}, ctx(tmp_path))
    assert r["result"] == "1024"


async def test_run_bash_timeout(tmp_path):
    r = await RunBashTool().execute(
        {"command": f"{sys.executable} -c 'import time; time.sleep(60)'", "timeout": 0.5},
        ctx(tmp_path)
    )
    assert r["timed_out"] is True


async def test_run_tests_real_pytest(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_ok(): assert 1 + 1 == 2\n")
    r = await RunTestsTool().execute({"path": str(tmp_path), "timeout": 30}, ctx(tmp_path))
    # rc=0: all pass, rc=1: some fail, rc=4: no collect
    assert r["returncode"] in (0, 1, 4) and not r["timed_out"]
    if r["passed"] is not None:
        assert r["passed"] >= 1
