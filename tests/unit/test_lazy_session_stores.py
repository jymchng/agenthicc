"""Session-owned SQLite stores open on first use, not before the first frame (AGX-02)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agenthicc.background.inbox import BackgroundInteractionInbox
from agenthicc.background.store import BackgroundStore
from agenthicc.memory.layers import GlobalMemoryLayer, ProjectMemoryLayer
from agenthicc.tools.fs.file_cache import WorkspaceFileCache

pytestmark = pytest.mark.unit


async def test_memory_layers_create_their_database_on_first_use(tmp_path: Path) -> None:
    project_db = tmp_path / "memory" / "project.db"
    global_db = tmp_path / "home" / "global.db"
    project = ProjectMemoryLayer(project_db)
    global_layer = GlobalMemoryLayer(global_db)

    assert not project_db.parent.exists()
    assert not global_db.parent.exists()

    await project.set("answer", 42)
    assert project_db.exists()
    assert await project.get("answer") == (True, 42)
    assert await global_layer.get("missing") == (False, None)
    assert global_db.exists()


def test_concurrent_first_uses_create_the_schema_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = ProjectMemoryLayer(tmp_path / "project.db")
    opened: list[str] = []
    original = ProjectMemoryLayer._open

    def counting(self: ProjectMemoryLayer) -> sqlite3.Connection:
        opened.append(threading.current_thread().name)
        return original(self)

    monkeypatch.setattr(ProjectMemoryLayer, "_open", counting)
    barrier = threading.Barrier(8)

    def first_use() -> None:
        barrier.wait()
        layer._init_schema()

    threads = [threading.Thread(target=first_use) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert layer._schema_ready
    assert len(opened) == 1


def test_background_inbox_creates_its_database_on_first_use(tmp_path: Path) -> None:
    root = tmp_path / "background"
    inbox = BackgroundInteractionInbox(BackgroundStore(root))
    database = root / "operator-inbox.sqlite3"
    assert not database.exists()

    assert inbox.list_items() == ()
    assert database.exists()
    assert database.stat().st_mode & 0o777 == 0o600


def test_file_cache_opens_on_first_use(tmp_path: Path) -> None:
    database = tmp_path / "cache" / "file-cache.db"
    cache = WorkspaceFileCache(database)
    assert not database.parent.exists()
    cache.close()  # closing an unopened cache is a no-op
    assert not database.parent.exists()

    source = tmp_path / "module.py"
    source.write_text("print('hi')\n", encoding="utf-8")
    cache = WorkspaceFileCache(database)
    cache.store(str(source), "print('hi')\n")
    assert database.exists()
    assert cache.get_fresh(str(source)) == "print('hi')\n"
    assert len(cache) == 1
    cache.close()


async def test_the_agents_manager_prepares_its_inbox_off_the_event_loop(tmp_path: Path) -> None:
    from agenthicc.background.agents_service import AgentsService
    from agenthicc.background.supervisor import BackgroundSupervisor

    root = tmp_path / "background"
    store = BackgroundStore(root)
    service = AgentsService(store, BackgroundSupervisor(store))
    database = root / "operator-inbox.sqlite3"
    assert not database.exists()

    await service.prepare_inbox_async()

    assert database.exists()
    await service.close()
