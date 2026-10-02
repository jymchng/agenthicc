"""End-to-end coverage for agents manager details and targeted input."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.input_inbox import BackgroundInputInbox
from agenthicc.background.supervisor import BackgroundSupervisor
from agenthicc.tui.cbreak_reader import Key
from agenthicc.tui.workspace.background_manager import BackgroundManager, ManagerResult

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_enter_opens_session_details_then_second_enter_attaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_dir = tmp_path / "artifacts" / "detail-session"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "conversation.jsonl").write_text(
        '{"kind":"text","payload":{"text":"Implementation is ready"}}\n',
        encoding="utf-8",
    )
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "detail-session",
        title="Inspect before attaching",
        cwd=str(tmp_path),
        workflow_name="code_plan",
        intent="test session details",
        artifact_dir=str(artifact_dir),
        now=1_780_000_000.0,
    ).evolve(
        status=SessionStatus.COMPLETED,
        provider="openai",
        model="test-model",
        current_phase="verify",
        phase_history=("design", "implement", "verify"),
        completed_at=1_780_000_010.0,
        latest_activity="Implementation is ready",
    )
    store.create(session)
    projection_ready = threading.Event()
    details_rendered = threading.Event()
    pending_read_started = threading.Event()
    release_pending_read = threading.Event()
    pending_read_finished = threading.Event()
    query_page = store.query_page

    def query_page_and_signal(**kwargs: object):
        result = query_page(**kwargs)
        projection_ready.set()
        return result

    store.query_page = query_page_and_signal  # type: ignore[method-assign]

    class ScriptedBackend:
        calls = 0
        manager: BackgroundManager | None = None

        def is_interactive(self) -> bool:
            return True

        @contextmanager
        def enter_raw_mode(self):
            yield

        def read_key(self) -> tuple[Key, str]:
            self.calls += 1
            if self.calls == 1:
                if not projection_ready.wait(timeout=3.0):
                    raise TimeoutError("initial session index did not load")
                return Key.ENTER, ""
            if self.calls == 2:
                if not details_rendered.wait(timeout=3.0):
                    raise TimeoutError("session detail page was not rendered")
                return Key.ENTER, ""
            if self.calls == 3:
                pending_read_started.set()
                try:
                    if not release_pending_read.wait(timeout=3.0):
                        raise TimeoutError("manager shutdown did not release its pending key read")
                    return Key.UP, ""
                finally:
                    pending_read_finished.set()
            raise AssertionError("manager should have attached after the second Enter")

        def restore(self) -> None:
            release_pending_read.set()

    backend = ScriptedBackend()
    monkeypatch.setattr("agenthicc.tui.terminal.backend.get_backend", lambda: backend)

    class DetailRecordingManager(BackgroundManager):
        def render(self, *, all_sessions: bool = False):
            rendered = super().render(all_sessions=all_sessions)
            if self._detail_session_id == session.session_id:
                details_rendered.set()
            return rendered

    console = Console(width=120, height=25, record=True)
    manager = DetailRecordingManager(console, store=store)
    backend.manager = manager

    result = await manager.run()

    assert result == ManagerResult("attach", session.session_id)
    assert details_rendered.is_set()
    assert pending_read_started.is_set()
    assert release_pending_read.is_set()
    assert pending_read_finished.is_set()


@pytest.mark.asyncio
async def test_details_input_composer_targets_live_owner_without_attach(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "composer-target",
        title="Live target",
        cwd=str(tmp_path),
        workflow_name="code_plan",
        intent="keep working",
    ).evolve(
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token="live-owner-token",
        attempt=2,
    )
    store.create(session)
    manager = BackgroundManager(Console(width=100, height=30, record=True), store=store)
    from agenthicc.background.supervisor import _WorkerProcessState

    manager.supervisor._worker_process_state = lambda _session: _WorkerProcessState.LIVE  # type: ignore[method-assign]
    manager._sessions = [session]
    manager._visible_sessions = [session]
    manager._total_count = 1
    manager._selected_session_id = session.session_id
    manager._detail_session_id = session.session_id

    manager.handle_key(Key.CHAR, "i")
    assert manager._composer_open
    assert manager._composer_target is not None
    assert manager._composer_target.session_id == session.session_id
    assert manager._composer_input is not None
    from agenthicc.tui.runtime.commands import SendMessageCommand

    editor = manager._composer_input
    editor.set_text("/workflow goal_flow")
    first = await editor._bus.dispatch_async(SendMessageCommand(text="/workflow goal_flow"))
    assert first.ok
    assert not manager._composer_open
    # Trigger-picker submissions clear their editor after the target accepts;
    # model that final input cleanup while exercising the async bus path above.
    editor.set_text("")

    # Opening the same target again must start a fresh composer lifecycle.
    # Previously `_composer_submitted` remained true after the first send, so
    # the first character of this second message closed the composer and
    # returned to the details view.
    manager.handle_key(Key.CHAR, "i")
    assert manager._composer_open
    manager.handle_key(Key.CHAR, "s")
    assert manager._composer_task is not None
    await manager._composer_task
    assert manager._composer_open
    assert editor._buf.text == "s"
    editor.set_text("second input")
    manager.handle_key(Key.ENTER)
    assert manager._composer_task is not None
    await manager._composer_task

    receipts = BackgroundInputInbox(store).receipts(session.session_id)
    current = store.get(session.session_id)
    assert [receipt.text for receipt in receipts] == ["/workflow goal_flow", "second input"]
    assert all(receipt.state == "accepted" for receipt in receipts)
    assert manager._composer_history[f"{session.session_id}:{session.attempt}"] == [
        "second input",
    ]
    assert not manager._composer_open
    assert manager._detail_session_id == session.session_id
    assert current.worker_pid == session.worker_pid
    assert current.lease_token == session.lease_token
    assert current.status == SessionStatus.RUNNING

    activity, latest_receipt, history = manager._load_detail_projection(session)
    assert activity == []
    assert history == []
    assert latest_receipt is not None and "queued" in latest_receipt
    assert all(receipt.text not in latest_receipt for receipt in receipts)
    manager._input_receipt_text[session.session_id] = latest_receipt
    manager.console.print(manager._render_detail_page(session, manager.viewport_budget))
    details = manager.console.export_text()
    assert "Latest input" in details
    assert "queued" in details
    assert all(receipt.text not in details for receipt in receipts)
    await manager._service.close()


@pytest.mark.asyncio
async def test_completed_details_input_opens_and_starts_new_background_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "completed-input-target",
        title="Completed target",
        cwd=str(tmp_path),
        workflow_name="goal_flow",
        intent="the previous goal must not be replayed",
    ).evolve(
        status=SessionStatus.COMPLETED,
        attempt=4,
        lease_token="previous-attempt-lease",
        completed_at=1_780_000_000.0,
        worker_finished_at=1_780_000_000.0,
    )
    store.create(session)
    supervisor = BackgroundSupervisor(store)
    manager = BackgroundManager(
        Console(width=100, height=30, record=True),
        store=store,
        supervisor=supervisor,
    )
    manager._sessions = [session]
    manager._visible_sessions = [session]
    manager._total_count = 1
    manager._selected_session_id = session.session_id
    manager._detail_session_id = session.session_id

    def prepare_editor(_target: BackgroundSession) -> None:
        manager._composer_input = object()  # type: ignore[assignment]

    monkeypatch.setattr(manager, "_build_target_input_editor", prepare_editor)
    launches: list[tuple[object, BackgroundSession]] = []

    def capture_launch(request: object, starting: BackgroundSession) -> BackgroundSession:
        launches.append((request, starting))
        return starting

    monkeypatch.setattr(supervisor, "_launch", capture_launch)
    monkeypatch.setattr(
        supervisor,
        "attach_foreground",
        lambda _session_id: (_ for _ in ()).throw(
            AssertionError("completed-session input must not attach to foreground")
        ),
    )

    manager.handle_key(Key.CHAR, "i")
    assert manager._composer_open
    assert manager._composer_target is not None
    assert manager._composer_target.session_id == session.session_id

    from agenthicc.tui.runtime.commands import SendMessageCommand

    command_text = "  /workflow goal_flow  \n"
    result = await manager._submit_target_input(
        session,
        (session.session_id, session.attempt),
        SendMessageCommand(text=command_text),
    )
    assert result.ok
    assert len(launches) == 1
    request, starting = launches[0]
    assert getattr(request, "run_id") == ""
    assert not getattr(request, "detached_goal")
    assert starting.status is SessionStatus.STARTING
    assert starting.session_id == session.session_id
    assert starting.resume_marker.startswith("input:")
    assert starting.completed_at is None
    assert manager._detail_session_id == session.session_id

    receipt = BackgroundInputInbox(store).receipts(session.session_id)
    assert len(receipt) == 1
    assert receipt[0].text == command_text
    assert receipt[0].state == "accepted"
    assert receipt[0].deferred
    assert "background recovery" in manager._composer_receipt.casefold()
    await manager._service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.ORPHANED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.ARCHIVED,
    ],
)
async def test_details_input_recovers_stale_session_without_foreground_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: SessionStatus,
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        f"recover-{status.value}",
        title="Recover without attach",
        cwd=str(tmp_path),
        workflow_name="code_plan",
        intent="original request",
    ).evolve(
        status=status,
        attempt=4,
        lease_token="previous-owner-lease",
    )
    store.create(session)
    supervisor = BackgroundSupervisor(store)
    manager = BackgroundManager(
        Console(width=100, height=30, record=True),
        store=store,
        supervisor=supervisor,
    )

    def prepare_editor(_target: BackgroundSession) -> None:
        manager._composer_input = object()  # type: ignore[assignment]

    monkeypatch.setattr(manager, "_build_target_input_editor", prepare_editor)
    resumed: list[str] = []

    def resume(session_id: str) -> BackgroundSession:
        resumed.append(session_id)
        return store.get(session_id)

    def attach(_session_id: str) -> BackgroundSession:
        raise AssertionError("background recovery must not attach to the foreground")

    monkeypatch.setattr(supervisor, "resume", resume)
    monkeypatch.setattr(supervisor, "attach_foreground", attach)
    manager._open_input_composer(session)
    assert manager._composer_open

    from agenthicc.tui.runtime.commands import SendMessageCommand

    result = await manager._submit_target_input(
        session,
        (session.session_id, session.attempt),
        SendMessageCommand(text="continue this recoverable session"),
    )
    try:
        assert result.ok
        assert resumed == [session.session_id]
        receipt = BackgroundInputInbox(store).receipts(session.session_id)[0]
        assert receipt.text == "continue this recoverable session"
        assert receipt.state == "accepted"
        assert receipt.deferred
        assert "background recovery" in manager._composer_receipt.casefold()
        assert store.get(session.session_id).status is status
    finally:
        await manager._service.close()


@pytest.mark.asyncio
async def test_deleted_session_input_stays_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "deleted-input-target",
        title="Deleted target",
        cwd=str(tmp_path),
        workflow_name="",
        intent="do not revive",
    ).evolve(status=SessionStatus.DELETED)
    store.create(session)
    manager = BackgroundManager(Console(width=100, height=30, record=True), store=store)
    manager._build_target_input_editor = lambda _target: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("deleted session must not construct an input editor")
    )

    manager._open_input_composer(session)

    assert not manager._composer_open
    assert "deleted" in manager._notice
    await manager._service.close()


def test_agents_pages_show_at_most_ten_non_deleted_sessions(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    for index in range(12):
        status = SessionStatus.DELETED if index == 0 else SessionStatus.COMPLETED
        session = BackgroundSession.create(
            f"page-session-{index:02d}",
            title=f"Session {index:02d}",
            cwd=str(tmp_path),
            workflow_name="",
            intent="pagination test",
        ).evolve(status=status)
        store.create(session)

    manager = BackgroundManager(Console(width=120, height=40, record=True), store=store)
    sessions = manager.refresh(force=True)

    assert manager.page_size == 10
    assert manager.page_count == 2
    assert len(sessions) == 11
    assert all(item.status is not SessionStatus.DELETED for item in sessions)
    assert manager._page_bounds() == (0, 10)
    manager.selected = 10
    assert manager._page_bounds() == (10, 11)


def test_details_page_footer_stays_visible_while_metadata_scrolls(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts" / "scroll-session"
    artifact_dir.mkdir(parents=True)
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "scroll-session",
        title="Long session details",
        cwd=str(tmp_path),
        workflow_name="",
        intent="review session details",
        artifact_dir=str(artifact_dir),
    ).evolve(status=SessionStatus.COMPLETED)
    store.create(session)
    console = Console(width=100, height=12, record=True)
    manager = BackgroundManager(console, store=store)
    manager._sessions = [session]
    manager._visible_sessions = [session]
    manager._total_count = 1
    manager._selected_session_id = session.session_id
    manager._detail_session_id = session.session_id

    manager.handle_key(Key.END)
    console.print(manager.render())
    rendered = console.export_text()

    assert manager._detail_session_id == session.session_id
    assert manager._selected_session_id == session.session_id
    assert "Updated:" in rendered
    assert "Home/End" in rendered
    assert len(rendered.splitlines()) <= console.height

    manager.handle_key(Key.HOME)
    manager.handle_key(Key.DOWN)
    assert manager._detail_scroll == 1
    assert manager._detail_session_id == session.session_id
    assert manager._selected_session_id == session.session_id
    manager.handle_key(Key.HOME)
    for _ in range(3):
        manager.handle_key(Key.PAGE_DOWN)
    console.print(manager.render())
    rendered = console.export_text()
    assert "Updated:" in rendered


@pytest.mark.asyncio
async def test_target_input_context_build_does_not_block_manager_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BackgroundStore(tmp_path / "background")
    session = BackgroundSession.create(
        "async-composer-target",
        title="Slow target context",
        cwd=str(tmp_path),
        workflow_name="",
        intent="continue",
    ).evolve(
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token="slow-context-owner",
        attempt=1,
    )
    store.create(session)
    manager = BackgroundManager(Console(width=100, height=30), store=store)
    manager._async_mode = True
    manager._visible_sessions = [session]
    manager._selected_session_id = session.session_id
    manager._detail_session_id = session.session_id
    started = threading.Event()
    release = threading.Event()

    class FakeEditor:
        def __init__(self) -> None:
            self.keys: list[tuple[object, str]] = []

        async def _dispatch(self, key: object, char: str) -> None:
            self.keys.append((key, char))

    editor = FakeEditor()

    def slow_builder(target: BackgroundSession) -> None:
        assert target.session_id == session.session_id
        started.set()
        if not release.wait(timeout=2):
            raise TimeoutError("test did not release composer preparation")
        manager._composer_target = target
        manager._composer_input = editor  # type: ignore[assignment]

    monkeypatch.setattr(manager, "_build_target_input_editor", slow_builder)
    try:
        manager.handle_key(Key.CHAR, "i")
        assert manager._composer_open and manager._composer_building
        assert await asyncio.to_thread(started.wait, 1)

        # The target context is deliberately blocked in a worker thread, but
        # input dispatch still returns and retains the key in the small FIFO.
        manager.handle_key(Key.CHAR, "x")
        assert len(manager._composer_pending_keys) == 1
        release.set()
        assert manager._composer_build_task is not None
        await manager._composer_build_task
        assert editor.keys == [(Key.CHAR, "x")]
        assert manager._composer_open
        assert not manager._composer_building
    finally:
        release.set()
        await manager._service.close()
