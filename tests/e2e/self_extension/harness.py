"""Scripted provider requests and failure points for real workflow sessions."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from lauren_ai._transport import Completion, Message, TokenUsage, ToolCall
from lauren_ai._transport._mock import MockTransport

from agenthicc.runners.headless import WorkflowExecutionResult, execute_workflow
from agenthicc.testing.recording_transport import _tool_schema_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from lauren_ai._transport import (
        CompletionChunk,
        RequestOptions,
        ToolChoice,
        ToolSchema,
    )

    from agenthicc.plugins.trust import ExtensionTrust
    from agenthicc.runners.session_context import SessionContext


def _capture_message(message: Message | dict[str, object]) -> dict[str, object]:
    """Copy the typed messages and the dictionaries sent by the agent loop."""
    return asdict(message) if isinstance(message, Message) else deepcopy(message)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """The request as submitted to the scripted provider."""

    model: str
    system: str | None
    messages: tuple[dict[str, object], ...]
    tools: tuple[str, ...]

    def contains(self, text: str) -> bool:
        """Match text in either the system prompt or the provider messages."""
        return text in (self.system or "") or any(
            text in str(message.get("content", "")) for message in self.messages
        )


@dataclass(frozen=True, slots=True)
class _ScriptStep:
    label: str
    accepts: Callable[[ModelRequest], bool]
    reply: Completion


class ScriptedTransport(MockTransport):
    """A provider that fails an unexpected request and captures request-time tools."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ModelRequest] = []
        self._steps: deque[_ScriptStep] = deque()
        self._unexpected: list[str] = []

    def expect(
        self, label: str, accepts: Callable[[ModelRequest], bool], reply: Completion
    ) -> None:
        self._steps.append(_ScriptStep(label, accepts, reply))

    def assert_finished(self) -> None:
        assert not self._unexpected, "\n".join(self._unexpected)
        assert not self._steps, f"Unconsumed script steps: {[step.label for step in self._steps]}"

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
        request = ModelRequest(
            model,
            system,
            tuple(_capture_message(message) for message in messages),
            tuple(_tool_schema_name(item) for item in tools or []),
        )
        self.requests.append(request)
        if not self._steps:
            self._unexpected.append(f"Unexpected model request {len(self.requests)}")
            raise AssertionError(self._unexpected[-1])
        step = self._steps[0]
        if not step.accepts(request):
            self._unexpected.append(
                f"Model request {len(self.requests)} did not match {step.label!r}"
            )
            raise AssertionError(self._unexpected[-1])
        self._steps.popleft()
        self.queue_response(step.reply)
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


def reply(text: str) -> Completion:
    """Construct a normal end-turn reply for a scripted step."""
    return Completion(
        id="wfc-end-turn",
        model="offline",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


def tool_call(name: str, arguments: dict[str, object], *, use_id: str = "") -> Completion:
    """Construct a scripted model response containing one tool call."""
    identity = use_id or f"wfc-{name}"
    return Completion(
        id=identity,
        model="offline",
        content="",
        tool_calls=[ToolCall(tool_use_id=identity, name=name, input=arguments)],
        stop_reason="tool_use",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


@dataclass(slots=True)
class AuthoredSession:
    context: SessionContext
    project: Path
    trust: ExtensionTrust

    async def run(self, workflow: str, intent: str) -> WorkflowExecutionResult:
        return await execute_workflow(self.context, workflow, intent)


class ProcessStopped(BaseException):
    """The scripted boundary was reached before its caller could continue."""


@dataclass(slots=True)
class CrashAt:
    seam: str | None = None

    def check(self, seam: str) -> None:
        if seam == self.seam:
            raise ProcessStopped(seam)
