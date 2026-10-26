"""``sessions list`` opens saved sessions in the agents hub (AGX-05 §5.5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agenthicc.cli.context import CLIContext, CLIFlags

pytestmark = pytest.mark.unit


def test_sessions_list_on_a_terminal_opens_the_history_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from agenthicc.cli.commands import background, sessions

    opened: list[str] = []

    async def open_hub(ctx: CLIContext, *, tab: str = "", **_kwargs: str) -> None:
        opened.append(tab)

    monkeypatch.setattr(background, "_open_hub", open_hub)
    monkeypatch.setattr(
        "agenthicc.tui.terminal.backend.get_backend",
        lambda: SimpleNamespace(is_interactive=lambda: True),
    )
    sessions.sessions_list(CLIContext())
    assert opened == ["history"]


def test_sessions_list_without_a_terminal_prints_the_saved_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    from agenthicc.cli.commands import sessions

    monkeypatch.setattr(
        "agenthicc.tui.terminal.backend.get_backend",
        lambda: SimpleNamespace(is_interactive=lambda: False),
    )
    monkeypatch.setattr(
        "agenthicc.sessions._load_all_session_indexes",
        lambda: {"session-1": {"cwd": "/work", "last_used": 1.0}},
    )
    sessions.sessions_list(CLIContext())
    assert "session-1" in capsys.readouterr().out


def test_a_saved_session_opened_from_the_hub_resumes_in_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.cli.commands import sessions

    captured: dict[str, object] = {}

    async def resume(**kwargs: object) -> None:
        captured.update(kwargs)

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
    asyncio.run(sessions.open_saved_session(ctx, "session-1"))

    assert captured == {
        "resume_id": "session-1",
        "cli_overrides": ["execution.model=test"],
        "record_cassette": str(tmp_path / "cassette"),
        "cli_flags": CLIFlags(dangerously_skip_permissions=True),
        "config_path": str(tmp_path / "agenthicc.toml"),
        "cli_secret_overrides": ["execution.api_key=TEST_KEY"],
        "cwd": str(tmp_path),
        "config": None,
    }
