"""Focused unit coverage for PRD-206's bounded manager projection."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore
from agenthicc.tui.workspace.background_manager import BackgroundManager, ViewportBudget

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str, now: float) -> BackgroundSession:
    artifact = tmp_path / "sessions" / session_id
    artifact.mkdir(parents=True, exist_ok=True)
    return BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="test",
        artifact_dir=str(artifact),
        now=now,
    )


def test_viewport_budget_reserves_detail_and_controls() -> None:
    short = ViewportBudget.from_terminal(80, 8)
    normal = ViewportBudget.from_terminal(120, 25)
    tiny = ViewportBudget.from_terminal(80, 5)

    assert short.table_rows == 1
    assert short.detail_height == 3
    assert normal.table_rows > 1
    assert normal.detail_height >= 4
    assert tiny.compact is True
    assert tiny.table_rows == 1


def test_manager_keeps_complete_render_inside_normal_and_short_viewports(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(20):
        store.create(_session(tmp_path, f"session-{index}", float(index)))

    for height in (8, 10, 15, 25):
        console = Console(width=140, height=height, record=True)
        manager = BackgroundManager(console, store=store)
        manager.refresh(force=True)
        console.print(manager.render())
        rendered_text = console.export_text(clear=False)
        lines = rendered_text.splitlines()
        assert len(lines) <= height
        assert "Enter attach" in rendered_text
        assert "▶" in rendered_text
        assert "Details · selected" in rendered_text
        if height >= 15:
            assert f"Dir: {tmp_path.name}" in rendered_text


def test_selection_is_session_id_stable_when_store_order_changes(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(3):
        store.create(_session(tmp_path, f"session-{index}", float(index)))
    manager = BackgroundManager(Console(), store=store)
    manager.refresh(force=True)
    manager.selected = next(
        index for index, item in enumerate(manager.sessions) if item.session_id == "session-1"
    )
    assert manager.selected_session_id == "session-1"

    store.update("session-2", last_active=100.0)
    manager.refresh(force=True)
    assert manager.selected_session_id == "session-1"
    assert manager.selected_session is not None
    assert manager.selected_session.session_id == "session-1"

    store.delete("session-1", force=True)
    manager.refresh(force=True)
    assert manager.selected_session_id != "session-1"
    assert manager.selected_session is not None


def test_enter_refuses_a_deleted_selection_instead_of_shifting_target(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "session-1", 1.0))
    store.create(_session(tmp_path, "session-2", 2.0))
    manager = BackgroundManager(Console(), store=store)
    manager.refresh(force=True)
    selected_id = manager.selected_session_id
    assert selected_id is not None
    store.delete(selected_id, force=True)

    result = manager.handle_key("ENTER")

    assert result is None or result.session_id != selected_id


def test_latest_activity_ignores_tool_rows_and_caches_tail_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, "activity", 1.0)
    path = Path(session.artifact_dir) / "conversation.jsonl"
    path.write_text(
        json.dumps({"kind": "text", "payload": {"text": "old text"}})
        + "\n"
        + json.dumps({"kind": "tool_complete", "payload": {"text": "secret tool"}})
        + "\n"
        + json.dumps({"kind": "user_message", "payload": {"text": "new text"}})
        + "\n",
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    manager = BackgroundManager(Console(), store=store)

    reads = 0
    original_read = Path.read_bytes

    def counted_read(target: Path) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(target)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    first = manager._activity_lines(session)
    second = manager._activity_lines(session)

    assert first == ["user_message: new text"]
    assert second == first
    assert reads == 1

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "tool_complete", "payload": {"text": "noise"}}) + "\n")
    assert manager._activity_lines(session) == first
    assert reads == 2


def test_store_projection_avoids_repeated_full_folds_and_detects_external_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "one", 1.0))
    assert len(store.list()) == 1

    reads = 0
    original_read = store._read_events

    def counted_read() -> list[dict[str, object]]:
        nonlocal reads
        reads += 1
        return original_read()

    monkeypatch.setattr(store, "_read_events", counted_read)
    assert len(store.list()) == 1
    assert reads == 0

    other = BackgroundStore(root)
    other.create(_session(tmp_path, "two", 2.0))
    assert {item.session_id for item in store.list()} == {"one", "two"}
    assert reads == 1


def test_render_and_maintenance_have_separate_side_effect_boundaries(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "one", 1.0))
    calls: list[str] = []

    class Supervisor:
        def recover_stale(self) -> list[BackgroundSession]:
            calls.append("recover")
            return []

    manager = BackgroundManager(Console(), store=store, supervisor=Supervisor())
    manager.refresh(force=True)
    manager.render()
    manager.render()
    assert calls == []
    manager.maintain(force=True)
    assert calls == ["recover"]


def test_interactive_delete_does_not_block_on_worker_cleanup(tmp_path: Path) -> None:
    from agenthicc.background import BackgroundSupervisor, SessionStatus
    from agenthicc.tui.cbreak_reader import Key

    session = _session(tmp_path, "slow-delete", 1.0)
    store = BackgroundStore(tmp_path / "background")
    store.create(session)
    supervisor = BackgroundSupervisor(store)
    started = threading.Event()
    release = threading.Event()
    delete = supervisor.delete

    def slow_delete(session_id: str) -> BackgroundSession:
        started.set()
        release.wait(timeout=2.0)
        return delete(session_id)

    supervisor.delete = slow_delete  # type: ignore[method-assign]
    manager = BackgroundManager(Console(), store=store, supervisor=supervisor)
    manager._interactive_delete = True

    manager.handle_key(Key.CTRL_X)
    started_at = time.monotonic()
    assert manager.handle_key(Key.CHAR, "y") is None
    assert time.monotonic() - started_at < 0.5
    assert started.wait(timeout=1.0)
    assert store.get(session.session_id).status is SessionStatus.QUEUED

    release.set()
    deadline = time.monotonic() + 2.0
    while manager._deletion_thread is not None and time.monotonic() < deadline:
        manager._poll_async_delete()
        time.sleep(0.01)
    manager._poll_async_delete()
    assert manager._deletion_thread is None
    assert store.get(session.session_id, include_deleted=True).status is SessionStatus.DELETED
    assert manager.sessions == []
