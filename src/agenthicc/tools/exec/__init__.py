"""Command execution tools: bash, python, run_command, run_tests (PRD-16)."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import os
import signal
import tempfile
import time
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agenthicc.tools.base import Tool, arg_bool, arg_str
from agenthicc.tools.workspace_access import WorkspaceAccessPolicy, current_workspace_access
from agenthicc.tools.exec.child import (
    StreamCapture,
    child_env,
    child_preexec,
    exec_settings,
    project_python,
)
from agenthicc.tools.exec.outcome import (
    CommandKind,
    CommandOutcome,
    CommandState,
    bash_executable,
    invalid_timeout_result,
    resolve_deadline,
    validate_timeout,
)


def _junit_summary(path: str) -> dict[str, object] | None:
    """Counts and the first failures from a pytest ``--junitxml`` report, then delete it."""
    import xml.etree.ElementTree as ElementTree
    from contextlib import suppress

    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    finally:
        with suppress(FileNotFoundError):
            os.unlink(path)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            value = suite.get(key, "0")
            totals[key] += int(value) if value.isdigit() else 0
    failures: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None or len(failures) >= 20:
            continue
        classname = case.get("classname", "")
        node = classname.replace(".", "/") + ".py::" if classname else ""
        failures.append(
            {
                "test": f"{node}{case.get('name', '')}",
                "kind": problem.tag,
                "message": (problem.get("message") or "")[:500],
            }
        )
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return {
        "passed": max(0, passed),
        "failed": totals["failures"],
        "errors": totals["errors"],
        "skipped": totals["skipped"],
        "failures": failures,
    }


def _unittest_summary(output: str) -> dict[str, object]:
    """Counts from ``python -m unittest`` output (its summary goes to stderr)."""
    ran = re.search(r"^Ran (\d+) tests?", output, re.MULTILINE)
    failures = re.search(r"failures=(\d+)", output)
    errors = re.search(r"errors=(\d+)", output)
    skipped = re.search(r"skipped=(\d+)", output)
    total = int(ran.group(1)) if ran else 0
    failed = int(failures.group(1)) if failures else 0
    errored = int(errors.group(1)) if errors else 0
    skipped_count = int(skipped.group(1)) if skipped else 0
    return {
        "passed": max(0, total - failed - errored - skipped_count) if ran else None,
        "failed": failed if ran else None,
        "errors": errored if ran else None,
        "skipped": skipped_count if ran else None,
        "failures": [],
    }


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

_CANCELLATION_OUTPUT_GRACE_S = 0.3
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


async def _drain(
    stream: asyncio.StreamReader | None,
    capture: StreamCapture,
    limit_reached: asyncio.Event,
) -> None:
    """Read *stream* into *capture* until EOF or until the output limit."""
    if stream is None:
        return
    limit = exec_settings().max_output_bytes
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        capture.feed(chunk)
        if capture.total > limit:
            limit_reached.set()
            return


async def _run_proc(
    cmd: list[str],
    *,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> dict[str, object]:
    t0 = time.perf_counter()
    effective_env = child_env(env)
    kind = CommandKind.SHELL if shell else CommandKind.EXEC
    deadline = resolve_deadline(timeout)
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[None] | None = None
    stdout_capture = StreamCapture()
    stderr_capture = StreamCapture()
    stderr_notes: list[str] = []
    limit_reached = asyncio.Event()

    if not await asyncio.to_thread(os.path.isdir, cwd):
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

    async def collect(proc: asyncio.subprocess.Process) -> None:
        await asyncio.gather(
            _drain(proc.stdout, stdout_capture, limit_reached),
            _drain(proc.stderr, stderr_capture, limit_reached),
        )
        await proc.wait()

    async def output_after_cleanup() -> None:
        if communication is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(communication), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stderr_notes.append("[process output could not be fully drained]")

    async def spawn_process() -> asyncio.subprocess.Process:
        if shell:
            return await asyncio.create_subprocess_shell(
                cmd[0],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=effective_env,
                start_new_session=True,
                preexec_fn=child_preexec(),
                executable=await asyncio.to_thread(bash_executable),
            )
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=effective_env,
            start_new_session=True,
            preexec_fn=child_preexec(),
        )

    try:
        # Shield the spawn itself.  asyncio can cancel the parent after the
        # child has been created but before create_subprocess_* returns.  The
        # old implementation then lost the Process handle and could leave an
        # orphan.  Finishing this tiny task gives cancellation cleanup the
        # handle it needs.
        spawn_task = asyncio.create_task(spawn_process(), name="command-spawn")
        spawn_cancelled = False
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            spawn_cancelled = True
            proc = await asyncio.shield(spawn_task)

        process = proc
        communication = asyncio.create_task(collect(process), name="command-output")
        cleanup_result = "not_required"
        state = CommandState.EXITED
        termination_reason: str | None = None
        cancelled = False
        if spawn_cancelled:
            state = CommandState.CANCELLED
            cancelled = True
            termination_reason = "owning task cancelled"
            # The cancellation may have arrived while the child was still
            # being spawned. Let the readers observe already-produced pipe
            # data before terminating a process whose handle we only just
            # recovered.
            try:
                await asyncio.wait_for(
                    asyncio.shield(communication), timeout=_CANCELLATION_OUTPUT_GRACE_S
                )
            except asyncio.TimeoutError:
                pass
            cleanup_task = asyncio.create_task(cleanup(reason="cancellation"))
            cleanup_result = await asyncio.shield(cleanup_task)
            await output_after_cleanup()
            stderr_notes.append("[process stopped: cancellation]")
            if _PROPAGATE_TOOL_CANCELLATION.get():
                raise asyncio.CancelledError
        else:
            limit_task = asyncio.create_task(limit_reached.wait(), name="command-output-limit")
            try:
                done, _pending = await asyncio.wait(
                    {communication, limit_task},
                    timeout=deadline.effective_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if limit_task in done and not communication.done():
                    state = CommandState.FAILED
                    termination_reason = "output_limit"
                    cleanup_result = await cleanup(reason="output_limit")
                    await output_after_cleanup()
                    stderr_notes.append(
                        f"[process stopped: output exceeded "
                        f"{exec_settings().max_output_bytes} bytes]"
                    )
                elif not done:
                    state = CommandState.TIMED_OUT
                    termination_reason = "command deadline expired"
                    cleanup_result = await cleanup(reason="timeout")
                    await output_after_cleanup()
                    stderr_notes.append("[process stopped: command timeout]")
            except asyncio.CancelledError:
                state = CommandState.CANCELLED
                cancelled = True
                termination_reason = "owning task cancelled"
                try:
                    await asyncio.wait_for(
                        asyncio.shield(communication), timeout=_CANCELLATION_OUTPUT_GRACE_S
                    )
                except asyncio.TimeoutError:
                    pass
                cleanup_task = asyncio.create_task(cleanup(reason="cancellation"))
                cleanup_result = await asyncio.shield(cleanup_task)
                await output_after_cleanup()
                stderr_notes.append("[process stopped: cancellation]")
                if _PROPAGATE_TOOL_CANCELLATION.get():
                    raise
            finally:
                limit_task.cancel()
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

    stdout = _redact_execution_text(stdout_capture.render())
    stderr_text = stderr_capture.render()
    if stderr_notes:
        stderr_text += ("" if not stderr_text or stderr_text.endswith("\n") else "\n") + (
            "\n".join(stderr_notes) + "\n"
        )
    stderr = _redact_execution_text(stderr_text)
    outcome = CommandOutcome(
        state=state,
        command_kind=kind,
        returncode=process.returncode if process is not None else -1,
        stdout=stdout,
        stderr=stderr,
        duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        timed_out=state is CommandState.TIMED_OUT,
        cancelled=cancelled,
        truncated=stdout_capture.omitted > 0 or stderr_capture.omitted > 0,
        termination_reason=termination_reason,
        deadline=deadline,
        cleanup_result=cleanup_result,
        command=_redact_execution_text(cmd[0]) if shell else None,
        argv=None if shell else tuple(_redact_execution_text(item) for item in cmd),
        cwd=cwd,
    )
    result = outcome.to_dict()
    result["stdout_bytes"] = stdout_capture.total
    result["stderr_bytes"] = stderr_capture.total
    return result


def _timeout(
    args: Mapping[str, object], default: float, kind: CommandKind
) -> tuple[float | None, dict[str, object] | None]:
    raw = args.get("timeout", default)
    try:
        return validate_timeout(raw), None
    except ValueError:
        return None, invalid_timeout_result(raw, command_kind=kind)


def _cwd(args: Mapping[str, object], context: Mapping[str, object]) -> str:
    """Legacy synchronous cwd resolver retained for adapter callers/tests."""

    requested = args.get("cwd")
    root = context.get("workspace_root", ".")
    base = (Path(root) if isinstance(root, str) else Path(".")).resolve()
    requested_text = requested if isinstance(requested, str) and requested else "."
    path = Path(requested_text)
    return str(path if path.is_absolute() else base / path)


async def _cwd_async(
    args: Mapping[str, object],
    context: Mapping[str, object],
    *,
    tool_name: str,
) -> str:
    requested = args.get("cwd")
    root = context.get("workspace_root", ".")
    base = (Path(root) if isinstance(root, str) else Path(".")).resolve()
    requested_text = requested if isinstance(requested, str) and requested else "."
    policy = context.get("workspace_access")
    if not isinstance(policy, WorkspaceAccessPolicy):
        policy = current_workspace_access()
    if policy is not None:
        result = await policy.authorize_tool(
            tool_name,
            {"cwd": requested_text},
            frozenset({"execute"}),
        )
        if not result.allowed:
            raise PermissionError(f"{result.code}: {result.error}")
        return str(
            await policy.authorize(
                requested_text,
                operation="execute_cwd",
                tool_name=tool_name,
                capabilities=frozenset({"execute"}),
                tool_input={"cwd": requested_text},
            )
        )
    path = Path(requested_text)
    return str(path if path.is_absolute() else base / path)


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
        try:
            cwd = await _cwd_async(args, context, tool_name=self.name)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), "code": "workspace_access"}
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
        try:
            cwd = await _cwd_async(args, context, tool_name=self.name)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), "code": "workspace_access"}
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
        try:
            cwd = await _cwd_async({}, context, tool_name=self.name)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), "code": "workspace_access"}
        code = arg_str(args, "code")
        timeout, invalid = _timeout(args, 30.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp_path = f.name
        interpreter = await asyncio.to_thread(
            project_python, cwd, arg_str(context, "workspace_root", cwd)
        )
        try:
            result = await _run_proc(
                [*interpreter.argv, tmp_path],
                cwd=cwd,
                timeout=timeout or 0.0,
                shell=False,
            )
            result["interpreter"] = " ".join(interpreter.argv)
            result["interpreter_source"] = interpreter.source
            return result
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
        try:
            cwd = await _cwd_async({}, context, tool_name=self.name)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), "code": "workspace_access"}
        expression = arg_str(args, "expression")
        timeout, invalid = _timeout(args, 10.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
        code = f"_r = ({expression}); print(repr(_r))"
        interpreter = await asyncio.to_thread(
            project_python, cwd, arg_str(context, "workspace_root", cwd)
        )
        result = await _run_proc(
            [*interpreter.argv, "-c", code],
            cwd=cwd,
            timeout=timeout or 0.0,
            shell=False,
        )
        result["result"] = _result_text(result, "stdout").strip()
        result["interpreter"] = " ".join(interpreter.argv)
        result["interpreter_source"] = interpreter.source
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
        import uuid

        path = arg_str(args, "path", "tests/")
        executable_path = path
        policy = context.get("workspace_access")
        if not isinstance(policy, WorkspaceAccessPolicy):
            policy = current_workspace_access()
        if policy is not None:
            path_result = await policy.authorize_tool(
                self.name,
                {"cwd": args.get("cwd", ""), "path": path},
                frozenset({"execute"}),
            )
            if not path_result.allowed:
                return {
                    "ok": False,
                    "error": path_result.error,
                    "code": path_result.code,
                }

        try:
            # Carry the complete declared path input through the cwd
            # authorization so an outside cwd and outside test target are
            # presented as one scope decision and share one final grant.
            cwd = await _cwd_async(args, context, tool_name=self.name)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), "code": "workspace_access"}
        if policy is not None:
            try:
                canonical_path = await policy.authorize(
                    path,
                    operation="execute_path",
                    tool_name=self.name,
                    capabilities=frozenset({"execute"}),
                    tool_input={"cwd": args.get("cwd", ""), "path": path},
                )
            except (OSError, PermissionError, ValueError) as exc:
                return {"ok": False, "error": str(exc), "code": "workspace_access"}
            # Use the revalidated canonical target in the subprocess argv.  A
            # preflight on one spelling must not authorize a later symlink or
            # relative-path interpretation of a different target.
            executable_path = str(canonical_path)
        timeout, invalid = _timeout(args, 120.0, CommandKind.EXEC)
        if invalid is not None:
            return invalid
        raw_extra = args.get("args") or []
        if not isinstance(raw_extra, list) or not all(isinstance(item, str) for item in raw_extra):
            raise ValueError("tool argument 'args' must be a list of strings")
        # The report location is ours; extra arguments may not redirect it.
        extra = [item for item in raw_extra if not item.startswith(("--junitxml", "--junit-xml"))]
        framework = arg_str(args, "framework", "pytest")
        interpreter = await asyncio.to_thread(
            project_python, cwd, arg_str(context, "workspace_root", cwd)
        )
        report_path = os.path.join(tempfile.gettempdir(), f"agenthicc-junit-{uuid.uuid4().hex}.xml")
        if framework == "pytest":
            cmd = [
                *interpreter.argv,
                "-m",
                "pytest",
                executable_path,
                f"--junitxml={report_path}",
                "-q",
                *extra,
            ]
        else:
            cmd = [*interpreter.argv, "-m", "unittest", "discover", executable_path, *extra]

        result = await _run_proc(cmd, cwd=cwd, timeout=timeout or 0.0, shell=False)
        result["interpreter"] = " ".join(interpreter.argv)
        result["interpreter_source"] = interpreter.source
        if framework == "pytest":
            summary = await asyncio.to_thread(_junit_summary, report_path)
            if summary is None and "No module named pytest" in _result_text(result, "stderr"):
                result.update(
                    {
                        "ok": False,
                        "error_code": "framework_unavailable",
                        "error": f"pytest is not installed for {result['interpreter']}",
                        "passed": None,
                        "failed": None,
                        "errors": None,
                    }
                )
                return result
            if summary is None:
                summary = {
                    "passed": None,
                    "failed": None,
                    "errors": None,
                    "skipped": None,
                    "failures": [],
                }
        else:
            summary = _unittest_summary(
                _result_text(result, "stderr") + "\n" + _result_text(result, "stdout")
            )
        result.update(summary)
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
