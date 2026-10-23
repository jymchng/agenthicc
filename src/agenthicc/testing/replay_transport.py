"""ReplayTransport — a MockTransport that ends a cassette replay deterministically.

``MockTransport`` raises ``EmptyQueueError`` once its queue is empty.  During a
cassette replay that exception would travel through the agent turn's error
classification and the workflow's failure handling, so the replay's end state
would depend on which layer happened to catch it.

ReplayTransport instead *parks* the request that arrives after the last
recorded response: it sets :attr:`exhausted` and blocks until it is cancelled.
The replay harness waits for either the workflow to finish or the cassette to
run out, then cancels the workflow task itself.  It also records, per consumed
response, which tool names the current runtime offered, so a replay can prove
that every recorded tool call was routed to a tool that still exists.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from lauren_ai import Completion, CompletionChunk, Message, RequestOptions, ToolSchema
from lauren_ai._transport import ToolChoice
from lauren_ai._transport._mock import MockTransport

__all__ = ["OfferedTools", "ReplayTransport"]


def _schema_name(schema: object) -> str:
    """Return a tool schema's name.

    The annotation says ``ToolSchema``, but the agent runner passes the
    provider-facing dict form; accept both.
    """
    if isinstance(schema, ToolSchema):
        return schema.name
    if isinstance(schema, dict):
        name = schema.get("name", "")
        return name if isinstance(name, str) else ""
    return ""


@dataclass(frozen=True)
class OfferedTools:
    """The tool names the runtime offered when one recorded response was served."""

    entry_index: int
    names: frozenset[str]


class ReplayTransport(MockTransport):
    """Serve recorded responses in order, then park until cancelled."""

    def __init__(
        self,
        streams: Sequence[list[CompletionChunk]],
        entry_indices: Sequence[int],
    ) -> None:
        super().__init__()
        if len(streams) != len(entry_indices):
            raise ValueError("every replayed stream needs exactly one cassette entry index")
        for chunks in streams:
            self.queue_stream(chunks)
        self._entry_indices: list[int] = list(entry_indices)
        self._served = 0
        self.offered: list[OfferedTools] = []
        self.exhausted = asyncio.Event()

    @property
    def served(self) -> int:
        """Number of recorded responses already handed to the runtime."""
        return self._served

    @property
    def last_served_index(self) -> int:
        """Cassette index of the response served most recently, or -1."""
        return self._entry_indices[self._served - 1] if self._served else -1

    @property
    def remaining(self) -> int:
        """Number of recorded responses not yet requested."""
        return len(self._entry_indices) - self._served

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None = None,
        tools: list[ToolSchema] | None = None,
        tool_choice: ToolChoice | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stop_sequences: list[str] | None = None,
        stream: bool = False,
        thinking: bool = False,
        thinking_budget_tokens: int = 8000,
        top_p: float | None = None,
        max_completion_tokens: int | None = None,
        request_options: RequestOptions | None = None,
    ) -> Completion | AsyncIterator[CompletionChunk]:
        if self.remaining <= 0:
            self.exhausted.set()
            # Park the request; the harness cancels the workflow task.
            await asyncio.Event().wait()
        self.offered.append(
            OfferedTools(
                entry_index=self._entry_indices[self._served],
                names=frozenset(_schema_name(schema) for schema in tools or ()),
            )
        )
        self._served += 1
        return await super().complete(
            messages,
            model=model,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            stop_sequences=stop_sequences,
            stream=stream,
            thinking=thinking,
            thinking_budget_tokens=thinking_budget_tokens,
            top_p=top_p,
            max_completion_tokens=max_completion_tokens,
            request_options=request_options,
        )
