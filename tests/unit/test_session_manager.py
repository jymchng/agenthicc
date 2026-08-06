"""Tests for the interactive saved-session selector."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from agenthicc.cli.context import CLIContext, CLIFlags
from agenthicc.tui.workspace.session_manager import SessionManager, SessionManagerResult

pytestmark = pytest.mark.unit


def _records(tmp_path: Path) -> list[tuple[str, dict[str, object]]]:
    return [
        (
            f"session-{index}",
            {"cwd": str(tmp_path), "last_used": float(index), "model": "test-model"},
        )
        for index in range(5)
    ]


def test_session_manager_paginates_and_enter_opens_selected_session(tmp_path: Path) -> None:
    console = Console(record=True)
    manager = SessionManager(
        console,
        loader=lambda: _records(tmp_path),
        page_size=2,
    )

    console.print(manager.render())
    output = console.export_text()
    assert "Saved Sessions (page 1/3)" in output
    assert "session-0" in output
    assert "session-2" not in output

    manager.handle_key("PAGE_DOWN")
    assert manager.selected_record is not None
    assert manager.selected_record[0] == "session-2"
    manager.handle_key("END")
    assert manager.handle_key("ENTER") == SessionManagerResult("open", "session-4")


def test_sessions_list_enter_uses_resume_pipeline_and_session_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.cli.commands import sessions

    captured: dict[str, object] = {}

    async def select(*args: object, **kwargs: object) -> SessionManagerResult:
        return SessionManagerResult("open", "session-1")

    async def resume(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        "agenthicc.tui.workspace.session_manager.run_session_manager",
        select,
    )
    monkeypatch.setattr(
        "agenthicc.sessions._load_all_session_indexes",
        lambda: {"session-1": {"cwd": str(tmp_path), "last_used": 1.0}},
    )
    monkeypatch.setattr("agenthicc.runners.tui_session._run_tui_session", resume)

    ctx = CLIContext(
        config_path=str(tmp_path / "agenthicc.toml"),
        record_cassette=str(tmp_path / "cassette"),
        flags=CLIFlags(dangerously_skip_permissions=True),
        set_overrides=("execution.model=test",),
        set_secret_overrides=("execution.api_key=TEST_KEY",),
    )
    asyncio.run(sessions._open_selected_session(ctx, page=1, page_size=0))

    assert captured == {
        "resume_id": "session-1",
        "cli_overrides": ["execution.model=test"],
        "record_cassette": str(tmp_path / "cassette"),
        "cli_flags": CLIFlags(dangerously_skip_permissions=True),
        "config_path": str(tmp_path / "agenthicc.toml"),
        "cli_secret_overrides": ["execution.api_key=TEST_KEY"],
        "cwd": str(tmp_path),
    }
