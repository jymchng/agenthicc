"""A hosted interactive runtime is findable while it runs, and never after it died."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenthicc.background.model import SessionStatus
from agenthicc.runners.session_lease import (
    SessionOwnerStore,
    discover_interactive_runtimes,
    interactive_registry_dir,
    register_interactive_runtime,
    unregister_interactive_runtime,
)
from agenthicc.tui.workspace.agents_hub.sources import INTERACTIVE_SOURCE, load_interactive

pytestmark = pytest.mark.unit


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    root = home / ".agenthicc" / "sessions"
    root.mkdir(parents=True)
    return root


def test_a_running_runtime_is_listed_with_its_session_directory(
    sessions: Path, tmp_path: Path
) -> None:
    with SessionOwnerStore(sessions).acquire("live-one", entrypoint="hosted-tui") as lease:
        register_interactive_runtime(lease, cwd=tmp_path / "project")

        (found,) = discover_interactive_runtimes(sessions)

        assert found.session_id == "live-one"
        assert found.cwd == str(tmp_path / "project")
        assert found.pid == lease.info.pid
        assert found.session_dir == SessionOwnerStore(sessions).session_path("live-one")


def test_a_runtime_that_exited_is_dropped_and_its_marker_removed(
    sessions: Path, tmp_path: Path
) -> None:
    owners = SessionOwnerStore(sessions)
    lease = owners.acquire("gone", entrypoint="hosted-tui")
    register_interactive_runtime(lease, cwd=tmp_path)
    marker = interactive_registry_dir(sessions) / "gone.json"
    assert marker.is_file()

    lease.release()

    assert discover_interactive_runtimes(sessions) == []
    assert not marker.exists(), "a stale marker is deleted the first time it is read"


def test_a_session_that_is_not_hosted_is_never_listed(sessions: Path, tmp_path: Path) -> None:
    """A terminal-owned session already shows in the saved list."""
    with SessionOwnerStore(sessions).acquire("terminal", entrypoint="tui") as lease:
        register_interactive_runtime(lease, cwd=tmp_path)

        assert discover_interactive_runtimes(sessions) == []


def test_a_marker_naming_another_owner_is_ignored(sessions: Path, tmp_path: Path) -> None:
    with SessionOwnerStore(sessions).acquire("mine", entrypoint="hosted-tui") as lease:
        register_interactive_runtime(lease, cwd=tmp_path)
        marker = interactive_registry_dir(sessions) / "mine.json"
        forged = json.loads(marker.read_text(encoding="utf-8"))
        forged["owner_id"] = "someone-else"
        marker.write_text(json.dumps(forged), encoding="utf-8")

        assert discover_interactive_runtimes(sessions) == []
        assert not marker.exists()


@pytest.mark.parametrize("content", ["not json", "[]", '{"session_id": 7}', ""])
def test_a_damaged_marker_is_removed_without_an_error(sessions: Path, content: str) -> None:
    directory = interactive_registry_dir(sessions)
    directory.mkdir(parents=True)
    marker = directory / "damaged.json"
    marker.write_text(content, encoding="utf-8")

    assert discover_interactive_runtimes(sessions) == []
    assert not marker.exists()


def test_an_unsafe_session_id_cannot_reach_outside_the_registry(sessions: Path) -> None:
    outside = interactive_registry_dir(sessions).parent / "escape.json"
    outside.write_text("keep", encoding="utf-8")

    unregister_interactive_runtime("../escape", sessions_dir=sessions)  # refused, not an error

    assert outside.read_text(encoding="utf-8") == "keep"


def test_unregister_is_idempotent(sessions: Path, tmp_path: Path) -> None:
    with SessionOwnerStore(sessions).acquire("twice", entrypoint="hosted-tui") as lease:
        register_interactive_runtime(lease, cwd=tmp_path)
        unregister_interactive_runtime("twice", sessions_dir=sessions)
        unregister_interactive_runtime("twice", sessions_dir=sessions)

        assert discover_interactive_runtimes(sessions) == []


def test_the_hub_gets_a_live_attached_agent_titled_by_the_first_request(
    sessions: Path, tmp_path: Path
) -> None:
    with SessionOwnerStore(sessions).acquire("explore", entrypoint="hosted-tui") as lease:
        journal = SessionOwnerStore(sessions).session_path("explore") / "conversation.jsonl"
        journal.write_text(
            json.dumps({"kind": "text", "payload": {"text": "ignored"}})
            + "\n"
            + json.dumps(
                {"kind": "user_message", "payload": {"text": "Explore the repo\nwith five agents"}}
            )
            + "\n",
            encoding="utf-8",
        )
        register_interactive_runtime(lease, cwd=tmp_path)

        (agent,) = load_interactive()

        assert agent.session_id == "explore"
        assert agent.status is SessionStatus.ATTACHED
        assert agent.source == INTERACTIVE_SOURCE
        assert agent.title == "Explore the repo with five agents"
        assert agent.cwd == str(tmp_path)
        assert agent.artifact_dir == str(journal.parent)
        assert agent.worker_pid == lease.info.pid
        assert agent.last_active >= agent.started_at

    assert load_interactive() == []


def test_a_session_with_no_request_yet_still_has_a_title(sessions: Path, tmp_path: Path) -> None:
    with SessionOwnerStore(sessions).acquire("fresh", entrypoint="hosted-tui") as lease:
        register_interactive_runtime(lease, cwd=tmp_path)

        (agent,) = load_interactive()

        assert agent.title == "Interactive session"
