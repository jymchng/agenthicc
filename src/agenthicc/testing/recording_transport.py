"""RecordingTransport — wraps any Transport and saves calls to a cassette JSONL.

Usage (from TUI session, when --record-cassette is given)::

    transport = _build_transport(llm_cfg)
    transport = RecordingTransport(transport, cassette_dir / "cassette.jsonl")
    agent_runner = AgentRunnerBase(transport=transport, signals=SignalBus())

Each ``complete()`` call appends one JSON line to ``cassette_path``::

    {
      "index": 0,
      "model": "claude-sonnet-4-6",
      "tool_names_available": ["finalize_plan", "request_plan_approval"],
      "response": {
        "content": "",
        "stop_reason": "tool_use",
        "tool_calls": [
          {"name": "finalize_plan", "tool_use_id": "tu_abc", "input": {"plan": "..."}}
        ]
      }
    }
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lauren_ai._transport import (
        Completion, CompletionChunk, ToolCallDelta, ToolSchema,
        Message, ToolChoice,
    )


class RecordingTransport:
    """Transparent proxy that records each complete() call to a JSONL file.

    Thread-safety: all writes are serialised via a sequential file open/close
    per entry.  Fine for single-session recording; not designed for concurrent
    runners writing to the same file.
    """

    def __init__(self, inner: object, cassette_path: Path) -> None:
        self._inner = inner
        self._path = cassette_path
        self._index = 0
        cassette_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate / create fresh on construction so a new recording session
        # always starts from a clean file.
        cassette_path.write_text("", encoding="utf-8")

    # ── Transport protocol ────────────────────────────────────────────────────

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
    ) -> Completion | AsyncIterator[CompletionChunk]:
        tool_names = [
            t.name if hasattr(t, "name") else t.get("name", "")
            for t in (tools or [])
        ]
        if not stream:
            from lauren_ai._transport import Completion  # noqa: PLC0415
            result = await self._inner.complete(
                messages, model=model, system=system, tools=tools,
                tool_choice=tool_choice, max_tokens=max_tokens,
                temperature=temperature, stop_sequences=stop_sequences,
                stream=False, thinking=thinking,
                thinking_budget_tokens=thinking_budget_tokens,
            )
            assert isinstance(result, Completion)
            self._record_completion(model, tool_names, result)
            return result

        # Streaming: intercept chunks to assemble the full response.
        inner_iter: AsyncIterator[CompletionChunk] = await self._inner.complete(
            messages, model=model, system=system, tools=tools,
            tool_choice=tool_choice, max_tokens=max_tokens,
            temperature=temperature, stop_sequences=stop_sequences,
            stream=True, thinking=thinking,
            thinking_budget_tokens=thinking_budget_tokens,
        )
        return self._intercepting_stream(model, tool_names, inner_iter)

    async def embed(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.embed(*args, **kwargs)

    async def count_tokens(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.count_tokens(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # ── Recording helpers ─────────────────────────────────────────────────────

    async def _intercepting_stream(
        self,
        model: str,
        tool_names: list[str],
        inner: AsyncIterator[CompletionChunk],
    ) -> AsyncIterator[CompletionChunk]:
        from lauren_ai._transport import CompletionChunk  # noqa: PLC0415
        collected: list[CompletionChunk] = []
        async for chunk in inner:
            collected.append(chunk)
            yield chunk
        self._record_from_chunks(model, tool_names, collected)

    def _record_completion(
        self,
        model: str,
        tool_names: list[str],
        completion: Completion,
    ) -> None:
        entry: dict[str, Any] = {
            "index": self._index,
            "model": model,
            "tool_names_available": tool_names,
            "response": {
                "content": completion.content,
                "stop_reason": completion.stop_reason,
                "tool_calls": [
                    {
                        "name": tc.name,
                        "tool_use_id": tc.tool_use_id,
                        "input": tc.input,
                    }
                    for tc in completion.tool_calls
                ],
            },
        }
        self._append(entry)

    def _record_from_chunks(
        self,
        model: str,
        tool_names: list[str],
        chunks: list[CompletionChunk],
    ) -> None:
        content_parts: list[str] = []
        stop_reason: str = "end_turn"
        # tool_use_id -> {name, input_json}
        partial: dict[str, dict[str, str]] = {}

        for chunk in chunks:
            if chunk.delta:
                content_parts.append(chunk.delta)
            if chunk.stop_reason:
                stop_reason = chunk.stop_reason
            tcd = chunk.tool_call_delta
            if tcd is not None:
                tid = tcd.tool_use_id
                if tcd.name:
                    partial.setdefault(tid, {"name": tcd.name, "input_json": ""})
                    partial[tid]["name"] = tcd.name
                if tid in partial:
                    partial[tid]["input_json"] = (
                        partial[tid].get("input_json", "") + tcd.input_delta
                    )

        tool_calls: list[dict[str, Any]] = []
        for tid, data in partial.items():
            if not data.get("name"):
                continue
            try:
                parsed_input = json.loads(data.get("input_json", "") or "{}")
            except json.JSONDecodeError:
                parsed_input = {}
            tool_calls.append({
                "name": data["name"],
                "tool_use_id": tid,
                "input": parsed_input,
            })

        entry: dict[str, Any] = {
            "index": self._index,
            "model": model,
            "tool_names_available": tool_names,
            "response": {
                "content": "".join(content_parts),
                "stop_reason": stop_reason,
                "tool_calls": tool_calls,
            },
        }
        self._append(entry)

    def _append(self, entry: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._index += 1
