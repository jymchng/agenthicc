"""Integration coverage for the durable PRD-206 projection boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore
from agenthicc.tui.workspace.background_manager import BackgroundManager

pytestmark = pytest.mark.integration


def _write_sessions(store: BackgroundStore, root: Path, count: int) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    records = []
    for index in range(count):
        session = BackgroundSession.create(
            f"session-{index:04d}",
            title=f"Session {index}",
            cwd=str(root),
            workflow_name="demo",
            intent="integration fixture",
            artifact_dir=str(root / "sessions" / f"session-{index:04d}"),
            now=float(index),
        )
        records.append(
            json.dumps(
                {
                    "seq": index + 1,
                    "event_type": "created",
                    "timestamp": float(index),
                    "payload": session.to_dict(),
                },
                separators=(",", ":"),
            )
        )
    store.events_path.write_text("\n".join(records) + "\n", encoding="utf-8")


def test_large_store_renders_one_interactive_page_but_keeps_projection_complete(
    tmp_path: Path,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _write_sessions(store, tmp_path, 1_000)
    manager = BackgroundManager(Console(width=140, height=25), store=store)

    manager.refresh(force=True)
    rendered = manager.render()

    assert len(manager.sessions) == 1_000
    # The interactive projection contains only the current page, while the
    # backing store/list API remains complete.
    assert manager.page_count > 1
    output = Console(width=140, height=25, record=True)
    output.print(rendered)
    text = output.export_text(clear=False)
    assert "session-0999" in text  # selected row/detail on the newest page
    assert "Session 988" in text
    assert "Session 984" not in text


def test_unchanged_repaints_do_not_fold_or_read_journals(tmp_path: Path, monkeypatch) -> None:
    store = BackgroundStore(tmp_path / "background")
    _write_sessions(store, tmp_path, 30)
    manager = BackgroundManager(Console(width=140, height=25), store=store)
    manager.refresh(force=True)
    manager.render()

    fold_reads = 0
    original_read = store._read_events

    def counted_read() -> list[dict[str, object]]:
        nonlocal fold_reads
        fold_reads += 1
        return original_read()

    monkeypatch.setattr(store, "_read_events", counted_read)
    first = manager.render()
    second = manager.render()

    assert first is second
    assert fold_reads == 0


def test_external_session_change_invalidates_projection_without_rebuilding_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    _write_sessions(store, tmp_path, 4)
    manager = BackgroundManager(Console(), store=store)
    manager.refresh(force=True)

    reads = 0
    original_read = store._read_events

    def counted_read() -> list[dict[str, object]]:
        nonlocal reads
        reads += 1
        return original_read()

    monkeypatch.setattr(store, "_read_events", counted_read)
    other = BackgroundStore(store.root)
    other.update("session-0000", latest_activity="changed externally")
    manager.refresh(force=True)
    manager.refresh(force=True)

    assert reads == 1
    assert store.get("session-0000").latest_activity == "changed externally"
