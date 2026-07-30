"""Stable, non-sensitive error values for the optional browser tools."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["BrowserErrorKind", "BrowserToolError"]


class BrowserErrorKind(StrEnum):
    """Machine-readable browser failure categories returned to an agent."""

    DISABLED = "not_configured"
    DEPENDENCY_MISSING = "dependency_missing"
    BINARY_MISSING = "binary_missing"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    UNHEALTHY = "unhealthy"
    APPROVAL_REQUIRED = "approval_required"
    POLICY_DENIED = "policy_denied"
    INVALID_INPUT = "invalid_input"
    STALE_PAGE = "stale_page"
    NOT_FOUND = "stale_page"
    LIMIT_EXCEEDED = "limit_exceeded"
    OUTPUT_LIMIT = "output_limit"
    TIMEOUT = "timeout"
    NETWORK = "network"
    EXECUTION = "execution"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class BrowserToolError(Exception):
    """An expected browser failure with a safe message for model output.

    The original exception is intentionally not retained in the public error
    message.  Callers may log it privately, but tool results must never expose
    credentials, cookies, query strings, filesystem paths, or stack traces.
    """

    def __init__(self, kind: BrowserErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_message = message
