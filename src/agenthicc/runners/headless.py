"""Headless runner — emits kernel events as JSON lines to stdout."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenthicc.cli.context import CLIContext


async def _run_headless(ctx: CLIContext | None = None) -> None:
    from agenthicc.kernel import AppState, Event, EventProcessor, SecurityPolicy, SystemSettings

    state = AppState.create(settings=SystemSettings(), policy=SecurityPolicy())

    # PRD-79: apply CLIFlags from the CLI context.
    if ctx is not None:
        state.cli_flags = ctx.flags

    processor = EventProcessor(initial_state=state, persist=False)
    sub = processor.subscribe()
    proc_task = asyncio.create_task(processor.run())
    print(json.dumps({"status": "ready", "mode": "headless"}), flush=True)
    try:
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            intent_id = uuid.uuid4().hex
            await processor.emit(Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text}))
            try:
                snap = await asyncio.wait_for(sub.get(), timeout=2.0)
                intent = snap.intents.get(intent_id)
                print(json.dumps({"event_type": "IntentCreated", "intent_id": intent_id,
                                  "status": intent.status.value if intent else "pending"}), flush=True)
            except asyncio.TimeoutError:
                print(json.dumps({"event_type": "Error", "message": "timeout"}), flush=True)
    finally:
        proc_task.cancel()
        await asyncio.gather(proc_task, return_exceptions=True)
