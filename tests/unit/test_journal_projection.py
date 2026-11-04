"""TUIR-03: journal queries without re-reading the file, writes off the loop."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenthicc.memory import journal as journal_module
from agenthicc.memory.journal import RECORD_DURABILITY, ConversationJournal

pytestmark = pytest.mark.unit


def _pool(journal: ConversationJournal, pool_id: str, *, background: bool = True) -> None:
    journal.subagent_pool_started(
        pool_id=pool_id,
        fingerprint=f"fp-{pool_id}",
        parent_run_id="turn-1",
        tasks=[{"task_id": "task-0", "agent_type": "explorer"}],
    )
    if background:
        journal.subagent_pool_backgrounded(
            pool_id=pool_id, tool_result={"status": "backgrounded", "pool_id": pool_id}
        )
    journal.subagent_pool_finished(pool_id=pool_id, total=1, succeeded=1, failed=0, text="done")


def test_queries_on_a_large_journal_do_not_reread_it(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        filler = {"kind": "usage_record", "schema_version": 1, "record": {"pad": "x" * 2_000}}
        for seq in range(25_000):  # ~50 MB
            handle.write(json.dumps({"seq": seq, **filler}) + "\n")
    journal = ConversationJournal(path)
    try:
        _pool(journal, "pool-1")
        journal.completed_background_subagent_pools()  # builds the projection once
        start = time.perf_counter()
        for _ in range(100):
            completed = journal.completed_background_subagent_pools()
            found = journal.find_subagent_pool("turn-1", "fp-pool-1")
        per_call = (time.perf_counter() - start) / 200
    finally:
        journal.close()

    assert [record["pool_id"] for record in completed] == ["pool-1"]
    assert found is not None
    assert per_call < 0.001, f"{per_call * 1000:.2f} ms per query"


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from(["start", "finish", "deliver", "usage"]), st.integers(0, 3)),
        max_size=30,
    ),
    st.booleans(),
)
def test_the_projection_equals_a_fresh_read(
    tmp_path_factory: pytest.TempPathFactory,
    operations: list[tuple[str, int]],
    torn_tail: bool,
) -> None:
    path = tmp_path_factory.mktemp("projection") / "journal.jsonl"
    journal = ConversationJournal(path)
    journal.fold_subagent_pool_lifecycle()  # projection built before the writes
    for operation, number in operations:
        pool_id = f"pool-{number}"
        if operation == "start":
            journal.subagent_pool_started(
                pool_id=pool_id, fingerprint="fp", parent_run_id="t", tasks=[]
            )
        elif operation == "finish":
            journal.subagent_pool_finished(
                pool_id=pool_id, total=1, succeeded=1, failed=0, text="x"
            )
        elif operation == "deliver":
            journal.subagent_pool_notification_delivered(pool_id=pool_id, message_id=pool_id)
        else:
            journal.append_usage_record({"record_id": f"r{number}"})
    incremental = (
        journal.fold_subagent_pool_lifecycle(),
        journal.completed_background_subagent_pools(),
        journal.delivered_subagent_pool_notifications(),
        len(journal.fold_usage_records()),
    )
    journal.close()
    if torn_tail:
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "subagent_pool_started", "pool_id": "torn"')
    reopened = ConversationJournal(path)
    try:
        fresh = (
            reopened.fold_subagent_pool_lifecycle(),
            reopened.completed_background_subagent_pools(),
            reopened.delivered_subagent_pool_notifications(),
            len(reopened.fold_usage_records()),
        )
    finally:
        reopened.close()
    assert fresh == incremental


async def test_usage_records_never_wait_for_a_slow_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = ConversationJournal(tmp_path / "journal.jsonl")
    real_fsync = os.fsync

    def slow_fsync(descriptor: int) -> None:
        time.sleep(0.05)
        real_fsync(descriptor)

    monkeypatch.setattr(journal_module.os, "fsync", slow_fsync)
    worst = 0.0

    async def worker(index: int) -> None:
        nonlocal worst
        for call in range(10):
            start = time.perf_counter()
            journal.append_usage_record({"record_id": f"w{index}-{call}"})
            worst = max(worst, time.perf_counter() - start)
            await asyncio.sleep(0.005)

    try:
        await asyncio.gather(*(worker(index) for index in range(6)))
        records = journal.fold_usage_records()
    finally:
        journal.close()

    assert len(records) == 60
    assert worst < 0.010, f"an append took {worst * 1000:.1f} ms"


def test_a_durable_record_is_on_disk_after_queued_usage_and_in_seq_order(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = ConversationJournal(path)
    try:
        journal.append_usage_record({"record_id": "u1"})
        journal.subagent_pool_started(pool_id="p", fingerprint="f", parent_run_id="t", tasks=[])
        sequences = [json.loads(line)["seq"] for line in path.read_text().splitlines()]
    finally:
        journal.close()
    assert sequences == [0, 1]


def test_every_record_kind_declares_its_durability() -> None:
    source = Path(journal_module.__file__).read_text(encoding="utf-8")
    kinds = set(re.findall(r'"kind": "([a-z_]+)"', source))
    assert kinds - set(RECORD_DURABILITY) == set()
    assert {kind for kind, mode in RECORD_DURABILITY.items() if mode == "advisory"} == {
        "usage_record"
    }
