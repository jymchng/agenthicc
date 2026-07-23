"""@tool() wrappers for exec/shell tools — for use with lauren-ai AgentRunnerBase.

NOTE: no ``from __future__ import annotations`` — @tool() inspects real annotations.
"""

import os
from lauren_ai._tools import tool
from agenthicc.tools.capabilities import tool_execute

__all__ = [
    "run_bash",
    "run_command",
    "run_python",
    "run_python_expr",
    "run_tests",
    "shell",
    "EXEC_AGENT_TOOLS",
]

_CTX = lambda: {"workspace_root": os.getcwd()}  # noqa: E731


@tool_execute
@tool()
async def shell(command: str, timeout: float = 30.0) -> dict[str, object]:
    """Execute a shell command and return stdout/stderr.

    Args:
        command: Shell command string to execute.
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunBashTool  # noqa: PLC0415

    return await RunBashTool().execute({"command": command, "timeout": timeout}, _CTX())


@tool_execute
@tool()
async def run_bash(command: str, timeout: float = 30.0) -> dict[str, object]:
    """Execute a bash shell command and return stdout/stderr.

    Args:
        command: Shell command string to execute.
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunBashTool  # noqa: PLC0415

    return await RunBashTool().execute({"command": command, "timeout": timeout}, _CTX())


@tool_execute
@tool()
async def run_command(argv: list[str], timeout: float = 30.0) -> dict[str, object]:
    """Execute an executable directly (no shell) and return stdout/stderr.

    Args:
        argv: Command and arguments as a list, e.g. ["python", "-c", "print(1)"].
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunCommandTool  # noqa: PLC0415

    return await RunCommandTool().execute({"argv": argv, "timeout": timeout}, _CTX())


@tool_execute
@tool()
async def run_python(code: str, timeout: float = 30.0) -> dict[str, object]:
    """Execute a Python code snippet in a subprocess.

    Args:
        code: Python source code to execute.
        timeout: Maximum seconds to wait (default 30).
    """
    from agenthicc.tools.exec import RunPythonTool  # noqa: PLC0415

    return await RunPythonTool().execute({"code": code, "timeout": timeout}, _CTX())


@tool_execute
@tool()
async def run_python_expr(expression: str, timeout: float = 10.0) -> dict[str, object]:
    """Evaluate a Python expression and return its repr.

    Args:
        expression: Python expression to evaluate, e.g. "2 ** 10".
        timeout: Maximum seconds to wait (default 10).
    """
    from agenthicc.tools.exec import RunPythonExprTool  # noqa: PLC0415

    return await RunPythonExprTool().execute({"expression": expression, "timeout": timeout}, _CTX())


@tool_execute
@tool()
async def run_tests(
    path: str = "tests/",
    framework: str = "pytest",
    args: list[str] | None = None,
    timeout: float = 120.0,
) -> dict[str, object]:
    """Run the test suite and return pass/fail counts.

    Args:
        path: Path to the test directory or file (default: tests/).
        framework: Test framework to use: "pytest" or "unittest" (default pytest).
        args: Additional CLI arguments passed to the test runner.
        timeout: Maximum seconds to wait (default 120).
    """
    from agenthicc.tools.exec import RunTestsTool  # noqa: PLC0415

    return await RunTestsTool().execute(
        {"path": path, "framework": framework, "args": args or [], "timeout": timeout},
        _CTX(),
    )


#: All exec agent tools — ready to pass to @use_tools().
EXEC_AGENT_TOOLS = [shell, run_bash, run_command, run_python, run_python_expr, run_tests]
