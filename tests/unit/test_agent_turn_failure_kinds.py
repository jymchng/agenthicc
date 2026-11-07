"""TUX-08: a failed agent turn publishes a classified, redacted error event."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from agenthicc.config import ExecutionSettings
from agenthicc.runners.agent_turn import AgentTurnRunner
from agenthicc.runners.agent_turn_context import AgentTurnContext
from agenthicc.runners.failure_kinds import FAILURE_ATTRIBUTE

pytestmark = pytest.mark.unit


async def _sdk_missing_key_error() -> BaseException:
    """The real error the installed Anthropic SDK raises with no key at all."""
    saved = {
        name: os.environ.pop(name, None) for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }
    try:
        client = anthropic.AsyncAnthropic(api_key=None)
        try:
            await client.messages.create(
                model="claude-x", max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )
        except Exception as exc:  # noqa: BLE001 - the SDK's real error is the input
            return exc
        raise AssertionError("no error without a key")
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


async def _fail_turn(error: BaseException, *, provider: str = "anthropic") -> dict[str, object]:
    conv_store = MagicMock()
    transport = SimpleNamespace(_config=SimpleNamespace(provider=provider, timeout=600.0))
    ctx = AgentTurnContext(
        text="hello",
        runner=SimpleNamespace(_transport=transport),  # type: ignore[arg-type]
        processor=MagicMock(),
        session_memory=None,
        max_agent_turns=1,
        conv_store=conv_store,
        app_state=None,
        exec_cfg=ExecutionSettings(irrecoverable_error_max_retries=0, transport_max_retries=0),
        skills={},
        mention_cache=MagicMock(),
        project_plugin_tools=[],
        mcp_registry=None,
        active_agent="auto",
        completed_turns=0,
        approval_svc=None,
        output_collector=None,
        system_prompt_suffix="",
        conversation_id="session-1",
    )
    runner = AgentTurnRunner(ctx)
    runner._turn_active = True
    runner._model_id = "claude-x"
    mock_runner = MagicMock()
    mock_runner.run_stream = AsyncMock(side_effect=error)
    with pytest.raises(type(error)) as raised:
        await runner._stream(MagicMock(), "hello", mock_runner)
    assert raised.value is error
    calls = [call for call in conv_store.append_event.call_args_list if call.args[0] == "error"]
    assert len(calls) == 1
    payload = calls[0].args[1]
    assert isinstance(payload, dict)
    return payload


async def test_a_missing_key_is_reported_as_missing_credentials_without_sdk_text() -> None:
    error = await _sdk_missing_key_error()
    payload = await _fail_turn(error)

    assert payload["kind"] == "missing_credentials"
    assert payload["message"] == "No API key for anthropic."
    assert payload["action"] == {
        "hint": "Set ANTHROPIC_API_KEY, or run /model to switch provider.",
        "commands": ["/model"],
    }
    # The redacted metadata stays alongside the kind.
    assert payload["exception_type"] == "TypeError"
    assert payload["provider"] == "anthropic"
    serialized = json.dumps(payload).casefold()
    assert "resolve authentication" not in serialized
    assert "api_key" not in serialized.replace("anthropic_api_key", "")
    # Other owners (headless JSON) read the same kind from the exception.
    assert getattr(error, FAILURE_ATTRIBUTE)["kind"] == "missing_credentials"


async def test_an_unrecognised_failure_is_internal_and_points_at_the_log() -> None:
    payload = await _fail_turn(ValueError("secret prompt fragment"))
    assert payload["kind"] == "internal"
    assert payload["message"] == "The turn failed unexpectedly (ValueError)."
    assert payload["action"] == {
        "hint": "Run /logs to see the diagnostics log at ~/.agenthicc/logs/session-1.log.",
        "commands": ["/logs"],
    }
    assert "secret prompt fragment" not in json.dumps(payload)


async def test_a_rejected_key_names_the_provider() -> None:
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        401,
        request=request,
        json={"type": "error", "error": {"type": "authentication_error", "message": "sk-ant-x"}},
    )
    error = anthropic.AuthenticationError("sk-ant-x", response=response, body=response.json())
    payload = await _fail_turn(error)
    assert payload["kind"] == "rejected_credentials"
    assert payload["message"] == "anthropic rejected the API key."
    assert payload["http_status"] == 401
    assert "sk-ant-x" not in json.dumps(payload)
