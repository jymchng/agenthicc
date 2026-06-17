"""Shared fixtures for the agenthicc test suite (PRD-08)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest


# ── cassette mark: skipped unless the file is explicitly targeted ─────────────

_CASSETTE_FILE = Path(__file__).parent / "integration" / "test_cassette_replay.py"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "cassette: slow cassette-replay regression tests. Skipped by default; "
        "run with: pytest tests/integration/test_cassette_replay.py  or  --run-cassette",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-cassette",
        action="store_true",
        default=False,
        help="Include cassette-replay regression tests (skipped by default).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if config.getoption("--run-cassette", default=False):
        return

    # Run if the cassette file (or a parent dir containing it) was given explicitly.
    explicitly_targeted = any(
        _CASSETTE_FILE.resolve() == Path(arg).resolve()
        or (Path(arg).is_dir() and _CASSETTE_FILE.is_relative_to(Path(arg).resolve()))
        for arg in config.args
        if Path(arg).exists()
    )
    if explicitly_targeted:
        return

    skip = pytest.mark.skip(
        reason=(
            "cassette-replay tests are skipped by default (slow). "
            "Run them with:  pytest tests/integration/test_cassette_replay.py  "
            "or add --run-cassette to any pytest invocation."
        )
    )
    for item in items:
        if item.get_closest_marker("cassette"):
            item.add_marker(skip)

from agenthicc.kernel import (
    AppState,
    Event,
    EventProcessor,
    SecurityPolicy,
    SystemSettings,
    root_reducer,
)


@pytest.fixture
def tmp_settings(tmp_path) -> SystemSettings:
    return SystemSettings(
        event_log_path=str(tmp_path / ".agenthicc" / "events.jsonl"),
        snapshot_path=str(tmp_path / ".agenthicc" / "snapshot.json"),
        max_parallel_tasks=5,
        agent_pool_size=5,
        snapshot_every_n_events=1000,
    )


@pytest.fixture
def fresh_appstate(tmp_settings) -> AppState:
    return AppState.create(settings=tmp_settings, policy=SecurityPolicy())


class EventBusTestHarness:
    """Wraps EventProcessor, capturing all events for assertion."""

    def __init__(self, initial_state: AppState) -> None:
        self.captured: list[Event] = []
        self.processor = EventProcessor(
            initial_state=initial_state,
            reducer=self._capturing_reducer,
            persist=False,
        )

    def _capturing_reducer(self, state: AppState, event: Event):
        self.captured.append(event)
        return root_reducer(state, event)

    def events_of_type(self, event_type: str) -> list[Event]:
        return [e for e in self.captured if e.event_type == event_type]

    async def wait_for_event(self, event_type: str, timeout: float = 2.0) -> Event:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = self.events_of_type(event_type)
            if matches:
                return matches[-1]
            await asyncio.sleep(0.005)
        raise TimeoutError(f"Event {event_type!r} not seen within {timeout}s")


@pytest.fixture
async def harness(fresh_appstate):
    h = EventBusTestHarness(fresh_appstate)
    task = asyncio.create_task(h.processor.run())
    yield h
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def running_processor(fresh_appstate):
    processor = EventProcessor(initial_state=fresh_appstate, persist=False)
    task = asyncio.create_task(processor.run())
    yield processor
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def make_mock_transport(responses: list[Any]):
    """Build a lauren-ai MockTransport pre-loaded with completions.

    Each entry is either a plain string (end_turn text response) or a dict
    {"tool_calls": [{"name": ..., "input": {...}}], "content": "..."} which
    becomes a tool_use completion.
    """
    from lauren_ai._transport import Completion, TokenUsage, ToolCall
    from lauren_ai._transport._mock import MockTransport

    mock = MockTransport()
    for i, response in enumerate(responses):
        if isinstance(response, dict) and "tool_calls" in response:
            tool_calls = [
                ToolCall(tool_use_id=f"tc-{i}-{j}", name=tc["name"], input=tc["input"])
                for j, tc in enumerate(response["tool_calls"])
            ]
            mock.queue_response(Completion(
                id=f"mock-{i}",
                model="mock",
                content=response.get("content", ""),
                tool_calls=tool_calls,
                stop_reason="tool_use",
                usage=TokenUsage(input_tokens=10, output_tokens=10),
            ))
        else:
            mock.queue_response(Completion(
                id=f"mock-{i}",
                model="mock",
                content=str(response),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=10, output_tokens=10),
            ))
    return mock


@pytest.fixture
def mock_transport_factory():
    return make_mock_transport
