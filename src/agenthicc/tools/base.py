"""Tool ABC and result envelope for the Agenthicc tool execution layer (PRD-04)."""

from __future__ import annotations

import abc
from dataclasses import dataclass

__all__ = ["Tool", "ToolResultEnvelope"]


class Tool(abc.ABC):
    """Abstract base class every Agenthicc tool must implement.

    Subclasses declare:

    * ``name`` — stable identifier used in tool_use messages and registry keys.
    * ``description`` — human-readable description forwarded to the LLM.
    * ``parameters`` — JSON Schema dict describing the accepted arguments.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, object] = {}

    @abc.abstractmethod
    async def execute(self, args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
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

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "value": self.value,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
