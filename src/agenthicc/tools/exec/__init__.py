"""Command execution tools: bash, python, run_command, run_tests (PRD-16)."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import os
import shutil
import signal
import sys
import tempfile
import time
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.tools.base import Tool, arg_bool, arg_str
from agenthicc.tools.exec.outcome import (
    CommandKind,
    CommandOutcome,
    CommandState,
    invalid_timeout_result,
    resolve_deadline,
    validate_timeout,
)


# Direct callers historically receive a structured ``cancelled`` outcome when
# they cancel a foreground command.  Interactive agent turns need a different
# contract: after the subprocess is cleaned up, cancellation must continue up
# through the tool executor so the LLM turn stops instead of receiving a normal
# tool result and continuing.  AgentTurnRunner enables this scope only around
# its provider/tool loop.
_PROPAGATE_TOOL_CANCELLATION: ContextVar[bool] = ContextVar(
    "agenthicc_propagate_tool_cancellation", default=False
)

if TYPE_CHECKING:
    from agenthicc.background.terminals import TerminalManager

__all__ = [
    "ExecToolKit",
    "RunBashTool",
    "RunCommandTool",
    "RunPythonTool",
    "RunPythonExprTool",
    "RunTestsTool",
    "WaitTerminalTool",
    "InspectTerminalTool",
    "WaitTerminalReadinessTool",
    "StopTerminalTool",
    "CommandKind",
    "CommandOutcome",
    "CommandState",
]

_MAX_OUTPUT_BYTES = 64 * 1024
_SECRET_FLAGS = re.compile(
    r"(?i)(--?(?:password|passwd|token|secret|api[-_]?key|authorization))\s*(?:=|\s)\s*([^\s]+)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|authorization))\s*=\s*([^\s;&]+)"
)


def _redact_execution_text(value: str) -> str:
    value = _SECRET_FLAGS.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", value)


def _spawn_failure_kind(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "executable_or_shell"
    if isinstance(exc, NotADirectoryError):
        return "cwd"
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, ValueError):
        return "environment"
    return "spawn"


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
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("tool argument 'argv' must be a non-empty list of strings")


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
    effective_env = {**os.environ, **(env or {})}
    kind = CommandKind.SHELL if shell else CommandKind.EXEC
    deadline = resolve_deadline(timeout)
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None

    if not os.path.isdir(cwd):
        return CommandOutcome(
            state=CommandState.SPAWN_FAILED,
            command_kind=kind,
            stderr=f"cwd is not a directory: {cwd}",
            duration_ms=0.0,
            termination_reason="spawn failed: invalid cwd",
            deadline=deadline,
            cleanup_result="not_spawned",
            spawn_failure="cwd",
            command=_redact_execution_text(cmd[0]) if shell else None,
            argv=None if shell else tuple(_redact_execution_text(item) for item in cmd),
            cwd=cwd,
        ).to_dict()

    async def cleanup(*, reason: str) -> str:
        if process is None or process.returncode is not None:
            return "already_exited"
        try:
            if os.name == "nt":
                process.send_signal(1)
            else:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGINT)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=0.5)
            return f"graceful_{reason}"
        except asyncio.TimeoutError:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    return "cleanup_unproven"
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=1.0)
                return f"force_{reason}"
            except asyncio.TimeoutError:
                return "cleanup_unproven"

    async def output_after_cleanup() -> tuple[bytes, bytes]:
        if communication is None:
            return b"", b""
        try:
            return await asyncio.wait_for(asyncio.shield(communication), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return b"", b"[process output could not be fully drained]\n"

    try:
        if shell:
            proc = await asyncio.create_subprocess_shell(
                cmd[0],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=effective_env,
                start_new_session=True,
                executable=(
                    "/bin/bash"
                    if os.name != "nt" and os.path.exists("/bin/bash")
                    else shutil.which("bash")
                ),
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
        process = proc
        communication = asyncio.create_task(process.communicate(), name="command-output")
        cleanup_result = "not_required"
        state = CommandState.EXITED
        termination_reason: str | None = None
        cancelled = False
        try:
            if deadline.effective_s is None:
                stdout_b, stderr_b = await asyncio.shield(communication)
            else:
                stdout_b, stderr_b = await asyncio.wait_for(
                    asyncio.shield(communication), timeout=deadline.effective_s
                )
        except asyncio.TimeoutError:
            state = CommandState.TIMED_OUT
            termination_reason = "command deadline expired"
            cleanup_result = await cleanup(reason="timeout")
            stdout_b, stderr_b = await output_after_cleanup()
            stderr_b += b"[process stopped: command timeout]\n"
        except asyncio.CancelledError:
            state = CommandState.CANCELLED
            cancelled = True
            termination_reason = "owning task cancelled"
            cleanup_task = asyncio.create_task(cleanup(reason="cancellation"))
            cleanup_result = await asyncio.shield(cleanup_task)
            stdout_b, stderr_b = await output_after_cleanup()
            stderr_b += b"[process stopped: cancellation]\n"
            if _PROPAGATE_TOOL_CANCELLATION.get():
                raise
        else:
            stdout_b, stderr_b = stdout_b, stderr_b
        if state is CommandState.EXITED and process.returncode != 0:
            state = CommandState.FAILED
            termination_reason = f"process exited with return code {process.returncode}"
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        outcome = CommandOutcome(
            state=CommandState.SPAWN_FAILED,
            command_kind=kind,
            stderr=str(exc),
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
            termination_reason=f"spawn failed: {type(exc).__name__}",
            spawn_failure=_spawn_failure_kind(exc),
            deadline=deadline,
            cleanup_result="not_spawned",
            command=_redact_execution_text(cmd[0]) if shell else None,
            argv=None if shell else tuple(_redact_execution_text(item) for item in cmd),
            cwd=cwd,
        )
        return outcome.to_dict()

    stdout = _truncate(_redact_execution_text(stdout_b.decode(errors="replace")))
    stderr = _truncate(_redact_execution_text(stderr_b.decode(errors="replace")))
    outcome = CommandOutcome(
        state=state,
        command_kind=kind,
        returncode=process.returncode if process is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        timed_out=state is CommandState.TIMED_OUT,
        cancelled=cancelled,
        truncated=("[... truncated]" in stdout or "[... truncated]" in stderr),
        termination_reason=termination_reason,
        deadline=deadline,
        cleanup_result=cleanup_result,
        command=_redact_execution_text(cmd[0]) if shell else None,
        argv=None if shell else tuple(_redact_execution_text(item) for item in cmd),
        cwd=cwd,
    )
    return outcome.to_dict()


def _timeout(
    args: Mapping[str, object], default: float, kind: CommandKind
) -> tuple[float | None, dict[str, object] | None]:
    raw = args.get("timeout", default)
    try:
        return validate_timeout(raw), None
    except ValueError:
        return None, invalid_timeout_result(raw, command_kind=kind)


def _cwd(args: Mapping[str, object], context: Mapping[str, object]) -> str:
    requested = args.get("cwd")
    root = context.get("workspace_root", ".")
    base = (Path(root) if isinstance(root, str) else Path(".")).resolve()
    if isinstance(requested, str) and requested:
        path = Path(requested)
        return str(path if path.is_absolute() else base / path)
    return str(base)


def _lifecycle(args: Mapping[str, object]) -> str:
    lifecycle = args.get("lifecycle", "oneshot")
    if not isinstance(lifecycle, str) or lifecycle not in {"oneshot", "service"}:
        raise ValueError("lifecycle must be 'oneshot' or 'service'")
    return lifecycle


def _readiness(args: Mapping[str, object]) -> dict[str, object] | None:
    value = args.get("readiness")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("readiness must be an object")
    return {str(key): item for key, item in value.items()}


class RunBashTool(Tool):
    name = "run_bash"
    description = "Run a Bash command with an authoritative outcome."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "number", "default": 30.0},
            "env": {"type": "object"},
            "background": {"type": "boolean", "default": False},
            "label": {"type": "string"},
            "lifecycle": {"type": "string", "enum": ["oneshot", "service"]},
            "readiness": {"type": "object"},
        },
        "required": ["command"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        command = arg_str(args, "command")
        cwd = _cwd(args, context)
        timeout, invalid = _timeout(args, 30.0, CommandKind.SHELL)
        if invalid is not None:
            return invalid
        try:
            env_overlay = _arg_env(args)
        except ValueError as exc:
            return {
                **invalid_timeout_result(0, command_kind=CommandKind.SHELL),
                "termination_reason": str(exc),
                "error": str(exc),
            }
        try:
            lifecycle = _lifecycle(args)
            readiness = _readiness(args)
        except ValueError as exc:
            return {
                **invalid_timeout_result(0, command_kind=CommandKind.SHELL),
                "termination_reason": str(exc),
                "error": str(exc),
            }
        policy = context.get("terminal_wait_policy", "foreground")
        background_requested = (
            arg_bool(args, "background", False) or policy == "background" or lifecycle == "service"
        )
        if lifecycle == "service" and not background_requested:
            return {
                "ok": False,
                "state": "rejected",
                "command_kind": CommandKind.SHELL.value,
                "error": "service lifecycle requires background ownership",
            }
        if background_requested:
            manager = cast("TerminalManager | None", context.get("terminal_manager"))
            if manager is None:
                return {
                    "ok": False,
                    "background": True,
                    "state": "rejected",
                    "error": "background terminals are unavailable in this execution context",
                }
            return await manager.start(
                command=command,
                cwd=cwd,
                timeout=timeout or 0.0,
                env=env_overlay,
                shell=True,
                label=arg_str(args, "label", ""),
                tool_call_id=arg_str(context, "tool_call_id", ""),
                lifecycle=lifecycle,
                readiness=readiness,
            )
        return await _run_proc(
            [command],
            cwd=cwd,
            timeout=timeout or 0.0,
            env=env_overlay,
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
            "background": {"type": "boolean", "default": False},
            "label": {"type": "string"},
            "lifecycle": {"type": "string", "enum": ["oneshot", "service"]},
            "readiness": {"type": "object"},
        },
        "required": ["argv"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        cwd = _cwd(args, context)
        timeout, invalid = _timeout(args, 30.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
        try:
            env_overlay = _arg_env(args)
            argv = _arg_argv(args)
        except ValueError as exc:
            return {
                **invalid_timeout_result(0, command_kind=CommandKind.EXEC),
                "termination_reason": str(exc),
                "error": str(exc),
            }
        try:
            lifecycle = _lifecycle(args)
            readiness = _readiness(args)
        except ValueError as exc:
            return {
                **invalid_timeout_result(0, command_kind=CommandKind.EXEC),
                "termination_reason": str(exc),
                "error": str(exc),
            }
        policy = context.get("terminal_wait_policy", "foreground")
        background_requested = (
            arg_bool(args, "background", False) or policy == "background" or lifecycle == "service"
        )
        if background_requested:
            manager = cast("TerminalManager | None", context.get("terminal_manager"))
            if manager is None:
                return {
                    "ok": False,
                    "background": True,
                    "state": "rejected",
                    "error": "background terminals are unavailable in this execution context",
                }
            return await manager.start(
                argv=argv,
                cwd=cwd,
                timeout=timeout or 0.0,
                env=env_overlay,
                shell=False,
                label=arg_str(args, "label", ""),
                tool_call_id=arg_str(context, "tool_call_id", ""),
                lifecycle=lifecycle,
                readiness=readiness,
            )
        return await _run_proc(
            argv,
            cwd=cwd,
            timeout=timeout or 0.0,
            env=env_overlay,
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
        timeout, invalid = _timeout(args, 30.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp_path = f.name
        try:
            return await _run_proc(
                [sys.executable, tmp_path],
                cwd=cwd,
                timeout=timeout or 0.0,
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
        timeout, invalid = _timeout(args, 10.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
        code = f"_r = ({expression}); print(repr(_r))"
        result = await _run_proc(
            [sys.executable, "-c", code],
            cwd=cwd,
            timeout=timeout or 0.0,
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
        timeout, invalid = _timeout(args, 120.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
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
            timeout=timeout or 0.0,
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


class WaitTerminalTool(Tool):
    """Wait for a handle returned by a background execution tool."""

    name = "wait_terminal"
    description = "Wait for an owned background terminal to finish and return its output."
    parameters = {
        "type": "object",
        "properties": {
            "terminal_id": {"type": "string"},
            "timeout": {"type": "number", "default": 0.0},
        },
        "required": ["terminal_id"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        manager = cast("TerminalManager | None", context.get("terminal_manager"))
        if manager is None:
            return {
                "ok": False,
                "background": True,
                "state": "rejected",
                "error": "background terminals are unavailable in this execution context",
            }
        timeout, invalid = _timeout(args, 0.0, CommandKind.EXEC)
        if invalid is not None:
            invalid.update({"background": True, "terminal_id": arg_str(args, "terminal_id")})
            return invalid
        return await manager.wait(
            arg_str(args, "terminal_id"),
            timeout=timeout or 0.0,
        )


class InspectTerminalTool(Tool):
    """Inspect one owned terminal without waiting or stopping it."""

    name = "inspect_terminal"
    description = "Inspect the state and bounded output of an owned terminal."
    parameters = {
        "type": "object",
        "properties": {"terminal_id": {"type": "string"}},
        "required": ["terminal_id"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        manager = cast("TerminalManager | None", context.get("terminal_manager"))
        terminal_id = arg_str(args, "terminal_id")
        if manager is None:
            return {"ok": False, "state": "rejected", "error": "terminal manager unavailable"}
        record = manager.get(terminal_id)
        if record is None:
            return {"ok": False, "state": "unknown", "terminal_id": terminal_id}
        if record.session_id != manager.session_id:
            return {
                "ok": False,
                "state": "rejected",
                "terminal_id": terminal_id,
                "error": "terminal belongs to another session",
            }
        return record.result()


class WaitTerminalReadinessTool(Tool):
    """Wait for an owned service's readiness probe without killing it."""

    name = "wait_terminal_ready"
    description = "Wait for an owned service readiness probe without stopping it."
    parameters = {
        "type": "object",
        "properties": {
            "terminal_id": {"type": "string"},
            "readiness": {"type": "object"},
        },
        "required": ["terminal_id"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        manager = cast("TerminalManager | None", context.get("terminal_manager"))
        if manager is None:
            return {"ok": False, "state": "rejected", "error": "terminal manager unavailable"}
        value = args.get("readiness")
        if value is not None and not isinstance(value, Mapping):
            return {
                "ok": False,
                "state": "rejected",
                "error": "readiness must be an object",
            }
        readiness = value if isinstance(value, Mapping) else None
        return await manager.wait_readiness(arg_str(args, "terminal_id"), readiness)


class StopTerminalTool(Tool):
    """Stop one exact owned terminal through its process-group owner."""

    name = "stop_terminal"
    description = "Stop one owned terminal gracefully, with bounded escalation."
    parameters = {
        "type": "object",
        "properties": {
            "terminal_id": {"type": "string"},
            "force": {"type": "boolean", "default": False},
        },
        "required": ["terminal_id"],
    }

    async def execute(
        self, args: Mapping[str, object], context: Mapping[str, object]
    ) -> dict[str, object]:
        manager = cast("TerminalManager | None", context.get("terminal_manager"))
        terminal_id = arg_str(args, "terminal_id")
        if manager is None:
            return {"ok": False, "state": "rejected", "error": "terminal manager unavailable"}
        stopped = await manager.stop(
            terminal_id,
            force=arg_bool(args, "force", False),
            reason="tool stop",
        )
        record = manager.get(terminal_id)
        if record is None:
            return {"ok": False, "state": "unknown", "terminal_id": terminal_id}
        result = record.result()
        result["stop_requested"] = stopped
        return result


class ExecToolKit:
    def tools(self) -> list[Tool]:
        return [
            RunBashTool(),
            RunCommandTool(),
            RunPythonTool(),
            RunPythonExprTool(),
            RunTestsTool(),
            WaitTerminalTool(),
            InspectTerminalTool(),
            WaitTerminalReadinessTool(),
            StopTerminalTool(),
        ]
