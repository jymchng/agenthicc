"""TUX-05 AC6 and AC7: ``/model`` changes the provider and model without a restart.

Two ``MockTransport``s stand for two providers. The session's real lazy runner
and its real ``build_llm_config`` factory run; only lauren-ai's transport
constructor is replaced, so each configuration reaches its own transport.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from pathlib import Path

import pytest
from lauren_ai._config import LLMConfig
from lauren_ai._memory import ShortTermMemory
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.commands import build_builtin_registry
from agenthicc.config import AgenthiccConfig, build_llm_config
from agenthicc.kernel import AppState as KernelAppState
from agenthicc.kernel import EventProcessor, SecurityPolicy, SystemSettings
from agenthicc.runners.model_switch import model_choices
from agenthicc.runners.tui_session import TUISession, _LazyAgentRunner
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.conversation_store import ConversationEvent
from agenthicc.tui.workspace.overlays.choice_picker import ChoicePickerOverlay
from tests.unit.test_tui_session_coverage import _make_session

pytestmark = pytest.mark.e2e

ANTHROPIC_KEY = "sk-ant-tux05-secret-one"
OPENAI_KEY = "sk-tux05-secret-two"


def _completion(text: str, n: int) -> Completion:
    return Completion(
        id=f"c{n}",
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


def _user_texts(transport: MockTransport) -> list[str]:
    """Every user message each request to *transport* carried."""
    texts: list[str] = []
    for call in transport.calls:
        for message in call.messages:
            role = message.get("role") if isinstance(message, dict) else message.role
            content = message.get("content") if isinstance(message, dict) else message.content
            if role == "user" and isinstance(content, str):
                texts.append(content)
    return texts


class _Harness:
    """A session whose provider transports are two ``MockTransport``s."""

    def __init__(
        self,
        session: TUISession,
        events: list[ConversationEvent],
        first: MockTransport,
        second: MockTransport,
        built: list[LLMConfig],
    ) -> None:
        self.session = session
        self.events = events
        self.first = first
        self.second = second
        self.built = built


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    monkeypatch.setattr("agenthicc.runners.session_tools.session_tools", lambda *a, **k: [])
    first, second = MockTransport(), MockTransport()
    first.queue_response(_completion("answer from the first model", 1))
    second.queue_response(_completion("answer from the second model", 2))
    built: list[LLMConfig] = []

    def build_transport(config: LLMConfig) -> MockTransport:
        built.append(config)
        return first if config.provider == "anthropic" else second

    monkeypatch.setattr("lauren_ai._module._build_transport", build_transport)

    session, ctx, _workspace, _input = _make_session()
    cfg = AgenthiccConfig()
    cfg.execution.provider = "anthropic"
    cfg.execution.model = "first-model"
    cfg.execution.auto_compact = False
    ctx.cfg = cfg
    for command in build_builtin_registry():
        ctx.cmd_registry.register(command)
    ctx.model_label = "anthropic/first-model"
    ctx.agent_runner = _LazyAgentRunner(
        partial(build_llm_config, cfg.execution, conversation_id=ctx.session_id),
        cassette_dir=None,
    )
    kernel = KernelAppState.create(
        settings=SystemSettings(
            event_log_path=str(tmp_path / "events.jsonl"),
            snapshot_path=str(tmp_path / "snapshot.json"),
        ),
        policy=SecurityPolicy(),
    )
    ctx.processor = EventProcessor(initial_state=kernel, persist=False)
    ctx.session_memory = ShortTermMemory(max_tokens=20_000)
    processor_task = asyncio.create_task(ctx.processor.run())
    events: list[ConversationEvent] = []
    ctx.app_state.conversation.on_event(events.append)
    try:
        yield _Harness(session, events, first, second, built)
    finally:
        processor_task.cancel()
        await asyncio.gather(processor_task, return_exceptions=True)


def _open_model_picker(session: TUISession) -> ChoicePickerOverlay:
    assert session.dispatch_slash("/model")
    overlay = session._workspace.overlays.widget
    assert isinstance(overlay, ChoicePickerOverlay)
    return overlay


def _choose(overlay: ChoicePickerOverlay, provider: str) -> None:
    """Move the selection to *provider*'s default row and press Enter."""
    rows = overlay.picker.rows
    index = next(i for i, row in enumerate(rows) if row.value.provider == provider)
    overlay.picker.selected = index
    overlay.handle_key(Key.ENTER, "")


async def test_the_next_turn_goes_to_the_second_transport_with_one_model_changed_event(
    harness: _Harness,
) -> None:
    session = harness.session
    await session.run_turn("question one")
    assert _user_texts(harness.first) == ["question one"]
    assert harness.second.calls == []

    overlay = _open_model_picker(session)
    _choose(overlay, "openai")
    await session.run_turn("question two")

    assert "question two" in _user_texts(harness.second)
    assert "question two" not in _user_texts(harness.first)
    assert len(harness.first.calls) == 1
    assert [config.provider for config in harness.built] == ["anthropic", "openai"]
    changed = [event for event in harness.events if event.kind == "model_changed"]
    assert len(changed) == 1
    assert changed[0].payload["provider"] == "openai"
    assert session._ctx.model_label.startswith("openai/")
    assert session._ctx.cfg.execution.provider == "openai"
    # Workflow runs are built from the session's base config, which follows the switch.
    assert session._wf_config_base.agent_runner is session._ctx.agent_runner


async def test_no_restart_text_and_no_credential_in_any_event(harness: _Harness) -> None:
    session = harness.session
    overlay = _open_model_picker(session)
    _choose(overlay, "openai")
    await session.run_turn("hello")
    dump = json.dumps([event.payload for event in harness.events], default=str)
    assert "restart" not in dump.lower()
    assert ANTHROPIC_KEY not in dump and OPENAI_KEY not in dump
    assert "restart" not in (overlay.status or "").lower()


async def test_a_model_chosen_during_a_turn_applies_when_the_turn_ends(harness: _Harness) -> None:
    session = harness.session
    release = asyncio.Event()

    async def running() -> None:
        await release.wait()

    session._agent_task = asyncio.create_task(running())
    choice = next(c for c in model_choices(session._ctx.cfg) if c.provider == "openai")
    message = session.switch_model(choice)
    assert "after this turn" in message
    assert session._ctx.cfg.execution.provider == "anthropic"
    assert [event for event in harness.events if event.kind == "model_changed"] == []
    release.set()
    await session._agent_task
    session.advance()
    assert session._ctx.cfg.execution.provider == "openai"
    assert len([event for event in harness.events if event.kind == "model_changed"]) == 1


async def test_a_provider_without_credentials_cannot_be_chosen(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    session = harness.session
    overlay = _open_model_picker(session)
    row = next(r for r in overlay.picker.rows if r.value.provider == "openai")
    assert row.disabled and "OPENAI_API_KEY" in row.disabled
    _choose(overlay, "openai")
    assert "OPENAI_API_KEY" in overlay.status
    assert session._ctx.cfg.execution.provider == "anthropic"
    assert [event for event in harness.events if event.kind == "model_changed"] == []
    assert harness.built == []
    # The same command with arguments refuses too, and names the variable.
    assert session.dispatch_slash("/model openai")
    assert session._ctx.cfg.execution.provider == "anthropic"
    assert OPENAI_KEY not in session._ctx.console.export_text()
