"""Common tool contracts for the Agenthicc execution layer."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, ClassVar, TypeAlias

if TYPE_CHECKING:
    from agenthicc.tools.context import ToolCallContext

__all__ = [
    "Tool",
    "ToolBase",
    "ToolLike",
    "ToolResult",
    "ToolResultEnvelope",
    "arg_bool",
    "arg_float",
    "arg_int",
    "arg_str",
]


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
        args: Mapping[str, object],
    ) -> ToolResult:
        """Execute with the normalized context and validated arguments."""
        ...


class Tool(abc.ABC):
    """Legacy tool contract retained for existing built-in subclasses.

    Existing Agenthicc tools accept ``(args, context)`` and return a plain
    dictionary.  ``ToolExecutor`` adapts that shape to :class:`ToolResult`.
    New tools should inherit :class:`ToolBase` and use the typed signature.

    Subclasses declare the same stable metadata as :class:`ToolBase`.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[dict[str, object]] = {}
    capabilities: ClassVar[frozenset[str]] = frozenset()
    destructive: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = False

    @abc.abstractmethod
    async def execute(
        self,
        args: Mapping[str, object],
        context: Mapping[str, object],
    ) -> dict[str, object]:
        """Execute the tool.

        :param args: Argument dict (matching :attr:`parameters`).
        :param context: Per-call context dict injected by the executor
            (sandbox handles, agent identity, hook state bag, ...).
        :return: Any JSON-serialisable value.
        """
        ...


ToolLike: TypeAlias = Callable[..., object] | Tool


def arg_str(args: Mapping[str, object], key: str, default: str | None = None) -> str:
    """Extract a string argument, rejecting malformed tool input."""
    value = args.get(key, default)
    if isinstance(value, str):
        return value
    raise ValueError(f"tool argument {key!r} must be a string")


def arg_bool(args: Mapping[str, object], key: str, default: bool = False) -> bool:
    """Extract a boolean argument, rejecting malformed tool input."""
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"tool argument {key!r} must be a boolean")


def arg_int(args: Mapping[str, object], key: str, default: int = 0) -> int:
    """Extract an integer argument, rejecting booleans and malformed values."""
    value = args.get(key, default)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"tool argument {key!r} must be an integer")


def arg_float(args: Mapping[str, object], key: str, default: float = 0.0) -> float:
    """Extract a numeric argument as a float."""
    value = args.get(key, default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"tool argument {key!r} must be a number")


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
