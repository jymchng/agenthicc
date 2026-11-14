"""Failure kinds for a turn or provider error, with actionable copy (TUX-08).

:func:`classify_turn_failure` maps an exception to a fixed :class:`FailureKind`
from its type, HTTP status and provider error type/code. Where a provider SDK
offers neither a type nor a code (the Anthropic SDK reports a missing key as a
bare ``TypeError``), it matches a **fixed allow-list of message prefixes**.
The matched text is never returned or stored: only the kind is.

:func:`failure_copy` turns a kind into the message and the action shown to the
user, with the provider, model and profile substituted. Every word of that copy
is written by agenthicc, so it is safe to persist and render; provider
exception text never is (BEAU-4, PRD-198).
"""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "FAILURE_ATTRIBUTE",
    "FailureAction",
    "FailureCopy",
    "FailureKind",
    "MESSAGE_PREFIXES",
    "classify_turn_failure",
    "failure_copy",
    "failure_host",
    "failure_payload",
    "failure_reason",
    "reported_failure",
]

#: The attribute a failed agent turn sets on its exception: the ``kind``,
#: ``message`` and ``action`` it reported, for other owners (headless JSON).
FAILURE_ATTRIBUTE = "_agenthicc_failure"


class FailureKind(StrEnum):
    """What went wrong, in terms the user can act on."""

    MISSING_CREDENTIALS = "missing_credentials"
    REJECTED_CREDENTIALS = "rejected_credentials"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    CONTEXT_TOO_LONG = "context_too_long"
    MODEL_NOT_FOUND = "model_not_found"
    BILLING = "billing"
    NETWORK = "network"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(frozen=True)
class FailureAction:
    """What the user can do next: one sentence and the commands it names."""

    hint: str
    commands: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {"hint": self.hint, "commands": list(self.commands)}


@dataclass(frozen=True)
class FailureCopy:
    """The user-facing message and action for one failure kind."""

    kind: FailureKind
    message: str
    action: FailureAction


#: Fixed, lower-case message prefixes per kind, tested against each installed
#: provider SDK. Only consulted when the exception carries no type, code or
#: status that identifies the kind. The matched text is never kept.
MESSAGE_PREFIXES: Mapping[FailureKind, tuple[str, ...]] = {
    FailureKind.MISSING_CREDENTIALS: (
        # anthropic: TypeError raised while building the auth headers.
        "could not resolve authentication method",
        # openai >= 2: OpenAIError raised by the client constructor.
        "missing credentials",
        # openai 1.x: the same, older wording.
        "the api_key client option must be set",
    ),
    FailureKind.CONTEXT_TOO_LONG: (
        # anthropic: invalid_request_error with no dedicated type.
        "prompt is too long",
        "input is too long",
        # openai-compatible servers without the context_length_exceeded code.
        "this model's maximum context length is",
    ),
    FailureKind.BILLING: (
        # anthropic: invalid_request_error, HTTP 400.
        "your credit balance is too low",
    ),
}

_TYPE_CODES: Mapping[str, FailureKind] = {
    "authentication_error": FailureKind.REJECTED_CREDENTIALS,
    "permission_error": FailureKind.REJECTED_CREDENTIALS,
    "invalid_api_key": FailureKind.REJECTED_CREDENTIALS,
    "rate_limit_error": FailureKind.RATE_LIMITED,
    "rate_limit_exceeded": FailureKind.RATE_LIMITED,
    "overloaded_error": FailureKind.OVERLOADED,
    "not_found_error": FailureKind.MODEL_NOT_FOUND,
    "model_not_found": FailureKind.MODEL_NOT_FOUND,
    "insufficient_quota": FailureKind.BILLING,
    "billing_error": FailureKind.BILLING,
    "billing_hard_limit_reached": FailureKind.BILLING,
    "context_length_exceeded": FailureKind.CONTEXT_TOO_LONG,
    "string_above_max_length": FailureKind.CONTEXT_TOO_LONG,
    "request_too_large": FailureKind.CONTEXT_TOO_LONG,
}

# SDK exception class names that identify a kind on their own. Names, not
# imports, so a provider SDK that is not installed costs nothing; the tests
# build each real class from the installed anthropic, openai and httpx SDKs.
_TYPE_NAMES: Mapping[str, FailureKind] = {
    # anthropic and openai
    "AuthenticationError": FailureKind.REJECTED_CREDENTIALS,
    "PermissionDeniedError": FailureKind.REJECTED_CREDENTIALS,
    "RateLimitError": FailureKind.RATE_LIMITED,
    "OverloadedError": FailureKind.OVERLOADED,
    "NotFoundError": FailureKind.MODEL_NOT_FOUND,
    "RequestTooLargeError": FailureKind.CONTEXT_TOO_LONG,
    "APITimeoutError": FailureKind.TIMEOUT,
    "APIConnectionError": FailureKind.NETWORK,
    # lauren-ai
    "AuthTransportError": FailureKind.REJECTED_CREDENTIALS,
    "AgentContextOverflowError": FailureKind.CONTEXT_TOO_LONG,
    # httpx
    "TimeoutException": FailureKind.TIMEOUT,
    "ConnectTimeout": FailureKind.TIMEOUT,
    "ReadTimeout": FailureKind.TIMEOUT,
    "WriteTimeout": FailureKind.TIMEOUT,
    "PoolTimeout": FailureKind.TIMEOUT,
    "ConnectError": FailureKind.NETWORK,
    "ReadError": FailureKind.NETWORK,
    "WriteError": FailureKind.NETWORK,
    "NetworkError": FailureKind.NETWORK,
    "ProxyError": FailureKind.NETWORK,
    "RemoteProtocolError": FailureKind.NETWORK,
    "UnsupportedProtocol": FailureKind.NETWORK,
}


def _attribute(value: object, name: str) -> object:
    """Read optional SDK metadata (``status_code``, ``body``) without a dynamic call."""
    try:
        return object.__getattribute__(value, name)
    except AttributeError:
        return None


def _chain(exc: BaseException) -> tuple[BaseException, ...]:
    """The exception and its causes, outermost first, identity-deduplicated."""
    found: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending and len(found) < 8:
        item = pending.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        found.append(item)
        pending.extend((item.__cause__, item.__context__))
    return tuple(found)


def _status(exc: BaseException) -> int | None:
    for code in (
        _attribute(exc, "status_code"),
        _attribute(_attribute(exc, "response"), "status_code"),
    ):
        if isinstance(code, int) and not isinstance(code, bool) and code > 0:
            return code
    return None


def _body_error(exc: BaseException) -> Mapping[str, object]:
    """The structured provider error object (``body["error"]`` or ``body``)."""
    body = _attribute(exc, "body")
    if isinstance(body, Mapping):
        inner = body.get("error")
        if isinstance(inner, Mapping):
            return inner
        return body
    return {}


def _identifiers(exc: BaseException) -> tuple[str, ...]:
    """Provider error ``type`` and ``code`` values, lower-cased."""
    values: list[object] = [_attribute(exc, "type"), _attribute(exc, "code")]
    error = _body_error(exc)
    values += [error.get("type"), error.get("code")]
    return tuple(value.casefold() for value in values if isinstance(value, str) and value)


def _message_heads(exc: BaseException) -> tuple[str, ...]:
    """Candidate message starts: the structured provider message, then ``str(exc)``."""
    heads: list[str] = []
    message = _body_error(exc).get("message")
    if isinstance(message, str):
        heads.append(message)
    heads.append(str(exc))
    return tuple(head.strip().strip("\"'").lstrip().casefold() for head in heads)


def _prefix_kind(exc: BaseException) -> FailureKind | None:
    for head in _message_heads(exc):
        for kind, prefixes in MESSAGE_PREFIXES.items():
            if any(head.startswith(prefix) for prefix in prefixes):
                return kind
    return None


def _status_kind(status: int, exc: BaseException) -> FailureKind | None:
    if status in (401, 403):
        return FailureKind.REJECTED_CREDENTIALS
    if status == 402:
        return FailureKind.BILLING
    if status == 404:
        return FailureKind.MODEL_NOT_FOUND
    if status in (408, 504):
        return FailureKind.TIMEOUT
    if status == 413:
        return FailureKind.CONTEXT_TOO_LONG
    if status == 429:
        return FailureKind.RATE_LIMITED
    if status >= 500:
        return FailureKind.OVERLOADED
    if status == 400:
        return _prefix_kind(exc)
    return None


def _type_kind(exc: BaseException) -> FailureKind | None:
    for cls in type(exc).__mro__:
        kind = _TYPE_NAMES.get(cls.__name__)
        if kind is not None:
            return kind
    if isinstance(exc, (socket.gaierror, ssl.SSLError, ConnectionError)):
        return FailureKind.NETWORK
    if isinstance(exc, TimeoutError):
        return FailureKind.TIMEOUT
    return None


def classify_turn_failure(exc: BaseException, provider: str = "") -> FailureKind:
    """Return the :class:`FailureKind` of *exc* raised by a turn of *provider*.

    Pure over the exception chain: provider error type/code first (they are
    the most specific: a 429 with ``insufficient_quota`` is a billing failure),
    then the HTTP status, the SDK exception class, and last the fixed message
    prefixes. Anything unrecognised is :attr:`FailureKind.INTERNAL`.
    *provider* is accepted for per-provider rules and does not change the
    result for the built-in SDKs.
    """
    del provider  # every built-in rule is provider-neutral today
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return FailureKind.CANCELLED
    chain = _chain(exc)
    for item in chain:
        for identifier in _identifiers(item):
            kind = _TYPE_CODES.get(identifier)
            if kind is not None:
                return kind
    for item in chain:
        status = _status(item)
        if status is not None:
            kind = _status_kind(status, item)
            if kind is not None:
                return kind
    for item in chain:
        prefix = _prefix_kind(item)
        if prefix is not None:
            return prefix
        kind = _type_kind(item)
        if kind is not None:
            return kind
    return FailureKind.INTERNAL


_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def failure_host(exc: BaseException) -> str:
    """The provider host of a failed request (``api.anthropic.com``), or ``""``.

    Read from the request object the SDK attached, never from message text.
    """
    for item in _chain(exc):
        host = _attribute(_attribute(_attribute(item, "request"), "url"), "host")
        if isinstance(host, str) and _HOST.match(host):
            return host
    return ""


def _display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _seconds(value: float) -> str:
    return f"{value:.0f} s" if value >= 1 else f"{value:.1f} s"


def failure_copy(
    kind: FailureKind,
    *,
    provider: str,
    model: str = "",
    profile: str = "",
    exception_type: str = "",
    retries: int = 0,
    retry_after_s: float | None = None,
    timeout_s: float | None = None,
    host: str = "",
    log_path: Path | None = None,
) -> FailureCopy:
    """The message and action for *kind*, with every value substituted."""
    from agenthicc.config import PROVIDER_API_KEY_ENVVAR

    name = provider or "the provider"
    retry = FailureAction("Send the message again to retry.")
    if kind is FailureKind.MISSING_CREDENTIALS:
        env_var = PROVIDER_API_KEY_ENVVAR.get(provider, "")
        hint = (
            f"Set {env_var}, or run /model to switch provider."
            if env_var
            else "Add a key to the provider profile, or run /model to switch provider."
        )
        return FailureCopy(kind, f"No API key for {name}.", FailureAction(hint, ("/model",)))
    if kind is FailureKind.REJECTED_CREDENTIALS:
        suffix = f" (profile {profile})" if profile and profile != "default" else ""
        return FailureCopy(
            kind,
            f"{name} rejected the API key{suffix}.",
            FailureAction("Check the key, or run /model to switch provider.", ("/model",)),
        )
    if kind is FailureKind.RATE_LIMITED:
        retried = f"; retried {retries} time{'s' if retries != 1 else ''}" if retries else ""
        wait = f"Wait {_seconds(retry_after_s)}" if retry_after_s else "Wait a moment"
        return FailureCopy(
            kind,
            f"Rate limited by {name}{retried}.",
            FailureAction(f"{wait}, then send the message again."),
        )
    if kind is FailureKind.OVERLOADED:
        return FailureCopy(kind, f"{name} is overloaded.", retry)
    if kind is FailureKind.CONTEXT_TOO_LONG:
        target = f"{model}'s" if model else "the model's"
        return FailureCopy(
            kind,
            f"The conversation is longer than {target} context.",
            FailureAction("Run /compact, then send the message again.", ("/compact",)),
        )
    if kind is FailureKind.MODEL_NOT_FOUND:
        named = f" named {model}" if model else " with that name"
        return FailureCopy(
            kind,
            f"{name} has no model{named}.",
            FailureAction("Run /model to choose another model.", ("/model",)),
        )
    if kind is FailureKind.BILLING:
        return FailureCopy(
            kind,
            f"{name} refused the request for billing reasons.",
            FailureAction(f"Check the {name} account's plan and credit."),
        )
    if kind is FailureKind.NETWORK:
        return FailureCopy(
            kind,
            f"Cannot reach {host or name}.",
            FailureAction("Check the network or proxy, then send the message again."),
        )
    if kind is FailureKind.TIMEOUT:
        within = f"in {_seconds(timeout_s)}" if timeout_s else "in time"
        return FailureCopy(kind, f"{name} did not answer {within}.", retry)
    if kind is FailureKind.CANCELLED:
        return FailureCopy(kind, "The turn was interrupted.", FailureAction(""))
    typed = f" ({exception_type})" if exception_type else ""
    where = f" at {_display_path(log_path)}" if log_path is not None else ""
    return FailureCopy(
        kind,
        f"The turn failed unexpectedly{typed}.",
        FailureAction(f"Run /logs to see the diagnostics log{where}.", ("/logs",)),
    )


def failure_payload(copy: FailureCopy) -> dict[str, object]:
    """The ``kind``/``message``/``action`` fields of an ``error`` event."""
    return {
        "kind": copy.kind.value,
        "message": copy.message,
        "action": copy.action.to_payload(),
    }


def reported_failure(exc: BaseException) -> dict[str, object] | None:
    """The failure payload a turn attached to *exc* or one of its causes."""
    for item in _chain(exc):
        value = _attribute(item, FAILURE_ATTRIBUTE)
        if isinstance(value, dict) and isinstance(value.get("kind"), str):
            return {str(key): field for key, field in value.items()}
    return None


def failure_reason(exc: BaseException) -> str:
    """A one-line failure reason safe to persist: the turn's copy, else ``Type: text``.

    A failed agent turn's exception carries agenthicc's own message; any other
    exception is agenthicc's or an extension's, and keeps its text.
    """
    failure = reported_failure(exc)
    if failure is not None:
        return str(failure.get("message", ""))
    return f"{type(exc).__name__}: {exc}"
