"""End-to-end coverage for the agents manager's two-step attach flow."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
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
