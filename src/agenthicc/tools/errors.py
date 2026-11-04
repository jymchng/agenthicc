"""The tool result envelope and its error vocabulary (TOOLR-04).

Every tool result is a JSON object:

```text
ok: bool            always present
error_code: str     present iff ok is false; a ToolErrorCode value
error: str          present iff ok is false; human-readable, at most 1,000 chars
retryable: bool     present iff ok is false
truncated: bool     present iff any field was cut
…tool-specific fields
```

:func:`tool_error` builds a failure. :class:`ToolContractHook`, the last
global hook on every agent and subagent runner, enforces the envelope across
the whole surface, including workflow and MCP tools:

- an exception a tool raises becomes a failure with a classified code, so no
  traceback reaches the model or the TUI (cancellation stays cancellation);
- a mapping result without ``ok`` gains ``ok: true``;
- a failure without ``error_code`` or ``retryable`` gains them, classified from
  its message.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from lauren_ai._tools._hooks import (
        AfterToolHookDecision,
        BeforeToolHookDecision,
        ErrorToolHookDecision,
        ToolCallContext,
    )

__all__ = [
    "ERROR_MESSAGE_LIMIT",
    "ToolContractHook",
    "ToolErrorCode",
    "classify_exception",
    "classify_message",
    "complete_envelope",
    "tool_error",
]

#: The longest ``error`` text a result carries.
ERROR_MESSAGE_LIMIT: Final = 1_000


class ToolErrorCode(StrEnum):
    """Why a tool call failed. The one vocabulary every tool uses."""

    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    PERMISSION_DENIED = "permission_denied"
    OUTSIDE_WORKSPACE = "outside_workspace"
    APPROVAL_DENIED = "approval_denied"
    BLOCKED_BY_MODE = "blocked_by_mode"
    AMBIGUOUS_MATCH = "ambiguous_match"
    STALE_FILE = "stale_file"
    UNDECODABLE = "undecodable"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    NETWORK = "network"
    HTTP_STATUS = "http_status"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    FRAMEWORK_UNAVAILABLE = "framework_unavailable"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    INTERNAL = "internal"


#: Codes whose failure may succeed if the same call is repeated (after a
#: re-read, for ``stale_file``).
_RETRYABLE: Final = frozenset(
    {
        ToolErrorCode.TIMEOUT,
        ToolErrorCode.NETWORK,
        ToolErrorCode.UNAVAILABLE,
        ToolErrorCode.CONFLICT,
        ToolErrorCode.STALE_FILE,
    }
)


def _bounded(message: str) -> str:
    if len(message) <= ERROR_MESSAGE_LIMIT:
        return message
    return message[: ERROR_MESSAGE_LIMIT - 1] + "…"


def tool_error(
    code: ToolErrorCode,
    message: str,
    *,
    retryable: bool | None = None,
    **fields: object,
) -> dict[str, object]:
    """A failure envelope: ``ok: false`` with *code*, *message* and *fields*."""
    result: dict[str, object] = {
        "ok": False,
        "error_code": code.value,
        "error": _bounded(message),
        "retryable": code in _RETRYABLE if retryable is None else retryable,
    }
    result.update(fields)
    return result


def classify_exception(exc: BaseException) -> ToolErrorCode:
    """The code for an exception a tool raised."""
    from agenthicc.tools.http import is_network_error

    if isinstance(exc, asyncio.CancelledError):
        return ToolErrorCode.CANCELLED
    if isinstance(exc, TimeoutError):
        return ToolErrorCode.TIMEOUT
    if isinstance(exc, FileNotFoundError):
        return ToolErrorCode.NOT_FOUND
    if isinstance(exc, FileExistsError):
        return ToolErrorCode.ALREADY_EXISTS
    if isinstance(exc, PermissionError):
        return ToolErrorCode.PERMISSION_DENIED
    if isinstance(exc, UnicodeDecodeError):
        return ToolErrorCode.UNDECODABLE
    if _is_http_status_error(exc):
        return ToolErrorCode.HTTP_STATUS
    if isinstance(exc, (ConnectionError, socket.gaierror, socket.herror)):
        return ToolErrorCode.NETWORK
    if isinstance(exc, OSError):
        # ``is_network_error`` counts every OSError, which a full disk is not.
        return ToolErrorCode.INTERNAL
    if is_network_error(exc):
        return ToolErrorCode.NETWORK
    if isinstance(exc, (TypeError, ValueError, re.error, json.JSONDecodeError)):
        return ToolErrorCode.INVALID_ARGUMENT
    return ToolErrorCode.INTERNAL


def _is_http_status_error(exc: BaseException) -> bool:
    import httpx

    return isinstance(exc, httpx.HTTPStatusError)


#: Message fragments, in priority order, for failures that carry no code.
_MESSAGE_CODES: Final[tuple[tuple[tuple[str, ...], ToolErrorCode], ...]] = (
    (
        ("outside workspace", "outside_workspace", "outside the workspace"),
        ToolErrorCode.OUTSIDE_WORKSPACE,
    ),
    (
        ("approval denied", "denied by user", "user denied", "rejected by user"),
        ToolErrorCode.APPROVAL_DENIED,
    ),
    (("blocked in", "not allowed in", "blocked by mode"), ToolErrorCode.BLOCKED_BY_MODE),
    (("permission", "forbidden", "denied"), ToolErrorCode.PERMISSION_DENIED),
    (("timed out", "timeout"), ToolErrorCode.TIMEOUT),
    (("not found", "no such file", "does not exist", "not_found"), ToolErrorCode.NOT_FOUND),
    (("already exists",), ToolErrorCode.ALREADY_EXISTS),
    (("connection", "network", "dns"), ToolErrorCode.NETWORK),
    (
        ("not installed", "unavailable", "not configured", "not available"),
        ToolErrorCode.UNAVAILABLE,
    ),
    (("invalid", "must be", "required", "expected"), ToolErrorCode.INVALID_ARGUMENT),
)


def classify_message(message: str) -> ToolErrorCode:
    """The code for a failure that reports only free text."""
    lowered = message.lower()
    for fragments, code in _MESSAGE_CODES:
        if any(fragment in lowered for fragment in fragments):
            return code
    return ToolErrorCode.INTERNAL


def complete_envelope(result: object) -> object:
    """*result* with the envelope's required keys filled in.

    A mapping without ``ok`` is a success unless it carries an ``error``; a
    failure gains ``error_code`` and ``retryable``. Other values are returned
    unchanged: a string or a list is a tool's whole answer.
    """
    if not isinstance(result, Mapping):
        return result
    ok = result.get("ok")
    error = result.get("error")
    if ok is True or (ok is None and not error):
        if ok is True:
            return result
        return {"ok": True, **result}
    if ok is not False and not error:
        return result
    completed = dict(result)
    completed["ok"] = False
    message = error if isinstance(error, str) and error else "tool failed"
    completed["error"] = _bounded(message)
    raw_code = completed.get("error_code")
    code = (
        ToolErrorCode(raw_code)
        if isinstance(raw_code, str) and raw_code in ToolErrorCode._value2member_map_
        else classify_message(message)
    )
    completed["error_code"] = code.value
    if not isinstance(completed.get("retryable"), bool):
        completed["retryable"] = code in _RETRYABLE
    return completed


class ToolContractHook:
    """The last global hook: no exception escapes, every result is an envelope."""

    async def before_tool_call(self, ctx: ToolCallContext) -> BeforeToolHookDecision:
        from lauren_ai._tools._hooks import BeforeToolHookDecision

        return BeforeToolHookDecision.proceed()

    async def after_tool_call(self, result: object, ctx: ToolCallContext) -> AfterToolHookDecision:
        from lauren_ai._tools._hooks import AfterToolHookDecision

        completed = complete_envelope(result)
        if completed is result:
            return AfterToolHookDecision.proceed()
        return AfterToolHookDecision.replace(completed)

    async def on_tool_error(self, exc: Exception, ctx: ToolCallContext) -> ErrorToolHookDecision:
        from lauren_ai._tools._hooks import ErrorToolHookDecision

        code = classify_exception(exc)
        detail = str(exc) or type(exc).__name__
        return ErrorToolHookDecision.suppress_with(
            tool_error(code, f"{type(exc).__name__}: {detail}")
        )
