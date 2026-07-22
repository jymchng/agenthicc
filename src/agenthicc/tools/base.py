"""Common tool contracts for the Agenthicc execution layer."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from agenthicc.tools.context import ToolCallContext

__all__ = ["Tool", "ToolBase", "ToolResult", "ToolResultEnvelope"]


@dataclass(slots=True)
class ToolResult:
    """Normalized result returned by every Agenthicc tool call.

    ``value`` is populated for successful calls.  Failed calls retain a stable
    ``error_kind`` so callers can distinguish policy, timeout, network, and
    provider failures without parsing human-readable error text.
    """

    ok: bool
    value: object = None
    error: str | None = None
    duration_ms: float = 0.0
    error_kind: str | None = None

    @classmethod
    def success(cls, value: object = None) -> "ToolResult":
        """Build a successful result."""
        return cls(ok=True, value=value)

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        error_kind: str | None = None,
    ) -> "ToolResult":
        """Build a failed result with an optional machine-readable kind."""
        return cls(ok=False, error=error, error_kind=error_kind)

    def with_duration(self, duration_ms: float) -> "ToolResult":
        """Return a copy carrying the measured execution duration."""
        return replace(self, duration_ms=duration_ms)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible result envelope."""
        return {
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "error_kind": self.error_kind,
        }


class ToolBase(abc.ABC):
    """Typed base class for tools executed by :class:`ToolExecutor`."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[dict[str, object]] = {}
    capabilities: ClassVar[frozenset[str]] = frozenset()
    destructive: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = False

    @abc.abstractmethod
    async def execute(
        self,
        context: "ToolCallContext",
        args: dict[str, object],
    ) -> ToolResult:
        """Execute with the normalized context and validated arguments."""
        ...


class Tool(ToolBase):
    """Legacy tool contract retained for existing built-in subclasses.

    Existing Agenthicc tools accept ``(args, context)`` and return a plain
    dictionary.  ``ToolExecutor`` adapts that shape to :class:`ToolResult`.
    New tools should inherit :class:`ToolBase` and use the typed signature.

    Subclasses declare the same stable metadata as :class:`ToolBase`.
    """

    @abc.abstractmethod
    async def execute(
        self,
        args: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        """Execute the tool.

        :param args: Argument dict (matching :attr:`parameters`).
        :param context: Per-call context dict injected by the executor
            (sandbox handles, agent identity, hook state bag, ...).
        :return: Any JSON-serialisable value.
        """
        ...


@dataclass(slots=True)
class ToolResultEnvelope:
    """Structured outcome of a single tool invocation."""

    tool_call_id: str
    tool_name: str
    ok: bool
    value: object = None
    error: str | None = None
    duration_ms: float = 0.0
    error_kind: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "error_kind": self.error_kind,
        }
