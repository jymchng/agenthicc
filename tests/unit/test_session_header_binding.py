"""Unit coverage for dynamic provider session-header bindings (PRD-187)."""

from __future__ import annotations

import textwrap

import pytest

from agenthicc.config import (
    ExecutionSettings,
    RequestOptionSettings,
    build_llm_config,
    load_config,
)
from agenthicc.runners.agent_turn import (
    _is_transient_network_error,
    _missing_opencode_session_diagnostic,
    _provider_failure_metadata,
)

pytestmark = pytest.mark.unit


def _write(path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _opencode_config() -> str:
    return """
    [execution]
    profile = "opencode_go"

    [providers.opencode_go]
    provider = "openai"
    protocol = "opencode-go"
    model = "kimi-k3"
    base_url = "https://opencode.ai/zen/go/v1"
    api_key = { env = "OPENCODE_API_KEY" }
    session_header = "x-opencode-session"

    [providers.opencode_go.default_headers]
    "X-Gateway" = "gateway"
    """


def test_profile_binds_the_stable_conversation_id_to_llm_headers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _opencode_config())
    monkeypatch.setenv("OPENCODE_API_KEY", "secret")

    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    resolved = config.resolve_provider_profile()
    assert resolved is not None
    assert resolved.session_header == "x-opencode-session"

    llm = build_llm_config(config.execution, conversation_id="session-abc")

    assert llm.default_headers == {
        "X-Gateway": "gateway",
        "x-opencode-session": "session-abc",
    }
    assert llm.base_url == "https://opencode.ai/zen/go/v1"
    redacted = repr(config.redacted_dict())
    assert "x-opencode-session" in redacted
    assert "session-abc" not in redacted
    assert "secret" not in redacted


def test_official_opencode_go_automatically_binds_agenthicc_session_id(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(path, _opencode_config().replace('session_header = "x-opencode-session"', ""))
    monkeypatch.setenv("OPENCODE_API_KEY", "secret")

    config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
    config.resolve_provider_profile()

    llm = build_llm_config(config.execution, conversation_id="stable-session-id")

    assert llm.default_headers["x-opencode-session"] == "stable-session-id"


def test_official_opencode_go_rejects_conflicting_explicit_header() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        base_url="https://opencode.ai/zen/go/v1",
        session_header="X-Conversation-ID",
    )

    with pytest.raises(ValueError, match="requires x-opencode-session"):
        build_llm_config(settings, conversation_id="stable-session-id")


def test_custom_opencode_compatible_gateway_keeps_opt_in_header_behavior() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        base_url="https://gateway.example.test/v1",
        default_headers={"X-Gateway": "gateway"},
    )

    llm = build_llm_config(settings, conversation_id="stable-session-id")

    assert llm.default_headers == {"X-Gateway": "gateway"}


def test_legacy_execution_setting_can_bind_a_custom_header() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        session_header="X-Conversation-ID",
    )

    first = build_llm_config(settings, conversation_id="conversation-1")
    second = build_llm_config(settings, conversation_id="conversation-2")

    assert first.default_headers["X-Conversation-ID"] == "conversation-1"
    assert second.default_headers["X-Conversation-ID"] == "conversation-2"


def test_no_binding_preserves_existing_header_behavior() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        default_headers={"X-Static": "value"},
    )

    llm = build_llm_config(settings, conversation_id="conversation-1")

    assert llm.default_headers == {"X-Static": "value"}


def test_dynamic_header_requires_the_session_identity() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        session_header="x-opencode-session",
    )

    with pytest.raises(ValueError, match="conversation_id"):
        build_llm_config(settings)


def test_dynamic_header_rejects_case_insensitive_static_collision() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        default_headers={"X-OpenCode-Session": "wrong"},
        session_header="x-opencode-session",
    )

    with pytest.raises(ValueError, match="conflicts with static header"):
        build_llm_config(settings, conversation_id="conversation-1")


def test_dynamic_header_rejects_request_option_collision() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        request_options=RequestOptionSettings.from_mapping(
            {"extra_headers": {"X-OpenCode-Session": "wrong"}},
            path="execution.request_options",
        ),
        session_header="x-opencode-session",
    )

    with pytest.raises(ValueError, match="request_options"):
        build_llm_config(settings, conversation_id="conversation-1")


@pytest.mark.parametrize(
    "session_header",
    ["bad header", ""],
)
def test_profile_validates_dynamic_header_name(tmp_path, session_header: str) -> None:
    path = tmp_path / "agenthicc.toml"
    _write(
        path,
        f"""
        [execution]
        profile = "gateway"
        [providers.gateway]
        provider = "openai"
        model = "gateway-model"
        session_header = "{session_header}"
        """,
    )

    if not session_header:
        # Empty is the documented opt-out and is intentionally valid.
        config = load_config(project_path=path, user_path=tmp_path / "missing.toml")
        assert config.providers["gateway"].session_header == ""
    else:
        with pytest.raises(ValueError, match="header name"):
            load_config(project_path=path, user_path=tmp_path / "missing.toml")


def test_dynamic_header_rejects_control_characters_when_constructed_directly() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        session_header="x\r\nbad",
    )

    with pytest.raises(ValueError, match="header name"):
        build_llm_config(settings, conversation_id="conversation-1")


@pytest.mark.parametrize(
    "conversation_id", ["", ".", "..", "a/b", "a\\b", "a\x00b", "a\nb", "\ud800"]
)
def test_session_identity_rejects_unsafe_values(conversation_id: str) -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        session_header="x-opencode-session",
    )

    with pytest.raises(ValueError, match="conversation_id"):
        build_llm_config(settings, conversation_id=conversation_id)


def test_session_identity_has_a_bounded_utf8_length() -> None:
    settings = ExecutionSettings(
        provider="openai",
        model="gateway-model",
        api_key="secret",
        session_header="x-opencode-session",
    )

    with pytest.raises(ValueError, match="256 UTF-8 bytes"):
        build_llm_config(settings, conversation_id="é" * 129)


def test_missing_session_error_is_actionable_and_not_retryable() -> None:
    class MissingSessionError(Exception):
        status_code = 400

    error = MissingSessionError(
        "Error from provider: MissingSessionID; request is missing x-opencode-session"
    )

    diagnostic = _missing_opencode_session_diagnostic(error)

    assert diagnostic is not None
    assert "Agenthicc binds that header" in diagnostic
    assert "proxy preserve the header" in diagnostic
    assert _is_transient_network_error(error) is False


def test_provider_failure_projection_keeps_safe_ids_and_omits_payload_text() -> None:
    from types import SimpleNamespace

    class ProviderError(Exception):
        status_code = 400
        type = "invalid_request_error"
        code = "missing_session_id"
        request_id = "req-123"

    prompt = "private prompt with secret sk-ant-12345678901234567890"
    error = ProviderError(f"request echo: {prompt}")
    context = SimpleNamespace(
        conversation_id="session-abc",
        resume_turn_id="turn-xyz",
        exec_cfg=SimpleNamespace(profile="opencode_go"),
        provider_diagnostic_context={
            "background_attempt": 4,
            "phase": "recovering_prior_turn",
            "interrupted_turn_id": "turn-xyz",
            "input_message_id": "input-456",
        },
    )

    metadata = _provider_failure_metadata(
        error,
        context,  # type: ignore[arg-type]
        provider="openai",
        model="kimi-k3",
        turn_id="turn-xyz",
    )

    assert metadata == {
        "provider": "openai",
        "model": "kimi-k3",
        "session_id": "session-abc",
        "turn_id": "turn-xyz",
        "exception_type": "ProviderError",
        "profile": "opencode_go",
        "http_status": 400,
        "provider_error_type": "invalid_request_error",
        "provider_error_code": "missing_session_id",
        "provider_request_id": "req-123",
        "background_attempt": 4,
        "phase": "recovering_prior_turn",
        "interrupted_turn_id": "turn-xyz",
        "input_message_id": "input-456",
    }
    assert prompt not in repr(metadata)
    assert "sk-ant-" not in repr(metadata)


def test_background_provider_status_is_redacted_but_correlatable() -> None:
    from types import SimpleNamespace

    from agenthicc.background.worker import WorkerRequest, _safe_provider_failure_message

    class ProviderError(Exception):
        status_code = 400
        type = "invalid_request_error"
        code = "invalid_payload"
        request_id = "req-safe-123"

    private_text = "private prompt sk-ant-12345678901234567890"
    error = ProviderError(f"upstream echoed {private_text}")
    session = SimpleNamespace(
        cfg=SimpleNamespace(
            execution=SimpleNamespace(
                provider="openai",
                model="kimi-k3",
                profile="opencode_go",
            )
        )
    )
    request = WorkerRequest(
        session_id="session-abc",
        workflow_name="",
        intent=private_text,
        cwd="/tmp/worker",
        config_path=None,
        set_overrides=(),
        dangerously_skip_permissions=False,
    )

    message = _safe_provider_failure_message(
        session,
        error,
        request=request,
        attempt=7,
        diagnostic_context={
            "phase": "recovering_prior_turn",
            "interrupted_turn_id": "turn-12",
            "input_message_id": "input-13",
        },
    )

    assert message is not None
    assert all(
        value in message
        for value in (
            "HTTP 400",
            "provider=openai",
            "model=kimi-k3",
            "profile=opencode_go",
            "code=invalid_payload",
            "request_id=req-safe-123",
            "phase=recovering_prior_turn",
            "interrupted_turn=turn-12",
            "input=input-13",
        )
    )
    assert private_text not in message
    assert "sk-ant-" not in message
