"""Unit coverage for canonical mode persistence and legacy migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.tui.runtime.mode_manager import ModeManager

pytestmark = pytest.mark.unit


def _redirect_session_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import agenthicc.tui.runtime.session_log as session_log

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_log, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(session_log, "_SESSION_INDEX", sessions / "index.json")


def test_new_session_metadata_starts_with_safe_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.tui.runtime.session_log import load_session_mode, register_session

    _redirect_session_store(monkeypatch, tmp_path)
    register_session("session-1", "/project", "model")

    assert load_session_mode("session-1") == "Safe"
    metadata = json.loads(
        (tmp_path / "sessions" / "session-1" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["mode"] == "Safe"


@pytest.mark.parametrize("mode", ["Safe", "Plan", "Yolo"])
def test_mode_changes_persist_only_canonical_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    from agenthicc.tui.runtime.session_log import (
        load_session_mode,
        register_session,
        update_session_mode,
    )

    _redirect_session_store(monkeypatch, tmp_path)
    register_session("session-2", "/project", "model")
    update_session_mode("session-2", mode)

    assert load_session_mode("session-2") == mode


def test_legacy_auto_state_resolves_to_yolo_and_is_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.tui.runtime.session_log import (
        load_session_mode,
        register_session,
        update_session_mode,
    )

    _redirect_session_store(monkeypatch, tmp_path)
    register_session("session-3", "/project", "model")
    update_session_mode("session-3", "Auto")

    manager = ModeManager()
    persisted = load_session_mode("session-3")
    assert persisted is not None
    assert manager.set_by_name(persisted).name == "Yolo"  # type: ignore[union-attr]
    update_session_mode("session-3", manager.active_name)

    assert load_session_mode("session-3") == "Yolo"


def test_missing_and_corrupt_metadata_are_safe_to_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agenthicc.tui.runtime.session_log import load_session_mode

    _redirect_session_store(monkeypatch, tmp_path)
    assert load_session_mode("missing") is None

    metadata = tmp_path / "sessions" / "broken" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("not json", encoding="utf-8")
    assert load_session_mode("broken") is None


def test_unknown_persisted_mode_is_not_silently_yolo() -> None:
    manager = ModeManager()

    assert manager.set_by_name("Debug") is None
    assert manager.active_name == "Safe"
