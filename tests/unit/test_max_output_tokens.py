"""Unit tests for the per-turn completion ceiling.

lauren-ai defaults ``AgentConfig.max_tokens_per_turn`` to 4096. agenthicc used to
leave that default in place, which silently truncated any single large tool call:
a ``write_file`` carrying a whole source file was cut off mid-argument, the
partial call was discarded, the sub-turn produced nothing, and the calling phase
retried forever with no visible cause. ``ExecutionSettings.max_output_tokens``
makes the ceiling explicit and configurable, and a truncated response now says so.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._config import AgentConfig
from lauren_ai._memory import ShortTermMemory
from lauren_ai._signals import SignalBus
from lauren_ai._transport import Completion, RequestOptions, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.config import AgenthiccConfig, load_config
from agenthicc.kernel import AppState, EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.agent_turn import _run_agent_turn
from agenthicc.tui.conversation_store import AppState as TUIAppState

pytestmark = pytest.mark.unit


# ── configuration ─────────────────────────────────────────────────────────────


def test_the_default_ceiling_is_well_above_the_library_default() -> None:
    """4096 is not enough for a whole source file in one tool call."""
    cfg = AgenthiccConfig()
    assert cfg.execution.max_output_tokens == 32_768
    assert cfg.execution.max_output_tokens > AgentConfig().max_tokens_per_turn


def test_the_ceiling_is_reserved_from_the_live_window() -> None:
    cfg = AgenthiccConfig()
    window = cfg.execution.effective_context_window()
    budget = cfg.execution.effective_usable_budget()
    assert budget == window - cfg.execution.max_output_tokens - max(4_000, window // 25)


def test_raising_the_ceiling_shrinks_the_live_window_by_the_same_amount() -> None:
    cfg = AgenthiccConfig()
    before = cfg.execution.effective_usable_budget()
    cfg.execution.max_output_tokens += 8_000
    assert cfg.execution.effective_usable_budget() == before - 8_000


def test_the_budget_stays_positive_for_an_absurd_ceiling() -> None:
    cfg = AgenthiccConfig()
    cfg.execution.max_output_tokens = 10_000_000
    assert cfg.execution.effective_usable_budget() >= 1


def test_a_non_positive_ceiling_does_not_inflate_the_budget() -> None:
    cfg = AgenthiccConfig()
    cfg.execution.max_output_tokens = 0
    window = cfg.execution.effective_context_window()
    assert cfg.execution.effective_usable_budget() < window


def test_the_ceiling_is_read_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "agenthicc.toml"
    toml.write_text("[execution]\nmax_output_tokens = 32768\n", encoding="utf-8")
    cfg = load_config(project_path=toml, user_path=tmp_path / "missing.toml")
    assert cfg.execution.max_output_tokens == 32_768


def test_the_ceiling_falls_back_to_the_default_for_junk_toml(tmp_path: Path) -> None:
    toml = tmp_path / "agenthicc.toml"
    toml.write_text('[execution]\nmax_output_tokens = "lots"\n', encoding="utf-8")
    cfg = load_config(project_path=toml, user_path=tmp_path / "missing.toml")
    assert cfg.execution.max_output_tokens == 32_768


# ── the ceiling actually reaches the provider ─────────────────────────────────


@pytest.fixture
async def processor(tmp_path: Path):
    kernel_state = AppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "ev.jsonl"),
            snapshot_path=str(tmp_path / "snap.json"),
        ),
        policy=SecurityPolicy(),
    )
    proc = EventProcessor(initial_state=kernel_state, persist=False)
    task = asyncio.create_task(proc.run())
    yield proc
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _completion(stop_reason: str = "end_turn") -> Completion:
    return Completion(
        id="c1",
        model="mock-model",
        content="done",
        tool_calls=[],
        stop_reason=stop_reason,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


async def _turn(processor, app_state, cfg: AgenthiccConfig, mock: MockTransport) -> None:
    await _run_agent_turn(
        "do it",
        runner=AgentRunnerBase(transport=mock, signals=SignalBus()),
        processor=processor,
        session_memory=ShortTermMemory(max_tokens=cfg.execution.effective_usable_budget()),
        max_agent_turns=3,
        conv_store=app_state.conversation,
        app_state=app_state,
        exec_cfg=cfg.execution,
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        approval_svc=None,
        output_collector=[],
    )


async def test_the_configured_ceiling_is_sent_to_the_provider(processor) -> None:
    mock = MockTransport()
    mock.queue_response(_completion())
    cfg = AgenthiccConfig()
    cfg.execution.max_output_tokens = 24_000

    await _turn(processor, TUIAppState.create(), cfg, mock)
    await processor.drain()

    assert mock.calls
    assert mock.calls[0].max_tokens == 24_000


async def test_the_default_ceiling_is_sent_when_nothing_is_configured(processor) -> None:
    mock = MockTransport()
    mock.queue_response(_completion())
    cfg = AgenthiccConfig()

    await _turn(processor, TUIAppState.create(), cfg, mock)
    await processor.drain()

    assert mock.calls[0].max_tokens == 32_768


async def test_profile_sampling_and_request_options_reach_agent_config(processor) -> None:
    mock = MockTransport()
    mock.queue_response(_completion())
    cfg = AgenthiccConfig()
    cfg.execution.temperature = 0.3
    cfg.execution.top_p = 0.95
    cfg.execution.max_completion_tokens = 0
    cfg.execution.request_options = RequestOptions(
        provider={"reasoning_effort": "none"},
        extra_body={"vendor_trace": True},
    )

    await _turn(processor, TUIAppState.create(), cfg, mock)
    await processor.drain()

    call = mock.calls[0]
    assert call.temperature == 0.3
    assert call.top_p == 0.95
    assert call.max_completion_tokens == 0
    assert call.request_options is not None
    assert call.request_options.provider["reasoning_effort"] == "none"
    assert call.request_options.extra_body["vendor_trace"] is True


async def test_a_zero_ceiling_falls_back_to_the_library_default(processor) -> None:
    """A misconfigured zero must not become an unbounded or invalid request."""
    mock = MockTransport()
    mock.queue_response(_completion())
    cfg = AgenthiccConfig()
    cfg.execution.max_output_tokens = 0

    await _turn(processor, TUIAppState.create(), cfg, mock)
    await processor.drain()

    assert mock.calls[0].max_tokens == AgentConfig().max_tokens_per_turn


# ── truncation is reported instead of looking like an idle turn ───────────────


def _collect_system_texts(app_state: TUIAppState) -> list[str]:
    """Subscribe to the conversation store and capture system-event texts."""
    captured: list[str] = []

    def listener(event: object) -> None:
        if getattr(event, "kind", "") == "system":
            payload = getattr(event, "payload", {})
            if isinstance(payload, dict):
                captured.append(str(payload.get("text", "")))

    app_state.conversation.on_event(listener)
    return captured


async def test_a_truncated_response_is_reported_to_the_user(processor) -> None:
    mock = MockTransport()
    mock.queue_response(_completion(stop_reason="max_tokens"))
    app = TUIAppState.create()
    captured = _collect_system_texts(app)
    cfg = AgenthiccConfig()
    cfg.execution.max_output_tokens = 12_345

    await _turn(processor, app, cfg, mock)
    await processor.drain()

    notices = " ".join(captured)
    assert "truncated" in notices
    assert "12345" in notices  # names the ceiling that was hit
    assert "max_output_tokens" in notices  # names the setting to raise
    assert "write_file for the first chunk" in notices
    assert "append_file for each subsequent chunk" in notices
    assert "read the file to verify" in notices


async def test_a_normal_response_reports_no_truncation(processor) -> None:
    mock = MockTransport()
    mock.queue_response(_completion(stop_reason="end_turn"))
    app = TUIAppState.create()
    captured = _collect_system_texts(app)

    await _turn(processor, app, AgenthiccConfig(), mock)
    await processor.drain()

    assert not [text for text in captured if "truncated" in text]
