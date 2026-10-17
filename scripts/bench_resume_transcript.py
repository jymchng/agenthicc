#!/usr/bin/env python3
"""Benchmark the bounded resumed-session attach path without a provider.

Example (release/performance run):

    uv run python scripts/bench_resume_transcript.py --events 100000 --samples 5
    uv run python scripts/bench_resume_transcript.py --events 100000 --large-event-bytes 0 --tool-event-every 1

The benchmark measures local journal restoration, the first Rich Live frame,
bounded transcript-page reading, input-history projection, preview rendering,
input-ready latency after local runtime restoration, and their summed local
attach cost. It does not include provider, plugin, MCP, browser, or project
discovery initialization; those are measured by ``/startup`` in a real run.
No transcript text is printed or sent to a provider.
"""

from __future__ import annotations

import argparse
import asyncio
from io import StringIO
import json
import os
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import TypedDict

from rich.console import Console

import agenthicc.tui.runtime.session_log as session_log
from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.session_log import load_recent_transcript_page
from agenthicc.tui.workspace.workspace import Workspace


class Samples(TypedDict):
    p50_ms: float
    p95_ms: float
    values_ms: list[float]


def _summary(values: list[float]) -> Samples:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * 0.95 + 0.5)))
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "values_ms": [round(value, 3) for value in values],
    }


def _write_transcript(
    path: Path, events: int, large_event_bytes: int, tool_event_every: int
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with path.open("w", encoding="utf-8") as handle:
        for index in range(events):
            text = (
                "L" * large_event_bytes
                if index == events - 1 and large_event_bytes
                else f"synthetic transcript event {index}"
            )
            if index == events - 1 and large_event_bytes:
                kind = "text"
                payload: dict[str, object] = {"text": text}
            elif tool_event_every > 0 and index % tool_event_every == 0:
                kind = "tool_complete"
                payload = {
                    "name": "read_file",
                    "args_str": f" path-{index}",
                    "success": True,
                    "output_lines": [f"synthetic output line {index}"],
                    "output_more": 0,
                }
            else:
                kind = "text" if index % 2 else "user_message"
                payload = {"text": text}
            record = {
                "event_id": f"event-{index}",
                "kind": kind,
                "payload": payload,
                "timestamp": float(index + 1),
            }
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
            handle.write(line)
            total_bytes += len(line.encode("utf-8"))
    return total_bytes


def _seed_journal(path: Path, turns: int) -> None:
    conversation = SessionConversation.open(
        "benchmark-session",
        max_tokens=max(100_000, turns * 64),
        journal_path=path,
    )
    for index in range(turns):
        conversation.memory.add_user(f"benchmark request {index}")
        conversation.memory.add_assistant(
            {"role": "assistant", "content": f"benchmark response {index}"}
        )
    conversation.close()


async def _measure_one(session_id: str, journal_path: Path) -> dict[str, float | int]:
    started = time.perf_counter()
    conversation = SessionConversation.open(
        session_id,
        max_tokens=1_000_000,
        journal_path=journal_path,
    )
    journal_restore_ms = (time.perf_counter() - started) * 1000
    conversation.close()

    state = AppState.create()
    console = Console(file=StringIO(), record=True, force_terminal=False, width=100, height=30)
    workspace = Workspace(state, console)
    started = time.perf_counter()
    workspace.start()
    first_frame_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    page = await asyncio.to_thread(load_recent_transcript_page, session_id)
    preview_read_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    input_history = session_log.user_message_history_from_events(page.events)
    input_history_projection_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    await workspace.replay_transcript(page.events)
    preview_render_ms = (time.perf_counter() - started) * 1000
    workspace.stop()

    ready_after_context_ms = preview_read_ms + input_history_projection_ms + preview_render_ms
    return {
        "session_context_restore_ms": journal_restore_ms,
        "first_frame_ms": first_frame_ms,
        "preview_read_ms": preview_read_ms,
        "input_history_projection_ms": input_history_projection_ms,
        "preview_render_ms": preview_render_ms,
        "input_ready_after_context_ms": ready_after_context_ms,
        "local_attach_total_ms": (journal_restore_ms + first_frame_ms + ready_after_context_ms),
        "preview_events": len(page.entries),
        "preview_retained_bytes": page.retained_bytes,
        "preview_scanned_bytes": page.scanned_bytes,
        "input_history_items": len(input_history),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--large-event-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument(
        "--tool-event-every",
        type=int,
        default=10,
        help="Make every Nth record a tool completion; use 1 for all-tool logs, 0 to disable.",
    )
    parser.add_argument("--journal-turns", type=int, default=500)
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()
    if (
        args.events < 1
        or args.journal_turns < 0
        or args.samples < 1
        or args.large_event_bytes < 0
        or args.tool_event_every < 0
    ):
        parser.error(
            "events/samples must be positive; journal turns, large event size, and "
            "tool-event interval must be non-negative"
        )

    with tempfile.TemporaryDirectory(prefix="agenthicc-transcript-bench-") as temporary:
        root = Path(temporary)
        session_id = "benchmark-session"
        sessions_root = root / "sessions"
        previous_sessions_root = session_log._SESSIONS_DIR
        session_log._SESSIONS_DIR = sessions_root
        try:
            transcript_path = session_log.get_session_log_path(session_id)
            transcript_bytes = _write_transcript(
                transcript_path,
                args.events,
                args.large_event_bytes,
                args.tool_event_every,
            )
            journal_path = root / "conversation-journal.jsonl"
            _seed_journal(journal_path, args.journal_turns)
            measurements = [
                asyncio.run(_measure_one(session_id, journal_path)) for _ in range(args.samples)
            ]
        finally:
            session_log._SESSIONS_DIR = previous_sessions_root

    keys = [key for key in measurements[0] if key.endswith("_ms")]
    report: dict[str, object] = {
        "events": args.events,
        "transcript_bytes": transcript_bytes,
        "large_event_bytes": args.large_event_bytes,
        "tool_event_every": args.tool_event_every,
        "journal_turns": args.journal_turns,
        "samples": args.samples,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "timings": {key: _summary([float(item[key]) for item in measurements]) for key in keys},
        "last_sample_projection": {
            key: value for key, value in measurements[-1].items() if not key.endswith("_ms")
        },
        "targets": {
            "preview_read_p95_under_500ms": _summary(
                [float(item["preview_read_ms"]) for item in measurements]
            )["p95_ms"]
            < 500,
            "input_ready_after_context_p95_under_1000ms": _summary(
                [float(item["input_ready_after_context_ms"]) for item in measurements]
            )["p95_ms"]
            < 1000,
        },
        "scope_note": (
            "session_context_restore_ms measures SessionConversation.open() and its local journal "
            "fold, not full application setup. The full session_context and provider_journal_restore "
            "phases, plus provider, plugin, MCP, browser, and project discovery setup, are reported "
            "separately by /startup."
        ),
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
