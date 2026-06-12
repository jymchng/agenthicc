---
title: "PRD-16: Command Execution Tools — bash, Python, and Test Runner"
status: draft
version: 0.1.0
created: 2025-01-01
---

# PRD-16: Command Execution Tools

## Executive Summary

Agents performing autonomous software engineering tasks need to run shell commands, execute Python scripts, and invoke test suites. `run_bash`, `run_python`, `run_command`, `run_tests`, and `run_python_expr` form the execution layer. All tools use `asyncio.create_subprocess_exec` or `asyncio.create_subprocess_shell` for non-blocking I/O, capture stdout and stderr, enforce timeouts via `asyncio.wait_for`, kill process groups on timeout, and cap output at 64 KB. Write operations require `exec:allow` permission; `run_bash` and `run_command` are the most powerful and default to `require_confirmation` in production environments.

---

## Goals

| ID | Goal |
|----|------|
| G1 | `run_bash`, `run_python`, `run_command`, `run_tests`, `run_python_expr` available as Tool subclasses |
| G2 | All tools use async subprocess — never block the event loop |
| G3 | Timeout kills the entire process group (not just the parent process) |
| G4 | stdout/stderr capped at `max_output_kb` with `[... truncated]` suffix |
| G5 | `run_tests` parses pytest JSON report for structured pass/fail counts |
| G6 | `run_bash`/`run_command` require `exec:allow` permission |
| G7 | `cwd` defaults to `context["workspace_root"]` |

## Non-Goals
- Interactive / TTY mode (all tools run non-interactively)
- Streaming stdout (captured and returned as complete string)
- Remote execution (local processes only)

---

## Tool Catalog

| Tool | Parameters | Returns |
|------|-----------|---------|
| `run_bash` | `command: str, cwd?, timeout=30, env?` | `{stdout, stderr, returncode, duration_ms, timed_out}` |
| `run_command` | `argv: list[str], cwd?, timeout=30, env?` | `{stdout, stderr, returncode, duration_ms, timed_out}` |
| `run_python` | `code: str, timeout=30` | `{stdout, stderr, returncode, duration_ms}` |
| `run_python_expr` | `expression: str, timeout=10` | `{result, stdout, returncode}` |
| `run_tests` | `framework="pytest", path="tests/", args?, timeout=120` | `{stdout, stderr, returncode, passed?, failed?, errors?, duration_ms}` |

---

## Data Structures and Interfaces

```python
# src/agenthicc/tools/exec/__init__.py
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from typing import Any

from agenthicc.tools.base import Tool

__all__ = ["ExecToolKit"]

_MAX_OUTPUT_BYTES = 64 * 1024


def _truncate(text: str, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[... truncated]"


async def _run_proc(
    cmd: list[str],
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> dict[str, Any]:
    """Run a subprocess; return structured result dict."""
    t0 = time.perf_counter()
    timed_out = False
    effective_env = {**os.environ, **(env or {})}

    try:
        if shell:
            assert len(cmd) == 1
            proc = await asyncio.create_subprocess_shell(
                cmd[0],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=effective_env,
                start_new_session=True,   # process group for kill-on-timeout
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=effective_env,
                start_new_session=True,
            )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            # Kill the entire process group
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.communicate()
            stdout_b, stderr_b = b"", b"[process killed: timeout]\n".encode()
    except FileNotFoundError as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": -1,
                "duration_ms": 0.0, "timed_out": False}

    duration_ms = (time.perf_counter() - t0) * 1000
    return {
        "stdout": _truncate(stdout_b.decode(errors="replace")),
        "stderr": _truncate(stderr_b.decode(errors="replace")),
        "returncode": proc.returncode if not timed_out else -1,
        "duration_ms": round(duration_ms, 1),
        "timed_out": timed_out,
    }


class RunBashTool(Tool):
    name = "run_bash"
    description = "Run a bash shell command and return its output."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "cwd": {"type": "string", "description": "Working directory (default: workspace root)"},
            "timeout": {"type": "number", "default": 30.0},
            "env": {"type": "object", "description": "Extra environment variables"},
        },
        "required": ["command"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        cwd = args.get("cwd") or context.get("workspace_root", ".")
        return await _run_proc(
            [args["command"]],
            cwd=cwd,
            timeout=float(args.get("timeout", 30)),
            env=args.get("env"),
            shell=True,
        )


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run an executable directly (no shell) and return its output."
    parameters = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "Command and arguments as a list"},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "default": 30.0},
            "env": {"type": "object"},
        },
        "required": ["argv"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        cwd = args.get("cwd") or context.get("workspace_root", ".")
        return await _run_proc(
            list(args["argv"]),
            cwd=cwd,
            timeout=float(args.get("timeout", 30)),
            env=args.get("env"),
            shell=False,
        )


class RunPythonTool(Tool):
    name = "run_python"
    description = "Execute a Python code snippet in a subprocess."
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "number", "default": 30.0},
        },
        "required": ["code"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        cwd = context.get("workspace_root", ".")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(args["code"])
            tmp_path = f.name
        try:
            result = await _run_proc(
                [sys.executable, tmp_path],
                cwd=cwd,
                timeout=float(args.get("timeout", 30)),
                shell=False,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return result


class RunPythonExprTool(Tool):
    name = "run_python_expr"
    description = "Evaluate a Python expression and return its repr."
    parameters = {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "timeout": {"type": "number", "default": 10.0},
        },
        "required": ["expression"],
    }

    async def execute(self, args: dict, context: dict) -> Any:
        code = f"import sys; _r = ({args['expression']}); print(repr(_r))"
        cwd = context.get("workspace_root", ".")
        result = await _run_proc(
            [sys.executable, "-c", code],
            cwd=cwd,
            timeout=float(args.get("timeout", 10)),
            shell=False,
        )
        result["result"] = result["stdout"].strip()
        return result


class RunTestsTool(Tool):
    name = "run_tests"
    description = "Run the project test suite and return pass/fail counts."
    parameters = {
        "type": "object",
        "properties": {
            "framework": {"type": "string", "enum": ["pytest", "unittest"], "default": "pytest"},
            "path": {"type": "string", "default": "tests/"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Extra CLI arguments passed to the test runner"},
            "timeout": {"type": "number", "default": 120.0},
        },
    }

    async def execute(self, args: dict, context: dict) -> Any:
        cwd = context.get("workspace_root", ".")
        framework = args.get("framework", "pytest")
        path = args.get("path", "tests/")
        extra_args = list(args.get("args") or [])

        if framework == "pytest":
            import uuid
            report_path = f"/tmp/pytest_report_{uuid.uuid4().hex}.json"
            cmd = [sys.executable, "-m", "pytest", path,
                   f"--json-report", f"--json-report-file={report_path}",
                   "-q", *extra_args]
            result = await _run_proc(cmd, cwd=cwd,
                                     timeout=float(args.get("timeout", 120)),
                                     shell=False)
            # Parse JSON report if available
            passed = failed = errors = None
            try:
                import json
                with open(report_path) as f:
                    report = json.load(f)
                summary = report.get("summary", {})
                passed = summary.get("passed", 0)
                failed = summary.get("failed", 0)
                errors = summary.get("error", 0)
                os.unlink(report_path)
            except Exception:
                # Fall back to regex parse
                import re
                m = re.search(r"(\d+) passed", result["stdout"])
                if m:
                    passed = int(m.group(1))
                m = re.search(r"(\d+) failed", result["stdout"])
                if m:
                    failed = int(m.group(1))
            result.update({"passed": passed, "failed": failed, "errors": errors})
            return result
        else:
            cmd = [sys.executable, "-m", "unittest", "discover", path, *extra_args]
            return await _run_proc(cmd, cwd=cwd,
                                   timeout=float(args.get("timeout", 120)),
                                   shell=False)


class ExecToolKit:
    """Factory returning all exec tools."""

    def tools(self) -> list[Tool]:
        return [
            RunBashTool(),
            RunCommandTool(),
            RunPythonTool(),
            RunPythonExprTool(),
            RunTestsTool(),
        ]
```

---

## Configuration Reference

```toml
[tools.exec]
allow_bash = true
allow_python = true
allow_run_command = true
max_output_kb = 64
default_timeout_seconds = 30
cwd = "."
env_passthrough = ["PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV"]
```

Security rules generated from config:
```toml
[[security.permission_rules]]
tool_pattern = "run_bash"
action = "require_confirmation"   # override with allow_bash = true

[[security.permission_rules]]
tool_pattern = "run_tests"
action = "allow"                   # always safe
```

---

## Tests

```python
# tests/unit/test_exec_tools.py
"""Unit tests for exec tools (PRD-16)."""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agenthicc.tools.exec import RunBashTool, RunCommandTool, RunPythonTool, RunTestsTool, ExecToolKit

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_proc():
    with patch("agenthicc.tools.exec._run_proc") as m:
        yield m


class TestRunBashTool:
    async def test_success(self, mock_proc):
        mock_proc.return_value = {"stdout": "hello\n", "stderr": "", "returncode": 0,
                                  "duration_ms": 5.0, "timed_out": False}
        result = await RunBashTool().execute({"command": "echo hello"}, {})
        assert result["stdout"] == "hello\n"
        assert result["returncode"] == 0

    async def test_timeout_flag_in_result(self, mock_proc):
        mock_proc.return_value = {"stdout": "", "stderr": "killed", "returncode": -1,
                                  "duration_ms": 30000.0, "timed_out": True}
        result = await RunBashTool().execute({"command": "sleep 100", "timeout": 1}, {})
        assert result["timed_out"] is True

    async def test_nonzero_returncode(self, mock_proc):
        mock_proc.return_value = {"stdout": "", "stderr": "not found",
                                  "returncode": 127, "duration_ms": 1.0, "timed_out": False}
        result = await RunBashTool().execute({"command": "nonexistent_cmd"}, {})
        assert result["returncode"] == 127

    async def test_cwd_from_context(self, mock_proc):
        mock_proc.return_value = {"stdout": "", "stderr": "", "returncode": 0,
                                  "duration_ms": 1.0, "timed_out": False}
        await RunBashTool().execute({"command": "ls"}, {"workspace_root": "/my/project"})
        call_kwargs = mock_proc.call_args[1]
        assert call_kwargs["cwd"] == "/my/project"


class TestRunTestsTool:
    async def test_parses_passed_count_from_stdout(self, mock_proc):
        mock_proc.return_value = {
            "stdout": "10 passed, 2 failed in 1.5s\n",
            "stderr": "", "returncode": 1, "duration_ms": 1500.0, "timed_out": False,
        }
        # Patch json report to not exist
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = await RunTestsTool().execute({"framework": "pytest"}, {})
        assert result["passed"] == 10
        assert result["failed"] == 2


class TestExecToolKit:
    def test_returns_5_tools(self):
        tools = ExecToolKit().tools()
        assert len(tools) == 5
        names = {t.name for t in tools}
        assert "run_bash" in names
        assert "run_tests" in names


class TestTruncation:
    def test_long_output_truncated(self):
        from agenthicc.tools.exec import _truncate
        big = "x" * 200_000
        result = _truncate(big, max_bytes=1024)
        assert result.endswith("[... truncated]")
        assert len(result.encode()) <= 1024 + 50  # small slack for suffix

    def test_short_output_not_truncated(self):
        from agenthicc.tools.exec import _truncate
        short = "hello world"
        assert _truncate(short) == short
```

---

## Open Questions

1. **`run_bash` on Windows**: `asyncio.create_subprocess_shell` on Windows uses `cmd.exe` not bash. Add a `shell_executable` config option and detect platform. `run_command` is cross-platform.
2. **Process groups on Windows**: `os.killpg` is POSIX-only. On Windows, use `proc.kill()` and accept that child processes may linger.
3. **pytest-json-report dependency**: `run_tests` uses it for structured output. Add as optional dependency: `[tools.exec] install_json_reporter = true` triggers `uv pip install pytest-json-report` on first use.
