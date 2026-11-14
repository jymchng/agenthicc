"""TUX-08 §2.1: a fixed failure kind for every provider/turn failure, from real SDK errors."""

from __future__ import annotations

import asyncio
import json
import os
import ssl

import anthropic
import httpx
import openai
import pytest

from agenthicc.runners.failure_kinds import (
    FAILURE_ATTRIBUTE,
    MESSAGE_PREFIXES,
    FailureKind,
    classify_turn_failure,
    failure_copy,
    failure_host,
    failure_payload,
    failure_reason,
    reported_failure,
)
from agenthicc.tui.diagnostics import diagnostics_log_path

pytestmark = pytest.mark.unit

_ANTHROPIC = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
_OPENAI = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _anthropic_status(status: int, error_type: str, message: str) -> anthropic.APIStatusError:
    """The SDK's own status-to-class mapping, so every class is the real one."""
    response = httpx.Response(
        status,
        request=_ANTHROPIC,
        json={"type": "error", "error": {"type": error_type, "message": message}},
    )
    client = anthropic.Anthropic(api_key="sk-ant-test")
    return client._make_status_error(message, body=response.json(), response=response)


def _openai_status(
    status: int, message: str, error_type: str = "", code: str | None = None
) -> openai.APIStatusError:
    body = {"message": message, "type": error_type, "code": code}
    response = httpx.Response(status, request=_OPENAI, json={"error": body})
    client = openai.OpenAI(api_key="sk-test")
    return client._make_status_error(message, body=body, response=response)


def _anthropic_missing_key() -> BaseException:
    """The real error the installed Anthropic SDK raises with no key at all."""
    saved = {
        name: os.environ.pop(name, None) for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }
    try:
        client = anthropic.AsyncAnthropic(api_key=None)

        async def call() -> None:
            await client.messages.create(
                model="claude-x", max_tokens=1, messages=[{"role": "user", "content": "hi"}]
            )

        try:
            asyncio.run(call())
        except Exception as exc:  # noqa: BLE001 - the SDK's exact type is under test
            return exc
        raise AssertionError("the Anthropic SDK accepted a request without a key")
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def _openai_missing_key() -> BaseException:
    saved = {name: os.environ.pop(name, None) for name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY")}
    try:
        try:
            openai.OpenAI(api_key=None)
        except Exception as exc:  # noqa: BLE001 - the SDK's exact type is under test
            return exc
        raise AssertionError("the OpenAI SDK built a client without a key")
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def _wrapped(cause: BaseException, status: int | None = None) -> BaseException:
    """The lauren-ai transport wrapper the agent turn actually sees."""
    from lauren_ai._exceptions import TransientTransportError, TransportError

    cls = TransientTransportError if status in (429, 529) or status is None else TransportError
    error = cls(str(cause), status_code=status, provider="anthropic", cause=cause)
    return error


CASES: list[tuple[str, BaseException, FailureKind]] = [
    # missing credentials: no type, no status → the fixed prefix allow-list
    ("anthropic no key", _anthropic_missing_key(), FailureKind.MISSING_CREDENTIALS),
    ("openai no key", _openai_missing_key(), FailureKind.MISSING_CREDENTIALS),
    # rejected credentials
    (
        "anthropic 401",
        _anthropic_status(401, "authentication_error", "invalid x-api-key"),
        FailureKind.REJECTED_CREDENTIALS,
    ),
    (
        "anthropic 403",
        _anthropic_status(403, "permission_error", "no access"),
        FailureKind.REJECTED_CREDENTIALS,
    ),
    (
        "openai 401",
        _openai_status(401, "Incorrect API key", "invalid_request_error", "invalid_api_key"),
        FailureKind.REJECTED_CREDENTIALS,
    ),
    # rate limited
    (
        "anthropic 429",
        _anthropic_status(429, "rate_limit_error", "slow down"),
        FailureKind.RATE_LIMITED,
    ),
    (
        "openai 429",
        _openai_status(429, "Rate limit reached", "requests", "rate_limit_exceeded"),
        FailureKind.RATE_LIMITED,
    ),
    # overloaded
    (
        "anthropic 529",
        _anthropic_status(529, "overloaded_error", "Overloaded"),
        FailureKind.OVERLOADED,
    ),
    ("openai 503", _openai_status(503, "The server is overloaded"), FailureKind.OVERLOADED),
    # context too long
    (
        "anthropic 400 prompt too long",
        _anthropic_status(
            400, "invalid_request_error", "prompt is too long: 210000 tokens > 200000 maximum"
        ),
        FailureKind.CONTEXT_TOO_LONG,
    ),
    (
        "openai 400 context_length_exceeded",
        _openai_status(
            400, "maximum context length", "invalid_request_error", "context_length_exceeded"
        ),
        FailureKind.CONTEXT_TOO_LONG,
    ),
    # model not found
    (
        "anthropic 404",
        _anthropic_status(404, "not_found_error", "model: claude-x"),
        FailureKind.MODEL_NOT_FOUND,
    ),
    (
        "openai 404",
        _openai_status(404, "The model does not exist", "invalid_request_error", "model_not_found"),
        FailureKind.MODEL_NOT_FOUND,
    ),
    # billing
    (
        "anthropic 400 credit",
        _anthropic_status(
            400, "invalid_request_error", "Your credit balance is too low to access the API."
        ),
        FailureKind.BILLING,
    ),
    (
        "openai 429 insufficient_quota",
        _openai_status(429, "You exceeded your quota", "insufficient_quota", "insufficient_quota"),
        FailureKind.BILLING,
    ),
    (
        "openai 402",
        _openai_status(402, "Payment required"),
        FailureKind.BILLING,
    ),
    # network
    ("anthropic connection", anthropic.APIConnectionError(request=_ANTHROPIC), FailureKind.NETWORK),
    ("openai connection", openai.APIConnectionError(request=_OPENAI), FailureKind.NETWORK),
    ("httpx connect", httpx.ConnectError("refused", request=_ANTHROPIC), FailureKind.NETWORK),
    ("tls", ssl.SSLError("certificate verify failed"), FailureKind.NETWORK),
    # timeout
    ("anthropic timeout", anthropic.APITimeoutError(request=_ANTHROPIC), FailureKind.TIMEOUT),
    ("openai timeout", openai.APITimeoutError(request=_OPENAI), FailureKind.TIMEOUT),
    ("httpx read timeout", httpx.ReadTimeout("timed out", request=_ANTHROPIC), FailureKind.TIMEOUT),
    # cancelled
    ("cancelled", asyncio.CancelledError(), FailureKind.CANCELLED),
    # internal
    ("value error", ValueError("bad"), FailureKind.INTERNAL),
    ("type error", TypeError("unsupported operand"), FailureKind.INTERNAL),
]


@pytest.mark.parametrize(("label", "error", "kind"), CASES, ids=[case[0] for case in CASES])
def test_each_real_sdk_exception_has_its_kind(
    label: str, error: BaseException, kind: FailureKind
) -> None:
    assert classify_turn_failure(error, "anthropic") is kind, label


@pytest.mark.parametrize(
    ("label", "error", "kind"),
    [case for case in CASES if case[2] is not FailureKind.CANCELLED],
    ids=[case[0] for case in CASES if case[2] is not FailureKind.CANCELLED],
)
def test_the_lauren_transport_wrapper_keeps_the_kind(
    label: str, error: BaseException, kind: FailureKind
) -> None:
    status = getattr(error, "status_code", None)
    assert classify_turn_failure(_wrapped(error, status), "anthropic") is kind, label


def test_every_kind_is_covered_by_a_real_exception() -> None:
    assert {case[2] for case in CASES} == set(FailureKind)


def test_the_prefix_allow_list_matches_the_installed_sdks_wording() -> None:
    """A provider SDK that rewords its error fails here, not in front of the user."""
    for error in (_anthropic_missing_key(), _openai_missing_key()):
        head = str(error).strip().strip("\"'").casefold()
        assert any(
            head.startswith(prefix) for prefix in MESSAGE_PREFIXES[FailureKind.MISSING_CREDENTIALS]
        ), head


def test_messages_match_only_as_a_fixed_prefix() -> None:
    assert classify_turn_failure(RuntimeError("rate limit"), "anthropic") is FailureKind.INTERNAL
    assert (
        classify_turn_failure(
            RuntimeError("tool said: could not resolve authentication method"), "anthropic"
        )
        is FailureKind.INTERNAL
    )
    assert (
        classify_turn_failure(TypeError("Could not resolve authentication method."), "anthropic")
        is FailureKind.MISSING_CREDENTIALS
    )


def test_the_copy_is_agenthicc_text_with_values_substituted(tmp_path: object) -> None:
    missing = failure_copy(FailureKind.MISSING_CREDENTIALS, provider="anthropic")
    assert missing.message == "No API key for anthropic."
    assert missing.action.hint == "Set ANTHROPIC_API_KEY, or run /model to switch provider."
    assert missing.action.commands == ("/model",)

    rejected = failure_copy(FailureKind.REJECTED_CREDENTIALS, provider="anthropic", profile="work")
    assert rejected.message == "anthropic rejected the API key (profile work)."

    limited = failure_copy(
        FailureKind.RATE_LIMITED, provider="anthropic", retries=3, retry_after_s=40
    )
    assert limited.message == "Rate limited by anthropic; retried 3 times."
    assert limited.action.hint == "Wait 40 s, then send the message again."

    long = failure_copy(FailureKind.CONTEXT_TOO_LONG, provider="anthropic", model="opus-4.8")
    assert long.message == "The conversation is longer than opus-4.8's context."
    assert long.action.commands == ("/compact",)

    assert (
        failure_copy(FailureKind.MODEL_NOT_FOUND, provider="anthropic", model="claude-x").message
        == "anthropic has no model named claude-x."
    )
    assert (
        failure_copy(FailureKind.NETWORK, provider="anthropic", host="api.anthropic.com").message
        == "Cannot reach api.anthropic.com."
    )
    assert (
        failure_copy(FailureKind.TIMEOUT, provider="anthropic", timeout_s=600).message
        == "anthropic did not answer in 600 s."
    )
    internal = failure_copy(
        FailureKind.INTERNAL,
        provider="anthropic",
        exception_type="TypeError",
        log_path=diagnostics_log_path("abc"),
    )
    assert internal.message == "The turn failed unexpectedly (TypeError)."
    assert (
        internal.action.hint == "Run /logs to see the diagnostics log at ~/.agenthicc/logs/abc.log."
    )


def test_the_host_comes_from_the_request_object_not_the_message() -> None:
    error = anthropic.APIConnectionError(message="api.evil.example down", request=_ANTHROPIC)
    assert failure_host(error) == "api.anthropic.com"
    assert failure_host(ValueError("api.anthropic.com")) == ""


def test_the_payload_never_contains_the_matched_sdk_text() -> None:
    error = _anthropic_missing_key()
    kind = classify_turn_failure(error, "anthropic")
    payload = failure_payload(failure_copy(kind, provider="anthropic"))
    assert payload["kind"] == "missing_credentials"
    assert "resolve authentication" not in json.dumps(payload).casefold()


def test_a_reported_failure_is_found_through_wrapping_exceptions() -> None:
    inner = TypeError("secret provider text")
    inner.__dict__[FAILURE_ATTRIBUTE] = {"kind": "internal", "message": "The turn failed."}
    try:
        try:
            raise inner
        except TypeError as exc:
            raise RuntimeError("workflow wrapper") from exc
    except RuntimeError as outer:
        assert reported_failure(outer) == {"kind": "internal", "message": "The turn failed."}
        assert failure_reason(outer) == "The turn failed."
    assert failure_reason(ValueError("plain")) == "ValueError: plain"
    assert reported_failure(ValueError("plain")) is None
