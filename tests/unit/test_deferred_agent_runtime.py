"""Conversation memory and provider load after the first frame (AGX-02)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agenthicc.runners.session_conversation import SessionConversation
from agenthicc.runners.startup import ReadinessState, StartupCoordinator
from agenthicc.runners.tui_session import _build_agent_runner, _LazyAgentRunner

if TYPE_CHECKING:
    from lauren_ai._config import LLMConfig

pytestmark = pytest.mark.unit


def _seed_journal(path: Path) -> None:
    """Write a two-message conversation through the normal journaled memory."""
    conversation = SessionConversation.open("seeded", max_tokens=10_000, journal_path=path)
    conversation.memory.add_user("first question")
    conversation.close()


class _Budget:
    """A ``max_tokens`` factory that records when and where it was evaluated."""

    def __init__(self, release: threading.Event | None = None) -> None:
        self.calls: list[threading.Thread] = []
        self._release = release

    def __call__(self) -> int:
        self.calls.append(threading.current_thread())
        if self._release is not None:
            assert self._release.wait(5), "test never released the fold"
        return 10_000


def test_deferred_conversation_folds_nothing_until_memory_is_needed(tmp_path: Path) -> None:
    journal = tmp_path / "conversation.jsonl"
    _seed_journal(journal)
    budget = _Budget()

    conversation = SessionConversation.open_deferred(
        "seeded", max_tokens=budget, journal_path=journal
    )

    assert not conversation.memory_loaded
    assert budget.calls == []
    assert conversation.cursor > 0  # the journal itself is open
    assert [message["role"] for message in conversation.memory.messages()] == ["user"]
    assert conversation.memory_loaded
    assert len(budget.calls) == 1
    conversation.close()


def test_eager_open_still_folds_immediately(tmp_path: Path) -> None:
    journal = tmp_path / "conversation.jsonl"
    _seed_journal(journal)
    conversation = SessionConversation.open("seeded", max_tokens=10_000, journal_path=journal)
    assert conversation.memory_loaded
    conversation.close()


async def test_load_folds_on_a_worker_thread_once(tmp_path: Path) -> None:
    journal = tmp_path / "conversation.jsonl"
    _seed_journal(journal)
    budget = _Budget()
    conversation = SessionConversation.open_deferred(
        "seeded", max_tokens=budget, journal_path=journal
    )

    loaded = await conversation.load()

    assert loaded is conversation.memory
    assert await conversation.load() is loaded
    assert len(budget.calls) == 1
    assert budget.calls[0] is not threading.current_thread()
    conversation.close()


async def test_memory_access_during_a_fold_waits_for_the_same_object(tmp_path: Path) -> None:
    journal = tmp_path / "conversation.jsonl"
    _seed_journal(journal)
    release = threading.Event()
    budget = _Budget(release)
    conversation = SessionConversation.open_deferred(
        "seeded", max_tokens=budget, journal_path=journal
    )

    loading = asyncio.ensure_future(conversation.load())
    while not budget.calls:  # the worker thread now holds the fold lock
        await asyncio.sleep(0.01)
    seen: list[object] = []
    reader = threading.Thread(target=lambda: seen.append(conversation.memory))
    reader.start()
    release.set()
    memory = await loading
    reader.join(5)

    assert seen == [memory]
    assert len(budget.calls) == 1
    conversation.close()


async def test_close_waits_for_an_in_flight_fold(tmp_path: Path) -> None:
    journal = tmp_path / "conversation.jsonl"
    _seed_journal(journal)
    release = threading.Event()
    budget = _Budget(release)
    conversation = SessionConversation.open_deferred(
        "seeded", max_tokens=budget, journal_path=journal
    )
    loading = asyncio.ensure_future(conversation.load())
    while not budget.calls:
        await asyncio.sleep(0.01)

    closed = threading.Event()

    def close() -> None:
        conversation.close()
        closed.set()

    closer = threading.Thread(target=close)
    closer.start()
    assert not closed.wait(0.2)  # blocked behind the fold
    release.set()
    await loading
    closer.join(5)
    assert closed.is_set()


# ── provider runner ──────────────────────────────────────────────────────────


class _RunnerFactory:
    def __init__(self, *, fail: bool = False) -> None:
        self.threads: list[threading.Thread] = []
        self.fail = fail

    def __call__(self) -> object:
        self.threads.append(threading.current_thread())
        if self.fail:
            raise ValueError("Unknown LLM provider: 'nope'")
        return object()


def _lazy_runner(
    factory: _RunnerFactory, startup: StartupCoordinator | None, monkeypatch: pytest.MonkeyPatch
) -> _LazyAgentRunner:
    def unused_config() -> LLMConfig:
        raise AssertionError("the patched factory replaces config construction")

    runner = _LazyAgentRunner(unused_config, cassette_dir=None, startup=startup)
    monkeypatch.setattr(runner, "_create", factory)
    return runner


async def test_warm_up_builds_the_runner_off_the_loop_and_records_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup = StartupCoordinator()
    factory = _RunnerFactory()
    runner = _lazy_runner(factory, startup, monkeypatch)

    await runner.warm_up()

    assert startup.report("provider").state is ReadinessState.READY
    assert startup.report("provider").deferred
    assert factory.threads[0] is not threading.current_thread()
    delegate = runner.prepare()
    assert runner.prepare() is delegate
    assert len(factory.threads) == 1


async def test_warm_up_failure_is_recorded_and_retried_on_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenthicc.runners.startup import StartupDependencyError

    startup = StartupCoordinator()
    factory = _RunnerFactory(fail=True)
    runner = _lazy_runner(factory, startup, monkeypatch)

    with pytest.raises(ValueError, match="Unknown LLM provider"):
        await runner.warm_up()
    assert startup.report("provider").state is ReadinessState.FAILED
    with pytest.raises(StartupDependencyError):
        await startup.wait_for("provider")

    factory.fail = False
    assert runner.prepare() is not None
    assert startup.report("provider").state is ReadinessState.READY


def test_tui_provider_config_is_built_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real factory path builds an LLM config only when the runner is created."""
    from agenthicc.config import AgenthiccConfig, build_llm_config

    calls: list[str] = []
    cfg = AgenthiccConfig()
    cfg.execution.provider = "ollama"

    def llm_config() -> LLMConfig:
        calls.append("built")
        return build_llm_config(cfg.execution, conversation_id="session-1")

    runner = _build_agent_runner(llm_config)
    assert runner is not None
    assert calls == []
    assert runner.prepare() is not None
    assert calls == ["built"]
