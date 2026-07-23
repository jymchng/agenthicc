"""Command execution tools: bash, python, run_command, run_tests (PRD-16)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from collections.abc import Mapping

from agenthicc.tools.base import Tool, arg_float, arg_str

__all__ = ["ExecToolKit"]

_MAX_OUTPUT_BYTES = 64 * 1024


def _arg_env(args: Mapping[str, object]) -> dict[str, str] | None:
    value = args.get("env")
    if value is None:
        return None
    if isinstance(value, Mapping) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        return {key: item for key, item in value.items()}
    raise ValueError("tool argument 'env' must be an object of string values")


def _arg_argv(args: Mapping[str, object]) -> list[str]:
    value = args.get("argv")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("tool argument 'argv' must be a list of strings")


def _result_text(result: Mapping[str, object], key: str) -> str:
    value = result.get(key, "")
    return value if isinstance(value, str) else ""


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
) -> dict[str, object]:
    t0 = time.perf_counter()
    timed_out = False
    effective_env = {**os.environ, **(env or {})}
    try:
        if shell:
            proc = await asyncio.create_subprocess_shell(
                cmd[0],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=effective_env,
                start_new_session=True,
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
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            stdout_b, stderr_b = b"", b"[process killed: timeout]\n"
            await asyncio.gather(proc.wait(), return_exceptions=True)
    except FileNotFoundError as exc:
        return {
            "stdout": "",
            "stderr": str(exc),
            "returncode": -1,
            "duration_ms": 0.0,
            "timed_out": False,
        }

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
    description = "Run a shell command and return its stdout/stderr."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "default": 30.0},
            "env": {"type": "object"},
        },
        "required": ["command"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        command = arg_str(args, "command")
        cwd = arg_str(args, "cwd", arg_str(context, "workspace_root", "."))
        return await _run_proc(
            [command],
            cwd=cwd,
            timeout=arg_float(args, "timeout", 30.0),
            env=_arg_env(args),
            shell=True,
        )


class RunCommandTool(Tool):
    name = "run_command"
    description = "Run an executable directly (no shell) and return stdout/stderr."
    parameters = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "default": 30.0},
            "env": {"type": "object"},
        },
        "required": ["argv"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        cwd = arg_str(args, "cwd", arg_str(context, "workspace_root", "."))
        return await _run_proc(
            _arg_argv(args),
            cwd=cwd,
            timeout=arg_float(args, "timeout", 30.0),
            env=_arg_env(args),
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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        cwd = arg_str(context, "workspace_root", ".")
        code = arg_str(args, "code")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp_path = f.name
        try:
            return await _run_proc(
                [sys.executable, tmp_path],
                cwd=cwd,
                timeout=arg_float(args, "timeout", 30.0),
                shell=False,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        cwd = arg_str(context, "workspace_root", ".")
        expression = arg_str(args, "expression")
        code = f"_r = ({expression}); print(repr(_r))"
        result = await _run_proc(
            [sys.executable, "-c", code],
            cwd=cwd,
            timeout=arg_float(args, "timeout", 10.0),
            shell=False,
        )
        result["result"] = _result_text(result, "stdout").strip()
        return result


class RunTestsTool(Tool):
    name = "run_tests"
    description = "Run the test suite and return pass/fail counts."
    parameters = {
        "type": "object",
        "properties": {
            "framework": {"type": "string", "default": "pytest"},
            "path": {"type": "string", "default": "tests/"},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "number", "default": 120.0},
        },
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        import re
        import uuid  # noqa: PLC0415

        cwd = arg_str(context, "workspace_root", ".")
        raw_extra = args.get("args") or []
        if not isinstance(raw_extra, list) or not all(isinstance(item, str) for item in raw_extra):
            raise ValueError("tool argument 'args' must be a list of strings")
        extra = list(raw_extra)
        path = arg_str(args, "path", "tests/")
        report_path = f"/tmp/pytest_report_{uuid.uuid4().hex}.json"

        if arg_str(args, "framework", "pytest") == "pytest":
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                path,
                "--json-report",
                f"--json-report-file={report_path}",
                "-q",
                *extra,
            ]
        else:
            cmd = [sys.executable, "-m", "unittest", "discover", path, *extra]

        result = await _run_proc(
            cmd,
            cwd=cwd,
            timeout=arg_float(args, "timeout", 120.0),
            shell=False,
        )

        passed = failed = errors = None
        try:
            import json  # noqa: PLC0415

            with open(report_path) as f:
                report = json.load(f)
            summary = report.get("summary", {})
            passed = summary.get("passed", 0)
            failed = summary.get("failed", 0)
            errors = summary.get("error", 0)
            os.unlink(report_path)
        except Exception:
            stdout = _result_text(result, "stdout")
            m = re.search(r"(\d+) passed", stdout)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", stdout)
            if m:
                failed = int(m.group(1))

        result.update({"passed": passed, "failed": failed, "errors": errors})
        return result


class ExecToolKit:
    def tools(self) -> list[Tool]:
        return [
            RunBashTool(),
            RunCommandTool(),
            RunPythonTool(),
            RunPythonExprTool(),
            RunTestsTool(),
        ]
