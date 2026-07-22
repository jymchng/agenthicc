"""Tests for permanent-error early exit in workflow phase loops (PRD-117)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.runners.agent_turn import _http_status_code, _is_permanent_error
from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.code_plan.state import CodePlanContext, CodePlanState


# ── _http_status_code ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_http_status_code_direct_attribute() -> None:
    exc = Exception("bad request")
    exc.status_code = 400  # type: ignore[attr-defined]
    assert _http_status_code(exc) == 400


@pytest.mark.unit
def test_http_status_code_from_chained_cause() -> None:
    inner = Exception("inner")
    inner.status_code = 401  # type: ignore[attr-defined]
    outer = Exception("outer")
    outer.__cause__ = inner  # type: ignore[attr-defined]
    assert _http_status_code(outer) == 401


@pytest.mark.unit
def test_http_status_code_from_chained_context() -> None:
    inner = Exception("inner")
    inner.status_code = 403  # type: ignore[attr-defined]
    outer = Exception("outer")
    outer.__context__ = inner  # type: ignore[attr-defined]
    assert _http_status_code(outer) == 403


@pytest.mark.unit
def test_http_status_code_none_when_absent() -> None:
    assert _http_status_code(ValueError("no status")) is None


@pytest.mark.unit
def test_http_status_code_ignores_non_int() -> None:
    exc = Exception("bad")
    exc.status_code = "400"  # type: ignore[attr-defined]  — string, not int
    assert _http_status_code(exc) is None


# ── _is_permanent_error ───────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 499])
def test_is_permanent_4xx_except_429(status: int) -> None:
    exc = Exception("client error")
    exc.status_code = status  # type: ignore[attr-defined]
    assert _is_permanent_error(exc) is True


@pytest.mark.unit
def test_is_permanent_429_is_transient() -> None:
    """429 rate-limit is transient — worth retrying."""
    exc = Exception("rate limited")
    exc.status_code = 429  # type: ignore[attr-defined]
    assert _is_permanent_error(exc) is False


@pytest.mark.unit
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_is_permanent_5xx_is_transient(status: int) -> None:
    exc = Exception("server error")
    exc.status_code = status  # type: ignore[attr-defined]
    assert _is_permanent_error(exc) is False


@pytest.mark.unit
def test_is_permanent_no_status_is_transient() -> None:
    """Network errors without an HTTP status are treated as transient."""
    assert _is_permanent_error(ConnectionError("network unreachable")) is False
    assert _is_permanent_error(TimeoutError("timed out")) is False


@pytest.mark.unit
def test_is_permanent_detects_via_chained_cause() -> None:
    """Works even when the status is on the chained inner exception."""
    inner = Exception("BadRequestError")
    inner.status_code = 400  # type: ignore[attr-defined]
    outer = Exception("TransportError")
    outer.__cause__ = inner  # type: ignore[attr-defined]
    assert _is_permanent_error(outer) is True


# ── _stream() re-raises permanent errors ─────────────────────────────────────


@pytest.mark.unit
async def test_stream_reraises_permanent_error() -> None:
    """_stream() must re-raise 4xx errors so phase loops can exit immediately."""
    from agenthicc.runners.agent_turn import AgentTurnRunner
    from agenthicc.runners.agent_turn_context import AgentTurnContext
    from agenthicc.config import ExecutionSettings

    perm_exc = Exception("TransportError: 400 model not found")
    perm_exc.status_code = 400  # type: ignore[attr-defined]

    conv_store = MagicMock()
    ctx = AgentTurnContext(
        text="hello",
        runner=MagicMock(),
        processor=MagicMock(),
        session_memory=None,
        max_agent_turns=1,
        conv_store=conv_store,
        app_state=None,
        exec_cfg=ExecutionSettings(),
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=None,
        system_prompt_suffix="",
    )

    runner = AgentTurnRunner(ctx)
    runner._turn_active = True
    runner._model_id = "test-model"
    runner._model_short = "test"

    # Make run_stream raise a permanent error
    mock_runner = MagicMock()
    mock_runner.run_stream = AsyncMock(side_effect=perm_exc)

    with pytest.raises(Exception) as exc_info:
        await runner._stream(MagicMock(), "test text", mock_runner)

    # The permanent error must propagate out
    assert exc_info.value is perm_exc

    # The TUI error event must still have been emitted
    conv_store.append_event.assert_called_once()
    call_args = conv_store.append_event.call_args
    assert call_args[0][0] == "error"
    assert "400" in str(call_args[0][1]["message"]) or "TransportError" in str(
        call_args[0][1]["message"]
    )

    # close_turn() must still be called (finally block ran)
    conv_store.close_turn.assert_called_once()


@pytest.mark.unit
async def test_stream_swallows_transient_error() -> None:
    """_stream() must NOT re-raise 5xx or network errors."""
    from agenthicc.runners.agent_turn import AgentTurnRunner
    from agenthicc.runners.agent_turn_context import AgentTurnContext
    from agenthicc.config import ExecutionSettings

    transient_exc = Exception("ServerError")
    transient_exc.status_code = 503  # type: ignore[attr-defined]

    conv_store = MagicMock()
    ctx = AgentTurnContext(
        text="hello",
        runner=MagicMock(),
        processor=MagicMock(),
        session_memory=None,
        max_agent_turns=1,
        conv_store=conv_store,
        app_state=None,
        exec_cfg=ExecutionSettings(),
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=None,
        system_prompt_suffix="",
    )

    runner = AgentTurnRunner(ctx)
    runner._turn_active = True
    runner._model_id = "test-model"
    runner._model_short = "test"

    mock_runner = MagicMock()
    mock_runner.run_stream = AsyncMock(side_effect=transient_exc)

    # Must NOT raise — transient errors are swallowed
    await runner._stream(MagicMock(), "test text", mock_runner)

    # Error event still emitted to TUI
    conv_store.append_event.assert_called_once()


# ── CodePlanRunner phase methods ──────────────────────────────────────────────


def _make_runner() -> CodePlanRunner:
    mock_cfg = MagicMock()
    mock_cfg.app_state.workflow_run.return_value = None
    mock_cfg.app_state.active_mode.return_value = MagicMock(
        blocked_capabilities=frozenset(), badge="P"
    )
    mock_cfg.app_state.update_workflow_phase = MagicMock()
    mock_cfg.processor.emit = AsyncMock()
    mock_cfg.approval_svc = None
    mock_cfg.plugin_tools = []
    mock_cfg.mcp_registry = None
    mock_cfg.memory_router = None
    mock_cfg.semantic_index = None
    mock_cfg.skills = {}
    mock_cfg.mention_cache = MagicMock()
    mock_cfg.completed_turns = 0
    mock_cfg.cfg.execution = MagicMock(model="test-model")
    mock_cfg.agent_runner = MagicMock()
    runner = CodePlanRunner(mock_cfg, None)
    runner._model_id = "test-model"
    runner._run_id = "test-run"
    return runner


def _make_ctx() -> CodePlanContext:
    return CodePlanContext(
        intent="test intent",
        run_id="test-run",
        shared_memory=MagicMock(),
    )


def _perm_exc(status: int = 400) -> Exception:
    exc = Exception(f"TransportError: {status} bad request")
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


@pytest.mark.unit
async def test_plan_exits_immediately_on_permanent_error() -> None:
    """_plan() returns FAILED on the first attempt when _run_turn raises 4xx."""
    runner = _make_runner()
    ctx = _make_ctx()

    run_turn_call_count = 0

    async def fake_run_turn(*args, **kwargs) -> None:
        nonlocal run_turn_call_count
        run_turn_call_count += 1
        raise _perm_exc(400)

    with patch.object(runner, "_run_turn", side_effect=fake_run_turn):
        result = await runner._plan(ctx)

    assert result is CodePlanState.FAILED
    assert run_turn_call_count == 1  # only ONE attempt, not 10
    assert "TransportError" in ctx.fail_reason
    assert "400" in ctx.fail_reason


@pytest.mark.unit
async def test_plan_sets_fail_reason_to_exception_message() -> None:
    runner = _make_runner()
    ctx = _make_ctx()

    async def fake_run_turn(*args, **kwargs) -> None:
        raise _perm_exc(401)

    with patch.object(runner, "_run_turn", side_effect=fake_run_turn):
        await runner._plan(ctx)

    assert ctx.fail_reason != ""
    assert "401" in ctx.fail_reason or "TransportError" in ctx.fail_reason


@pytest.mark.unit
async def test_execute_exits_immediately_on_permanent_error() -> None:
    runner = _make_runner()
    ctx = _make_ctx()
    ctx.plan = "approved plan text"

    run_turn_call_count = 0

    async def fake_run_turn(*args, **kwargs) -> None:
        nonlocal run_turn_call_count
        run_turn_call_count += 1
        raise _perm_exc(403)

    with patch.object(runner, "_run_turn", side_effect=fake_run_turn):
        result = await runner._execute(ctx)

    assert result is CodePlanState.FAILED
    assert run_turn_call_count == 1
    assert "403" in ctx.fail_reason or "TransportError" in ctx.fail_reason


@pytest.mark.unit
async def test_review_exits_immediately_on_permanent_error() -> None:
    runner = _make_runner()
    ctx = _make_ctx()
    ctx.plan = "a plan"
    ctx.execute_summary = "done"

    run_turn_call_count = 0

    async def fake_run_turn(*args, **kwargs) -> None:
        nonlocal run_turn_call_count
        run_turn_call_count += 1
        raise _perm_exc(422)

    with patch.object(runner, "_run_turn", side_effect=fake_run_turn):
        result = await runner._review(ctx)

    assert result is CodePlanState.FAILED
    assert run_turn_call_count == 1
    assert ctx.fail_reason != ""


@pytest.mark.unit
async def test_plan_continues_after_transient_error() -> None:
    """Transient errors are swallowed by _stream; _plan loop should continue."""
    runner = _make_runner()
    ctx = _make_ctx()

    # _run_turn returns normally (transient errors are swallowed by _stream
    # before reaching _run_turn's caller).  Simulate that here: two calls
    # return normally, third call fires the plan_event (simulated by not
    # firing any event — the loop exhausts attempts naturally).
    call_count = 0

    async def fake_run_turn(*args, **kwargs) -> None:
        nonlocal call_count
        call_count += 1
        # No exception — simulates _stream swallowing a transient error

    with patch.object(runner, "_run_turn", side_effect=fake_run_turn):
        # No exception from _run_turn means no early exit; loop runs to cap
        result = await runner._plan(ctx)

    # Without an exception propagating, the loop runs to _MAX_PLAN_ATTEMPTS
    # and falls through to the "exhausted attempts" failure path.
    assert result is CodePlanState.FAILED
    assert "exhausted" in ctx.fail_reason.lower()


@pytest.mark.unit
async def test_429_rate_limit_not_treated_as_permanent() -> None:
    """429 re-raised by _stream would only happen if we mis-classified it.
    Verify _is_permanent_error returns False for 429."""
    exc = Exception("rate limited")
    exc.status_code = 429  # type: ignore[attr-defined]
    from agenthicc.runners.agent_turn import _is_permanent_error

    assert _is_permanent_error(exc) is False
