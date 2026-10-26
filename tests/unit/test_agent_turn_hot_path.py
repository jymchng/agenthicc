"""Agent-turn bookkeeping without full-history copies (AGX-12).

The optimized paths must be indistinguishable from the ones they replace:

* ``ensure_valid_and_persist`` validates first and returns before copying when
  the history is valid. That is sound only if lauren-ai's repair never changes
  a valid history — checked for 10,000 seeded random histories.
* The full method, on valid and invalid histories, returns the same flag,
  raises the same errors and leaves the same messages and journal records as
  the previous copy-repair-compare implementation, kept below as a reference.
* Copy-on-write snapshots restore exactly what deep snapshots restore across
  random sequences of appends, snapshots, restores and repairs.
"""

from __future__ import annotations

import copy
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from lauren_ai import ToolConversationIntegrityError
from lauren_ai._memory import ShortTermMemory, _repair_tool_history, _validate_tool_history

from agenthicc.memory.journaled import JournaledShortTermMemory

pytestmark = pytest.mark.unit

Message = dict[str, object]


# ── generated histories ──────────────────────────────────────────────────────


def _user(text: str) -> Message:
    return {"role": "user", "content": text}


def _assistant_text(text: str) -> Message:
    return {"role": "assistant", "content": text}


def _assistant_calls(call_ids: list[str]) -> Message:
    blocks: list[object] = [{"type": "text", "text": "calling"}]
    blocks.extend(
        {"type": "tool_use", "id": call_id, "name": "probe", "input": {"n": index}}
        for index, call_id in enumerate(call_ids)
    )
    return {"role": "assistant", "content": blocks}


def _results(call_ids: list[str]) -> Message:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": f"ok {call_id}"}
            for call_id in call_ids
        ],
    }


def _random_history(rng: random.Random) -> list[Message]:
    """A conversation of episodes, some of them deliberately damaged."""
    messages: list[Message] = [_user("start")]
    next_id = 0
    for _ in range(rng.randint(0, 8)):
        kind = rng.choice(
            [
                "text",
                "tools",
                "tools",
                "missing_result",
                "partial_results",
                "late_result",
                "unknown_result",
                "duplicate_call",
            ]
        )
        if kind == "text":
            messages.append(_assistant_text("answer"))
            messages.append(_user("next"))
            continue
        count = rng.randint(1, 3)
        ids = [f"call_{next_id + offset}" for offset in range(count)]
        next_id += count
        if kind == "duplicate_call":
            ids = [ids[0], ids[0]]
        messages.append(_assistant_calls(ids))
        if kind == "tools" or kind == "duplicate_call":
            messages.append(_results(list(dict.fromkeys(ids))))
        elif kind == "partial_results" and len(ids) > 1:
            messages.append(_results(ids[:-1]))
        elif kind == "late_result":
            messages.append(_user("interjection"))
            messages.append(_results(ids))
        elif kind == "unknown_result":
            messages.append(_results([*ids, "call_unknown"]))
        # "missing_result" (and single-call "partial_results") add no result.
    return messages


_histories = st.randoms(use_true_random=False).map(_random_history)


def test_repair_never_changes_a_valid_history() -> None:
    """The soundness condition for validating before copying, on 10,000 histories."""
    rng = random.Random(20261023)
    valid = 0
    for _ in range(10_000):
        history = _random_history(rng)
        if not _validate_tool_history(history).ok:
            continue
        valid += 1
        snapshot = copy.deepcopy(history)
        repaired, report = _repair_tool_history(history)
        assert report.ok
        assert repaired == snapshot
        assert all(after is before for after, before in zip(repaired, history, strict=True))
    assert valid > 1_000  # the generator covers valid histories, not only damaged ones


# ── a journal that records what would be written ─────────────────────────────


@dataclass
class _RecordingJournal:
    """Stands in for ``ConversationJournal``; serializes every write when made."""

    records: list[str] = field(default_factory=list)
    corrupt_trailing_records: int = 0

    @property
    def cursor(self) -> int:
        return len(self.records)

    def fold(self) -> tuple[list[object], None]:
        return [], None

    def fold_pending_tool_exchange_call_hashes(self) -> frozenset[str]:
        return frozenset()

    def __getattr__(self, name: str) -> Callable[..., None]:
        def record(*args: object, **kwargs: object) -> None:
            self.records.append(json.dumps([name, args, kwargs], sort_keys=True, default=repr))

        return record


def _memory(history: list[Message]) -> tuple[JournaledShortTermMemory, _RecordingJournal]:
    journal = _RecordingJournal()
    memory = JournaledShortTermMemory(journal, max_tokens=100_000)  # type: ignore[arg-type]
    memory._messages = copy.deepcopy(history)
    journal.records.clear()
    return memory, journal


def _reference_ensure_valid_and_persist(memory: JournaledShortTermMemory) -> bool:
    """The implementation AGX-12 replaced: copy, repair, validate, compare."""
    before = copy.deepcopy(memory._messages)
    candidate = copy.copy(memory)
    candidate._messages = copy.deepcopy(before)
    candidate._active_exchange = None
    candidate.ensure_valid()
    report = candidate.validate_tool_history()
    if not report.ok:
        from agenthicc.memory.tool_history import _integrity_error

        raise _integrity_error(report)
    changed = candidate._messages != before
    if changed:
        memory.commit_repaired_tool_history(candidate._messages, memory._summary)
    return changed


def _outcome(call: Callable[[], bool]) -> object:
    try:
        return call()
    except ToolConversationIntegrityError as exc:
        return ("error", type(exc).__name__, exc.code)


@pytest.mark.timeout(300)
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_histories)
def test_ensure_valid_and_persist_matches_the_copying_implementation(
    history: list[Message],
) -> None:
    optimized, optimized_journal = _memory(history)
    reference, reference_journal = _memory(history)

    assert _outcome(optimized.ensure_valid_and_persist) == _outcome(
        lambda: _reference_ensure_valid_and_persist(reference)
    )
    assert optimized._messages == reference._messages
    assert optimized_journal.records == reference_journal.records


def test_a_valid_history_is_not_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    memory, journal = _memory([_user("hi"), _assistant_calls(["a"]), _results(["a"])])
    copies: list[object] = []
    real_deepcopy = copy.deepcopy

    def counting(value: object, memo: dict[int, object] | None = None) -> object:
        copies.append(value)
        return real_deepcopy(value, memo)

    monkeypatch.setattr("agenthicc.memory.journaled.copy.deepcopy", counting)
    assert memory.ensure_valid_and_persist() is False
    assert copies == []
    assert journal.records == []


def test_an_interrupted_tail_is_still_repaired_and_journaled() -> None:
    memory, journal = _memory([_user("hi"), _assistant_calls(["a", "b"])])

    assert memory.ensure_valid_and_persist() is True
    assert memory.validate_tool_history().ok
    assert journal.records, "the repaired projection must be durable"


# ── copy-on-write snapshots ──────────────────────────────────────────────────


_Operation = tuple[str, int]


@st.composite
def _operations(draw: st.DrawFn) -> list[_Operation]:
    return draw(
        st.lists(
            st.tuples(
                st.sampled_from(
                    [
                        "user",
                        "text",
                        "tools",
                        "dangling",
                        "snapshot",
                        "restore",
                        "repair",
                        "trim",
                    ]
                ),
                st.integers(min_value=0, max_value=5),
            ),
            max_size=25,
        )
    )


def _apply(
    memory: JournaledShortTermMemory,
    operations: list[_Operation],
    snapshot: Callable[[JournaledShortTermMemory], object],
) -> list[object]:
    """Run *operations* and return the observable state after each one."""
    saved: list[object] = []
    states: list[object] = []
    call_number = 0
    for name, argument in operations:
        if name == "user":
            memory.add_user(f"user {argument}")
        elif name == "text":
            memory.add_assistant({"role": "assistant", "content": f"text {argument}"})
        elif name == "tools":
            ids = [f"c{call_number}_{index}" for index in range(argument % 3 + 1)]
            call_number += 1
            memory.add_assistant(_assistant_calls(ids))
            memory.add_tool_results(
                [
                    SimpleNamespace(tool_use_id=call_id, content="ok", is_error=False)
                    for call_id in ids
                ]
            )
        elif name == "dangling":
            memory.add_assistant(_assistant_calls([f"d{call_number}"]))
            call_number += 1
        elif name == "snapshot":
            saved.append(snapshot(memory))
        elif name == "restore" and saved:
            memory.restore(saved[argument % len(saved)])
        elif name == "repair":
            states.append(_outcome(memory.ensure_valid_and_persist))
        elif name == "trim":
            memory.trim_to_fit(8 * (argument + 1))
        states.append(copy.deepcopy(memory._messages))
    return states


@pytest.mark.timeout(300)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_operations())
def test_copy_on_write_snapshots_restore_what_deep_snapshots_restore(
    operations: list[_Operation],
) -> None:
    optimized, optimized_journal = _memory([_user("start")])
    reference, reference_journal = _memory([_user("start")])

    optimized_states = _apply(optimized, operations, JournaledShortTermMemory.snapshot)
    reference_states = _apply(reference, operations, ShortTermMemory.snapshot)

    assert optimized_states == reference_states
    assert optimized_journal.records == reference_journal.records


def test_a_snapshot_is_independent_of_later_appends() -> None:
    memory, _journal = _memory([_user("start")])
    checkpoint = memory.snapshot()
    memory.add_user("later")

    memory.restore(checkpoint)

    assert memory._messages == [_user("start")]
