"""Unit tests for PRD-126 — transport retry with memory rollback."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

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
        class ReadTimeout(Exception):
            pass

        assert _is_transient_network_error(ReadTimeout())

    def test_connect_timeout_by_name_is_transient(self) -> None:
        class ConnectTimeout(Exception):
            pass

        assert _is_transient_network_error(ConnectTimeout())

    def test_connect_error_by_name_is_transient(self) -> None:
        class ConnectError(Exception):
            pass

        assert _is_transient_network_error(ConnectError())

    def test_network_error_by_name_is_transient(self) -> None:
        class NetworkError(Exception):
            pass

        assert _is_transient_network_error(NetworkError())

    def test_builtin_timeout_error_NOT_transient(self) -> None:
        # PRD-126 gap 5: bare builtin TimeoutError IS asyncio.TimeoutError in
        # 3.11+, so it must NOT be auto-retried (would mask wait_for timeouts).
        assert not _is_transient_network_error(TimeoutError())

    def test_api_timeout_error_is_transient(self) -> None:
        class APITimeoutError(Exception):
            pass

        assert _is_transient_network_error(APITimeoutError())

    def test_api_connection_error_is_transient(self) -> None:
        class APIConnectionError(Exception):
            pass

        assert _is_transient_network_error(APIConnectionError())

    def test_pool_timeout_is_transient(self) -> None:
        class PoolTimeout(Exception):
            pass

        assert _is_transient_network_error(PoolTimeout())

    def test_transient_via_cause_chain(self) -> None:
        class ReadTimeout(Exception):
            pass

        wrapper = Exception("wrapped")
        wrapper.__cause__ = ReadTimeout()
        assert _is_transient_network_error(wrapper)

    def test_transient_via_context_chain(self) -> None:
        class ConnectError(Exception):
            pass

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
        class ReadTimeout(Exception):
            pass

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


# ── run_with_transport_retry (shared helper) ──────────────────────────────────


class _FakeMemory:
    """Minimal ShortTermMemory-like object with snapshot/restore."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._snapshots: list[list[str]] = []

    def snapshot(self):
        return list(self.messages)

    def restore(self, snap) -> None:
        self.messages = list(snap)


class TestRunWithTransportRetry:
    """Tests for the shared agenthicc.runners.retry.run_with_transport_retry."""

    def _config(self, max_retries=2, base_delay=0.0, max_total=0.0):
        from agenthicc.runners.retry import RetryConfig

        return RetryConfig(
            max_retries=max_retries,
            base_delay_s=base_delay,
            max_total_duration_s=max_total,
            jitter=False,
        )

    async def test_success_on_first_attempt(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry

        calls = [0]

        async def fn():
            calls[0] += 1

        await run_with_transport_retry(fn, config=self._config())
        assert calls[0] == 1

    async def test_retries_on_transient_error(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def fn():
            calls[0] += 1
            if calls[0] < 3:
                raise TransientTransportError("timeout")

        await run_with_transport_retry(fn, config=self._config(max_retries=2))
        assert calls[0] == 3

    async def test_memory_restored_on_retry(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        mem = _FakeMemory()
        mem.messages = ["user-intent"]
        calls = [0]

        async def fn():
            calls[0] += 1
            mem.messages.append("partial-assistant")  # corrupt
            if calls[0] == 1:
                raise TransientTransportError("timeout")

        await run_with_transport_retry(fn, config=self._config(max_retries=1), memory=mem)
        # attempt 1 appended then failed → restored to ["user-intent"];
        # attempt 2 appended once and succeeded.
        assert mem.messages == ["user-intent", "partial-assistant"]
        assert calls[0] == 2

    async def test_exhausted_retries_raises(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        async def fn():
            raise TransientTransportError("timeout")

        with pytest.raises(TransientTransportError):
            await run_with_transport_retry(fn, config=self._config(max_retries=2))

    async def test_permanent_error_not_retried(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransportError

        calls = [0]

        async def fn():
            calls[0] += 1
            raise TransportError("bad", status_code=400)

        with pytest.raises(TransportError):
            await run_with_transport_retry(fn, config=self._config(max_retries=3))
        assert calls[0] == 1

    async def test_cancelled_error_not_retried(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry

        calls = [0]

        async def fn():
            calls[0] += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_with_transport_retry(fn, config=self._config(max_retries=3))
        assert calls[0] == 1

    async def test_zero_max_retries_does_not_retry(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def fn():
            calls[0] += 1
            raise TransientTransportError("timeout")

        with pytest.raises(TransientTransportError):
            await run_with_transport_retry(fn, config=self._config(max_retries=0))
        assert calls[0] == 1

    async def test_on_retry_callback_invoked(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        events = []
        calls = [0]

        async def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise TransientTransportError("timeout")

        async def on_retry(attempt, max_r, delay, exc):
            events.append((attempt, max_r))

        await run_with_transport_retry(fn, config=self._config(max_retries=2), on_retry=on_retry)
        assert events == [(1, 2)]

    async def test_sync_on_retry_callback(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        events = []
        calls = [0]

        async def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise TransientTransportError("timeout")

        def on_retry(attempt, max_r, delay, exc):  # sync callback
            events.append(attempt)

        await run_with_transport_retry(fn, config=self._config(max_retries=2), on_retry=on_retry)
        assert events == [1]

    async def test_reset_fns_called_on_retry(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        reset_calls = [0]
        calls = [0]

        async def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise TransientTransportError("timeout")

        def reset():
            reset_calls[0] += 1

        await run_with_transport_retry(fn, config=self._config(max_retries=2), reset_fns=[reset])
        assert reset_calls[0] == 1

    async def test_max_total_duration_cap(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def fn():
            calls[0] += 1
            raise TransientTransportError("timeout")

        # base_delay 10s, cap 5s → first delay (10s) exceeds remaining budget → raise
        with pytest.raises(TransientTransportError):
            await run_with_transport_retry(
                fn, config=self._config(max_retries=5, base_delay=10.0, max_total=5.0)
            )
        assert calls[0] == 1  # no retry: delay would exceed cap

    async def test_deadline_skips_retry(self) -> None:
        import time
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def fn():
            calls[0] += 1
            raise TransientTransportError("timeout")

        # deadline only 0.1s away → no meaningful window for a retry
        deadline = time.monotonic() + 0.1
        with pytest.raises(TransientTransportError):
            await run_with_transport_retry(
                fn, config=self._config(max_retries=3, base_delay=1.0), deadline_monotonic=deadline
            )
        assert calls[0] == 1

    async def test_named_timeout_retried(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry

        class ReadTimeout(Exception):
            pass

        calls = [0]

        async def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise ReadTimeout("read timeout")

        await run_with_transport_retry(fn, config=self._config(max_retries=1))
        assert calls[0] == 2

    async def test_no_memory_still_retries(self) -> None:
        from agenthicc.runners.retry import run_with_transport_retry
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def fn():
            calls[0] += 1
            if calls[0] == 1:
                raise TransientTransportError("timeout")

        await run_with_transport_retry(fn, config=self._config(max_retries=1), memory=None)
        assert calls[0] == 2


# ── build_llm_config uses llm_sdk_max_retries (gap 4) ─────────────────────────


class TestBuildLlmConfigMaxRetries:
    def test_anthropic_receives_sdk_retries_not_turn_retries(self) -> None:
        from agenthicc.config import build_llm_config
        import os

        os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
        cfg = ExecutionSettings(
            provider="anthropic",
            model="claude-haiku-4-5",
            llm_sdk_max_retries=2,
            transport_max_retries=9,  # turn-level — must NOT leak to the SDK
        )
        llm = build_llm_config(cfg)
        assert llm.max_retries == 2  # SDK retries, not the turn-level 9

    def test_zero_sdk_retries_forwarded(self) -> None:
        from agenthicc.config import build_llm_config

        cfg = ExecutionSettings(
            provider="anthropic",
            model="claude-haiku-4-5",
            llm_sdk_max_retries=0,
        )
        llm = build_llm_config(cfg)
        assert llm.max_retries == 0

    def test_default_sdk_retries_is_two(self) -> None:
        from agenthicc.config import build_llm_config

        cfg = ExecutionSettings(provider="anthropic", model="claude-haiku-4-5")
        llm = build_llm_config(cfg)
        assert llm.max_retries == 2


# ── ExecutionSettings gap-fix fields ──────────────────────────────────────────


class TestNewConfigFields:
    def test_llm_sdk_max_retries_default(self) -> None:
        assert ExecutionSettings().llm_sdk_max_retries == 2

    def test_transport_retry_max_total_default(self) -> None:
        assert ExecutionSettings().transport_retry_max_total_s == 0.0

    def test_toml_override_sdk_retries(self) -> None:
        cfg = _dict_to_config({"execution": {"llm_sdk_max_retries": 0}})
        assert cfg.execution.llm_sdk_max_retries == 0

    def test_toml_override_max_total(self) -> None:
        cfg = _dict_to_config({"execution": {"transport_retry_max_total_s": 120.0}})
        assert cfg.execution.transport_retry_max_total_s == 120.0


# ── Subagent worker retry (gap 3) ─────────────────────────────────────────────


class _FakeSubRunner:
    """Stand-in for the per-worker AgentRunnerBase built inside _execute."""

    def __init__(self, behavior) -> None:
        self._behavior = behavior

    async def run(self, agent, text, *, memory, config_override):  # noqa: ANN001
        return await self._behavior(memory, text)


class TestSubagentWorkerRetry:
    def _worker(self, retry_config):
        from agenthicc.subagents.pool import SubagentWorker, SubagentTask
        from agenthicc.subagents.types import DEFAULT_REGISTRY

        spec = DEFAULT_REGISTRY.get("explorer")
        task = SubagentTask("t1", "explorer", "find files")

        class _Parent:
            _transport = object()

        return SubagentWorker(
            task=task,
            spec=spec,
            index=1,
            parent_runner=_Parent(),
            parent_model="m",
            all_tools=[],
            retry_config=retry_config,
        )

    async def test_worker_retries_transient_error(self) -> None:
        from agenthicc.runners.retry import RetryConfig
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def behavior(memory, text):
            calls[0] += 1
            memory._messages.append({"role": "user", "content": text})
            if calls[0] == 1:
                raise TransientTransportError("timeout")

            class _Resp:
                content = "found 3 files"

            return _Resp()

        worker = self._worker(RetryConfig(max_retries=1, base_delay_s=0.0, jitter=False))
        with patch(
            "lauren_ai._agents._runner.AgentRunnerBase",
            return_value=_FakeSubRunner(behavior),
        ):
            result = await worker.run()
        assert result.ok
        assert "found 3 files" in result.text
        assert calls[0] == 2

    async def test_worker_no_retry_config_fails_once(self) -> None:
        from lauren_ai._exceptions import TransientTransportError

        calls = [0]

        async def behavior(memory, text):
            calls[0] += 1
            raise TransientTransportError("timeout")

        worker = self._worker(retry_config=None)
        with patch(
            "lauren_ai._agents._runner.AgentRunnerBase",
            return_value=_FakeSubRunner(behavior),
        ):
            result = await worker.run()
        assert not result.ok
        assert calls[0] == 1
