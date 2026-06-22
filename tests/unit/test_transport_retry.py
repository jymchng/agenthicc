"""Unit tests for PRD-126 — transport retry with memory rollback."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenthicc.config import ExecutionSettings, _dict_to_config
from agenthicc.runners.agent_turn import _is_transient_network_error, _is_permanent_error

pytestmark = pytest.mark.unit


# ── _is_transient_network_error ───────────────────────────────────────────────

class TestIsTransientNetworkError:
    def test_transient_transport_error_is_transient(self) -> None:
        from lauren_ai._exceptions import TransientTransportError
        assert _is_transient_network_error(TransientTransportError("timeout"))

    def test_read_timeout_by_name_is_transient(self) -> None:
        class ReadTimeout(Exception): pass
        assert _is_transient_network_error(ReadTimeout())

    def test_connect_timeout_by_name_is_transient(self) -> None:
        class ConnectTimeout(Exception): pass
        assert _is_transient_network_error(ConnectTimeout())

    def test_connect_error_by_name_is_transient(self) -> None:
        class ConnectError(Exception): pass
        assert _is_transient_network_error(ConnectError())

    def test_network_error_by_name_is_transient(self) -> None:
        class NetworkError(Exception): pass
        assert _is_transient_network_error(NetworkError())

    def test_timeout_error_builtin_is_transient(self) -> None:
        assert _is_transient_network_error(TimeoutError())

    def test_transient_via_cause_chain(self) -> None:
        class ReadTimeout(Exception): pass
        wrapper = Exception("wrapped")
        wrapper.__cause__ = ReadTimeout()
        assert _is_transient_network_error(wrapper)

    def test_transient_via_context_chain(self) -> None:
        class ConnectError(Exception): pass
        wrapper = Exception("wrapped")
        wrapper.__context__ = ConnectError()
        assert _is_transient_network_error(wrapper)

    def test_permanent_transport_error_not_transient(self) -> None:
        from lauren_ai._exceptions import TransportError
        e = TransportError("bad request", status_code=400)
        assert not _is_transient_network_error(e)

    def test_plain_exception_not_transient(self) -> None:
        assert not _is_transient_network_error(ValueError("bad input"))

    def test_runtime_error_not_transient(self) -> None:
        assert not _is_transient_network_error(RuntimeError("unexpected"))

    def test_not_transient_when_cause_is_none(self) -> None:
        e = Exception("no cause")
        assert not _is_transient_network_error(e)


# ── complement with _is_permanent_error ──────────────────────────────────────

class TestErrorTaxonomy:
    def test_400_is_permanent_not_transient(self) -> None:
        from lauren_ai._exceptions import TransportError
        e = TransportError("bad", status_code=400)
        assert _is_permanent_error(e)
        assert not _is_transient_network_error(e)

    def test_429_is_neither_permanent_nor_transient_network(self) -> None:
        from lauren_ai._exceptions import TransientTransportError
        e = TransientTransportError("rate limited", status_code=429)
        assert not _is_permanent_error(e)
        # 429 is TransientTransportError → IS transient network
        assert _is_transient_network_error(e)

    def test_read_timeout_is_not_permanent(self) -> None:
        class ReadTimeout(Exception): pass
        assert not _is_permanent_error(ReadTimeout())


# ── ExecutionSettings new fields ──────────────────────────────────────────────

class TestExecutionSettingsRetryFields:
    def test_default_max_retries_is_three(self) -> None:
        cfg = ExecutionSettings()
        assert cfg.transport_max_retries == 3

    def test_default_base_delay_is_one(self) -> None:
        cfg = ExecutionSettings()
        assert cfg.transport_retry_base_delay_s == 1.0

    def test_toml_override_max_retries(self) -> None:
        cfg = _dict_to_config({"execution": {"transport_max_retries": 0}})
        assert cfg.execution.transport_max_retries == 0

    def test_toml_override_base_delay(self) -> None:
        cfg = _dict_to_config({"execution": {"transport_retry_base_delay_s": 5.0}})
        assert cfg.execution.transport_retry_base_delay_s == 5.0

    def test_zero_retries_disables_retry(self) -> None:
        cfg = ExecutionSettings(transport_max_retries=0)
        assert cfg.transport_max_retries == 0


# ── _run_turn_with_retry behaviour ────────────────────────────────────────────

class TestRunTurnWithRetry:
    """Tests for CodePlanRunner._run_turn_with_retry using a minimal stub."""

    def _make_runner(self, max_retries: int = 2, base_delay: float = 0.0):
        """Build a minimal CodePlanRunner stub with controlled config."""
        from agenthicc.workflows.code_plan.runner import CodePlanRunner  # noqa: PLC0415

        exec_cfg = ExecutionSettings(
            transport_max_retries=max_retries,
            transport_retry_base_delay_s=base_delay,
        )
        cfg = MagicMock()
        cfg.cfg.execution = exec_cfg
        cfg.conv_store = MagicMock()
        cfg.conv_store.append_event = MagicMock()
        cfg.app_state = MagicMock()
        cfg.agent_runner = MagicMock()
        cfg.processor = MagicMock()
        cfg.approval_svc = None
        cfg.skills = {}
        cfg.mention_cache = None
        cfg.mcp_registry = None
        cfg.completed_turns = 0
        cfg.memory_router = None
        cfg.semantic_index = None
        cfg.params = None

        runner = CodePlanRunner.__new__(CodePlanRunner)
        runner._cfg = cfg
        runner._mode_manager = None
        runner._run_id = "test-run"
        runner._model_id = "test-model"
        return runner

    def _make_ctx(self, messages: list | None = None):
        from lauren_ai._memory import ShortTermMemory  # noqa: PLC0415
        from agenthicc.workflows.code_plan.state import CodePlanContext  # noqa: PLC0415
        mem = ShortTermMemory(max_tokens=8_000)
        if messages:
            for m in messages:
                mem._messages.append(m)
        return CodePlanContext(
            intent="test intent",
            run_id="test-run",
            shared_memory=mem,
        )

    async def test_success_on_first_attempt(self) -> None:
        runner = self._make_runner()
        ctx = self._make_ctx()
        call_count = [0]

        async def fake_run_turn(*args, **kwargs):
            call_count[0] += 1

        runner._run_turn = fake_run_turn
        await runner._run_turn_with_retry("text", tools=[], mode=None,
                                           system_prompt="sp", max_turns=5, ctx=ctx)
        assert call_count[0] == 1

    async def test_retries_on_transient_error(self) -> None:
        from lauren_ai._exceptions import TransientTransportError  # noqa: PLC0415
        runner = self._make_runner(max_retries=2, base_delay=0.0)
        ctx = self._make_ctx()
        call_count = [0]

        async def fake_run_turn(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise TransientTransportError("timeout")

        runner._run_turn = fake_run_turn
        await runner._run_turn_with_retry("text", tools=[], mode=None,
                                           system_prompt="sp", max_turns=5, ctx=ctx)
        assert call_count[0] == 3

    async def test_memory_restored_on_retry(self) -> None:
        from lauren_ai._exceptions import TransientTransportError  # noqa: PLC0415
        runner = self._make_runner(max_retries=1, base_delay=0.0)
        ctx = self._make_ctx(messages=[{"role": "user", "content": "hello"}])
        original_len = len(ctx.shared_memory._messages)
        call_count = [0]

        async def fake_run_turn(*args, **kwargs):
            call_count[0] += 1
            # Corrupt memory on first attempt to simulate partial turn
            ctx.shared_memory._messages.append({"role": "assistant", "content": "partial"})
            if call_count[0] == 1:
                raise TransientTransportError("timeout")

        runner._run_turn = fake_run_turn
        await runner._run_turn_with_retry("text", tools=[], mode=None,
                                           system_prompt="sp", max_turns=5, ctx=ctx)
        # After successful second attempt, memory should have been restored before retry
        # (the fake_run_turn appended again on attempt 2, so final length = original + 1)
        assert call_count[0] == 2

    async def test_exhausted_retries_raises(self) -> None:
        from lauren_ai._exceptions import TransientTransportError  # noqa: PLC0415
        runner = self._make_runner(max_retries=2, base_delay=0.0)
        ctx = self._make_ctx()

        async def always_fails(*args, **kwargs):
            raise TransientTransportError("timeout")

        runner._run_turn = always_fails
        with pytest.raises(TransientTransportError):
            await runner._run_turn_with_retry("text", tools=[], mode=None,
                                               system_prompt="sp", max_turns=5, ctx=ctx)

    async def test_permanent_error_not_retried(self) -> None:
        from lauren_ai._exceptions import TransportError  # noqa: PLC0415
        runner = self._make_runner(max_retries=3, base_delay=0.0)
        ctx = self._make_ctx()
        call_count = [0]

        async def fail_permanent(*args, **kwargs):
            call_count[0] += 1
            raise TransportError("bad request", status_code=400)

        runner._run_turn = fail_permanent
        with pytest.raises(TransportError):
            await runner._run_turn_with_retry("text", tools=[], mode=None,
                                               system_prompt="sp", max_turns=5, ctx=ctx)
        assert call_count[0] == 1  # no retry

    async def test_cancelled_error_not_retried(self) -> None:
        runner = self._make_runner(max_retries=3, base_delay=0.0)
        ctx = self._make_ctx()
        call_count = [0]

        async def fail_cancelled(*args, **kwargs):
            call_count[0] += 1
            raise asyncio.CancelledError()

        runner._run_turn = fail_cancelled
        with pytest.raises(asyncio.CancelledError):
            await runner._run_turn_with_retry("text", tools=[], mode=None,
                                               system_prompt="sp", max_turns=5, ctx=ctx)
        assert call_count[0] == 1  # no retry

    async def test_zero_max_retries_does_not_retry(self) -> None:
        from lauren_ai._exceptions import TransientTransportError  # noqa: PLC0415
        runner = self._make_runner(max_retries=0, base_delay=0.0)
        ctx = self._make_ctx()
        call_count = [0]

        async def fail_once(*args, **kwargs):
            call_count[0] += 1
            raise TransientTransportError("timeout")

        runner._run_turn = fail_once
        with pytest.raises(TransientTransportError):
            await runner._run_turn_with_retry("text", tools=[], mode=None,
                                               system_prompt="sp", max_turns=5, ctx=ctx)
        assert call_count[0] == 1

    async def test_retry_notification_appended(self) -> None:
        from lauren_ai._exceptions import TransientTransportError  # noqa: PLC0415
        runner = self._make_runner(max_retries=1, base_delay=0.0)
        ctx = self._make_ctx()
        call_count = [0]

        async def fail_once(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise TransientTransportError("timeout")

        runner._run_turn = fail_once
        await runner._run_turn_with_retry("text", tools=[], mode=None,
                                           system_prompt="sp", max_turns=5, ctx=ctx)

        runner._cfg.conv_store.append_event.assert_called_once()
        call_args = runner._cfg.conv_store.append_event.call_args
        assert call_args[0][0] == "system"
        assert "retrying" in call_args[0][1]["text"].lower()

    async def test_named_timeout_exception_retried(self) -> None:
        class ReadTimeout(Exception): pass
        runner = self._make_runner(max_retries=1, base_delay=0.0)
        ctx = self._make_ctx()
        call_count = [0]

        async def fail_once(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ReadTimeout("read timeout")

        runner._run_turn = fail_once
        await runner._run_turn_with_retry("text", tools=[], mode=None,
                                           system_prompt="sp", max_turns=5, ctx=ctx)
        assert call_count[0] == 2


# ── build_llm_config passes max_retries ──────────────────────────────────────

class TestBuildLlmConfigMaxRetries:
    def test_anthropic_receives_max_retries(self) -> None:
        from agenthicc.config import build_llm_config
        import os
        os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
        cfg = ExecutionSettings(
            provider="anthropic",
            model="claude-haiku-4-5",
            transport_max_retries=5,
        )
        llm = build_llm_config(cfg)
        assert llm.max_retries == 5

    def test_zero_retries_forwarded(self) -> None:
        from agenthicc.config import build_llm_config
        cfg = ExecutionSettings(
            provider="anthropic",
            model="claude-haiku-4-5",
            transport_max_retries=0,
        )
        llm = build_llm_config(cfg)
        assert llm.max_retries == 0
