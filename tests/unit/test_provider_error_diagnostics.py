"""Structured and redacted provider-error diagnostics (PRD-205)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import Request, Response
from openai import BadRequestError
from lauren_ai._exceptions import TransportError

from agenthicc.runners.agent_turn import (
    _format_provider_error,
    _http_status_code,
    _network_retry_detail,
    _provider_error_diagnostic,
)
from agenthicc.runners.tui_session import _fmt_exc
from agenthicc.tui.conversation_store import AppState, ConversationEvent
from agenthicc.tui.workspace.appender import ScrollBufferAppender


pytestmark = pytest.mark.unit


def _wrapped_provider_error(
    body: dict[str, object],
    *,
    request_id: str = "req-123",
    url: str = "https://gateway.example/v1/chat/completions?secret=do-not-show",
) -> Exception:
    response = SimpleNamespace(
        headers={"x-request-id": request_id},
        request=SimpleNamespace(url=url),
    )
    cause = ValueError("BadRequestError")
    cause.status_code = 400  # type: ignore[attr-defined]
    cause.body = body  # type: ignore[attr-defined]
    cause.response = response  # type: ignore[attr-defined]
    outer = Exception("TransportError")
    outer.provider = "openai"  # type: ignore[attr-defined]
    outer.__cause__ = cause
    return outer


def test_extracts_structured_fields_from_nested_sdk_body() -> None:
    error = _wrapped_provider_error(
        {
            "error": {
                "type": "invalid_request_error",
                "code": "model_not_found",
                "param": "model",
                "message": "The model 'missing-model' does not exist.",
            }
        }
    )

    diagnostic = _provider_error_diagnostic(error)

    assert diagnostic is not None
    assert diagnostic.provider == "openai"
    assert diagnostic.status_code == 400
    assert diagnostic.error_type == "invalid_request_error"
    assert diagnostic.code == "model_not_found"
    assert diagnostic.parameter == "model"
    assert diagnostic.request_id == "req-123"
    assert diagnostic.endpoint == "https://gateway.example/v1/chat/completions"
    assert "model_not_found" in diagnostic.detail
    assert "secret" not in diagnostic.detail


def test_extracts_the_real_lauren_ai_openai_exception_body() -> None:
    payload = {
        "error": {
            "type": "invalid_request_error",
            "message": "Upstream request failed: [invalid_request_error] invalid request",
        }
    }
    request = Request("POST", "https://gateway.example/v1/chat/completions")
    response = Response(
        400, request=request, headers={"x-request-id": "real-req-456"}, json=payload
    )
    sdk_error = BadRequestError("Error code: 400 - response", response=response, body=payload)
    error = TransportError(
        str(sdk_error),
        status_code=400,
        provider="openai",
        cause=sdk_error,
    )

    diagnostic = _provider_error_diagnostic(error)

    assert diagnostic is not None
    assert diagnostic.error_type == "invalid_request_error"
    assert diagnostic.message == payload["error"]["message"]
    assert diagnostic.request_id == "real-req-456"
    assert "does not identify the invalid field or value" in _format_provider_error(error)


def test_generic_invalid_request_explains_that_upstream_omitted_the_real_cause() -> None:
    error = _wrapped_provider_error(
        {
            "error": {
                "type": "invalid_request_error",
                "message": "Upstream request failed: [invalid_request_error] invalid request",
            }
        }
    )

    diagnostic = _provider_error_diagnostic(error)

    assert diagnostic is not None
    assert diagnostic.generic_invalid_request is True
    assert "does not identify the invalid field or value" in diagnostic.detail
    assert "message/tool history" in diagnostic.detail
    assert "Upstream request failed" in _network_retry_detail(error)
    assert "query" not in diagnostic.detail


def test_provider_error_format_is_used_by_checkpoint_and_tui_error_text() -> None:
    error = _wrapped_provider_error(
        {
            "error": {
                "type": "invalid_request_error",
                "message": "invalid request",
            }
        }
    )

    formatted = _format_provider_error(error)

    assert formatted is not None
    assert formatted == _fmt_exc(error)
    assert "HTTP 400" in formatted
    assert "Request ID: req-123" in formatted
    assert "does not identify the invalid field" in formatted


def test_response_body_can_be_recovered_when_sdk_only_exposes_response() -> None:
    response = SimpleNamespace(
        headers={"request-id": "response-id"},
        request=SimpleNamespace(url="https://example.test/v1/chat/completions"),
        json=lambda: {
            "error": {
                "type": "invalid_request_error",
                "message": "bad tools",
                "code": "tool_schema_invalid",
            }
        },
    )
    error = Exception("HTTP 400")
    error.status_code = 400  # type: ignore[attr-defined]
    error.response = response  # type: ignore[attr-defined]

    diagnostic = _provider_error_diagnostic(error)

    assert diagnostic is not None
    assert diagnostic.message == "bad tools"
    assert diagnostic.code == "tool_schema_invalid"
    assert diagnostic.request_id == "response-id"


def test_http_status_walks_lauren_ai_cause_attribute() -> None:
    inner = Exception("bad request")
    inner.status_code = 400  # type: ignore[attr-defined]
    outer = Exception("transport")
    outer.cause = inner  # type: ignore[attr-defined]

    assert _http_status_code(outer) == 400


def test_scroll_renderer_shows_structured_provider_details() -> None:
    state = AppState.create()
    from rich.console import Console

    console = Console(record=True, width=120, force_terminal=False)
    appender = ScrollBufferAppender(state, console)
    appender._render_one(
        ConversationEvent(
            "provider-retry",
            "provider_recovery_retry",
            {
                "attempt": 1,
                "max_retries": 5,
                "delay_s": 1.0,
                "detail": "Upstream request failed: invalid request",
                "status_code": 400,
                "provider_diagnostic": {
                    "error_type": "invalid_request_error",
                    "request_id": "req-123",
                    "endpoint": "https://gateway.example/v1/chat/completions",
                    "generic_invalid_request": True,
                },
            },
        )
    )

    rendered = console.export_text()
    assert "type=invalid_request_error" in rendered
    assert "Request ID: req-123" in rendered
    assert "The provider did not identify the invalid field or value." in rendered
