"""TOOLR-04: the error vocabulary, the envelope builder and the contract hook."""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import httpx
import pytest
from lauren_ai._tools import ToolContext, _add_to_tool_map
from lauren_ai._tools._executor import ToolExecutor
from lauren_ai._transport import ToolCall

from agenthicc.tools.approval import order_global_hooks
from agenthicc.tools.errors import (
    ERROR_MESSAGE_LIMIT,
    ToolErrorCode,
    classify_exception,
    classify_message,
    complete_envelope,
    tool_error,
)

pytestmark = pytest.mark.unit


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    return httpx.HTTPStatusError(
        "bad", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (TimeoutError(), ToolErrorCode.TIMEOUT),
        (FileNotFoundError("x"), ToolErrorCode.NOT_FOUND),
        (FileExistsError("x"), ToolErrorCode.ALREADY_EXISTS),
        (PermissionError("x"), ToolErrorCode.PERMISSION_DENIED),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), ToolErrorCode.UNDECODABLE),
        (_status_error(404), ToolErrorCode.HTTP_STATUS),
        (ConnectionRefusedError(), ToolErrorCode.NETWORK),
        (socket.gaierror(), ToolErrorCode.NETWORK),
        (httpx.ConnectError("x"), ToolErrorCode.NETWORK),
        (OSError("disk full"), ToolErrorCode.INTERNAL),
        (re.error("bad"), ToolErrorCode.INVALID_ARGUMENT),
        (ValueError("bad"), ToolErrorCode.INVALID_ARGUMENT),
        (TypeError("bad"), ToolErrorCode.INVALID_ARGUMENT),
        (RuntimeError("bad"), ToolErrorCode.INTERNAL),
    ],
)
def test_exceptions_map_to_one_code(exc: BaseException, code: ToolErrorCode) -> None:
    assert classify_exception(exc) is code


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("path is outside workspace", ToolErrorCode.OUTSIDE_WORKSPACE),
        ("User denied permission to run 'x'.", ToolErrorCode.APPROVAL_DENIED),
        ("write blocked in Plan mode", ToolErrorCode.BLOCKED_BY_MODE),
        ("permission_denied: nope", ToolErrorCode.PERMISSION_DENIED),
        ("command timed out", ToolErrorCode.TIMEOUT),
        ("file not found: a", ToolErrorCode.NOT_FOUND),
        ("browser is not installed", ToolErrorCode.UNAVAILABLE),
        ("something odd", ToolErrorCode.INTERNAL),
    ],
)
def test_free_text_failures_are_classified(message: str, code: ToolErrorCode) -> None:
    assert classify_message(message) is code


def test_a_failure_is_bounded_and_retryable_by_its_code() -> None:
    failure = tool_error(ToolErrorCode.TIMEOUT, "x" * 5_000, path="a")

    assert failure["ok"] is False and failure["error_code"] == "timeout"
    assert failure["retryable"] is True and failure["path"] == "a"
    assert len(str(failure["error"])) == ERROR_MESSAGE_LIMIT
    assert tool_error(ToolErrorCode.NOT_FOUND, "x")["retryable"] is False
    assert tool_error(ToolErrorCode.NOT_FOUND, "x", retryable=True)["retryable"] is True


def test_complete_envelope_fills_only_what_is_missing() -> None:
    assert complete_envelope({"value": 1}) == {"ok": True, "value": 1}
    unchanged = {"ok": True, "value": 1}
    assert complete_envelope(unchanged) is unchanged
    assert complete_envelope("text") == "text"
    assert complete_envelope([1, 2]) == [1, 2]

    failure = complete_envelope({"error": "file not found: a"})
    assert failure == {
        "ok": False,
        "error": "file not found: a",
        "error_code": "not_found",
        "retryable": False,
    }
    kept = complete_envelope({"ok": False, "error": "x", "error_code": "stale_file"})
    assert isinstance(kept, dict) and kept["error_code"] == "stale_file" and kept["retryable"]
    invented = complete_envelope({"ok": False, "error": "x", "error_code": "made_up"})
    assert isinstance(invented, dict) and invented["error_code"] == "internal"


def _workspace(root: Path) -> object:
    from agenthicc.tools.workspace_access import (
        WorkspaceAccessPolicy,
        WorkspaceScope,
        set_current_workspace_access,
    )
    from agenthicc.tui.conversation_store import AppState
    from agenthicc.tui.runtime.mode_manager import build_default_registry

    app = AppState.create()
    app.active_mode.set(build_default_registry().get("Yolo"))
    return set_current_workspace_access(
        WorkspaceAccessPolicy(WorkspaceScope.create(root), mode_provider=app.active_mode)
    )


async def _call(tool: object, arguments: dict[str, object]) -> dict[str, object]:
    tool_map: dict[str, object] = {}
    _add_to_tool_map(tool_map, tool)
    (name,) = tool_map
    executor = ToolExecutor(tool_map, global_hooks=order_global_hooks([]))  # type: ignore[arg-type]
    result = await executor.execute(
        ToolCall(tool_use_id="t", name=name, input=arguments),
        ToolContext(agent_context=None, tool_use_id="t", turn=0),
    )
    assert result.is_error is False
    content = json.loads(result.content) if isinstance(result.content, str) else result.content
    assert isinstance(content, dict)
    return content


async def test_an_invalid_regex_and_a_bad_args_list_are_invalid_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tools.exec.agent_tools import run_tests
    from agenthicc.tools.fs.agent_tools import grep_files
    from agenthicc.tools.workspace_access import reset_current_workspace_access

    (tmp_path / "a.txt").write_text("x\n")
    monkeypatch.chdir(tmp_path)
    token = _workspace(tmp_path)
    try:
        regex = await _call(grep_files, {"path": "a.txt", "pattern": "("})
        args = await _call(run_tests, {"args": "not-a-list"})
    finally:
        reset_current_workspace_access(token)  # type: ignore[arg-type]

    assert regex["ok"] is False and regex["error_code"] == "invalid_argument"
    assert args["ok"] is False and args["error_code"] == "invalid_argument"
    assert "ValueError" in str(args["error"]) and args["retryable"] is False


async def test_a_success_without_ok_reaches_the_provider_with_ok() -> None:
    from lauren_ai._tools import tool

    from agenthicc.tools.capabilities import tool_read

    @tool_read
    @tool()
    async def bare_probe() -> dict[str, object]:
        """Return a mapping without ``ok``."""
        return {"answer": 42}

    assert await _call(bare_probe, {}) == {"ok": True, "answer": 42}


async def test_a_raising_tool_inside_a_real_agent_turn_returns_the_envelope() -> None:
    """Through the runner a session builds: no traceback-shaped tool_result."""
    from lauren_ai._agents import agent, use_tools
    from lauren_ai._agents._runner import AgentRunnerBase
    from lauren_ai._tools import tool
    from lauren_ai._transport import Completion, TokenUsage
    from lauren_ai._transport._mock import MockTransport

    from agenthicc.runners.tool_populator import populate_agent_tools
    from agenthicc.tools.capabilities import tool_read

    @tool_read
    @tool()
    async def exploding_probe() -> dict[str, object]:
        """Always fail."""
        raise KeyError("missing")

    usage = TokenUsage(input_tokens=1, output_tokens=1)
    transport = MockTransport()
    transport.queue_tool_use("exploding_probe", {}, tool_use_id="x", model="m")
    transport.queue_response(
        Completion(
            id="2", model="m", content="done", stop_reason="end_turn", usage=usage, tool_calls=[]
        )
    )

    @agent(model="m")
    @use_tools(exploding_probe)
    class _Agent: ...

    instance = _Agent()
    assert populate_agent_tools(instance, [exploding_probe]) == ()
    runner = AgentRunnerBase(transport=transport, global_hooks=order_global_hooks([]))
    await runner.run(instance, "go")

    sent = repr(transport.calls[1].messages)
    assert '"error_code": "internal"' in sent and "KeyError" in sent
    assert "Traceback" not in sent and "failed:" not in sent
