"""Measure CPU and deterministic memory counters for the hermetic goal-flow replay."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import cast

import lauren_ai._memory as lauren_memory
from lauren_ai._memory import ShortTermMemory

from agenthicc.config import AgenthiccConfig
from agenthicc.memory.journaled import JournaledShortTermMemory
from agenthicc.testing import SessionCassette, run_headless_replay

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "goal_flow_campaign"
RUN_START = 34
APPROVALS_BEFORE_RUN = 1
EXPECTED_STEPS = 510


def _run() -> dict[str, float]:
    counters = {"full_history_deepcopies": 0, "full_validations": 0}
    tracked_histories: dict[int, list[object]] = {}

    def remember_history(memory: object) -> None:
        history = getattr(memory, "_messages", None)
        if isinstance(history, list):
            tracked_histories[id(history)] = cast("list[object]", history)

    original_deepcopy = copy.deepcopy
    original_validate = lauren_memory._validate_tool_history
    original_base_snapshot = ShortTermMemory.snapshot
    original_journaled_snapshot = JournaledShortTermMemory.snapshot
    original_ensure_valid = JournaledShortTermMemory.ensure_valid_and_persist

    def counted_deepcopy(value: object, memo: dict[int, object] | None = None) -> object:
        if id(value) in tracked_histories:
            counters["full_history_deepcopies"] += 1
        if memo is None:
            return original_deepcopy(value)
        return original_deepcopy(value, memo)

    def counted_validate(messages: list[object]) -> object:
        counters["full_validations"] += 1
        return original_validate(messages)

    def tracked_base_snapshot(memory: ShortTermMemory) -> object:
        remember_history(memory)
        return original_base_snapshot(memory)

    def tracked_journaled_snapshot(memory: JournaledShortTermMemory) -> object:
        remember_history(memory)
        return original_journaled_snapshot(memory)

    def tracked_ensure_valid(memory: JournaledShortTermMemory) -> bool:
        remember_history(memory)
        return original_ensure_valid(memory)

    copy.deepcopy = counted_deepcopy
    lauren_memory._validate_tool_history = counted_validate
    ShortTermMemory.snapshot = tracked_base_snapshot
    JournaledShortTermMemory.snapshot = tracked_journaled_snapshot
    JournaledShortTermMemory.ensure_valid_and_persist = tracked_ensure_valid

    try:
        cassette = SessionCassette.from_path(
            FIXTURE / "cassette.jsonl", FIXTURE / "approvals.jsonl"
        )
        campaign = cassette.slice(RUN_START, skip_approvals=APPROVALS_BEFORE_RUN)
        if len(campaign.replay_entries(skip_tool_free=True)) != EXPECTED_STEPS:
            raise RuntimeError(
                "goal-flow performance cassette no longer contains 510 provider steps"
            )

        with tempfile.TemporaryDirectory(prefix="agenthicc-perf-goal-flow-") as temporary:
            previous_directory = Path.cwd()
            os.chdir(temporary)
            cpu_started = time.process_time()
            try:
                result = asyncio.run(
                    run_headless_replay(
                        campaign,
                        workflow="goal_flow",
                        intent="Continue the documentation campaign.",
                        cfg=AgenthiccConfig(),
                        hermetic=True,
                        mode="Yolo",
                        timeout_s=1_500,
                    )
                )
                cpu_seconds = time.process_time() - cpu_started
            finally:
                os.chdir(previous_directory)

        if result.error is not None or result.status != "exhausted":
            raise RuntimeError(f"goal-flow replay failed: {result.error or result.status}")
        if result.transport_calls != EXPECTED_STEPS:
            raise RuntimeError(
                f"goal-flow replay used {result.transport_calls} steps; expected {EXPECTED_STEPS}"
            )
        return {
            "cpu_s": cpu_seconds,
            "steps": float(result.transport_calls),
            "deepcopies_per_step": round(counters["full_history_deepcopies"] / EXPECTED_STEPS, 6),
            "full_validations_per_step": round(counters["full_validations"] / EXPECTED_STEPS, 6),
        }
    finally:
        copy.deepcopy = original_deepcopy
        lauren_memory._validate_tool_history = original_validate
        ShortTermMemory.snapshot = original_base_snapshot
        JournaledShortTermMemory.snapshot = original_journaled_snapshot
        JournaledShortTermMemory.ensure_valid_and_persist = original_ensure_valid


if __name__ == "__main__":
    print(json.dumps(_run(), sort_keys=True))
