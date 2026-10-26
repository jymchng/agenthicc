"""Every scriptable ``agenthicc agents`` subcommand through the CLI entry point (AGX-05 §5.5)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from agenthicc.background import (
    BackgroundInteractionInbox,
    BackgroundSession,
    BackgroundStore,
    BackgroundSupervisor,
    SessionStatus,
)

pytestmark = pytest.mark.e2e


def _cli(store: BackgroundStore, capsys: pytest.CaptureFixture[str]) -> Callable[..., str]:
    """Run ``agenthicc --set background.store_path=STORE ARGS…`` in-process."""
    from agenthicc.__main__ import main

    def run(*args: str, code: int = 0) -> str:
        argv = ["agenthicc", "--set", f"background.store_path={store.root}", *args]
        with patch("sys.argv", argv):
            if code:
                with pytest.raises(SystemExit) as raised:
                    main()
                assert raised.value.code == code
            else:
                main()
        return capsys.readouterr().out

    return run


def _agent(
    store: BackgroundStore, session_id: str, status: SessionStatus, **changes: object
) -> BackgroundSession:
    record = BackgroundSession.create(
        session_id,
        title=session_id,
        cwd=str(store.root.parent),
        workflow_name="",
        intent=f"work for {session_id}",
    ).evolve(status=status, **changes)
    store.create(record)
    return record


def _waiting_item(
    store: BackgroundStore, session_id: str, kind: str, target: dict[str, object]
) -> str:
    """Raise an approval or question the way a worker does; return its item ID."""
    inbox = BackgroundInteractionInbox(store)
    current = store.update(
        session_id,
        status=SessionStatus.RUNNING,
        worker_pid=os.getpid(),
        lease_token="lease",
        attempt=1,
    )
    item = inbox.register(
        session_id=session_id,
        attempt=current.attempt,
        request_id=f"{session_id}-request",
        kind=kind,
        target=target,
        expires_at=None,
    )
    if kind == "approval":
        store.transition(
            session_id,
            SessionStatus.WAITING_APPROVAL,
            expected_status=SessionStatus.RUNNING,
            approval_item_id=item.item_id,
        )
    else:
        store.transition(
            session_id,
            SessionStatus.WAITING_INPUT,
            expected_status=SessionStatus.RUNNING,
            input_item_id=item.item_id,
        )
    return inbox.activate(item.item_id).item_id


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackgroundStore:
    monkeypatch.chdir(tmp_path)
    # No subcommand may start or signal a process: dispatching a queued agent
    # and stopping a worker are the supervisor's, recorded instead.
    monkeypatch.setattr(BackgroundSupervisor, "dispatch_pending", lambda self: ())
    return BackgroundStore(tmp_path / "background")


def test_lifecycle_subcommands_change_the_record(
    store: BackgroundStore, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _cli(store, capsys)
    _agent(store, "queued", SessionStatus.QUEUED)
    _agent(store, "done", SessionStatus.COMPLETED)
    _agent(store, "broken", SessionStatus.FAILED, error="boom")

    listed = json.loads(run("agents", "list", "--json"))
    assert {row["session_id"] for row in listed} == {"queued", "done", "broken"}
    assert json.loads(run("agents", "status", "done", "--json"))["status"] == "completed"

    assert "priority: queued → urgent" in run("agents", "priority", "queued", "urgent")
    assert store.get("queued").priority.value == "urgent"
    assert "rename: done → Shipped feature" in run("agents", "rename", "done", "Shipped feature")
    assert "labels: done → release, ui" in run(
        "agents", "labels", "done", "--labels", "release, ui"
    )
    assert store.get("done").labels == ("release", "ui")

    assert "cancel: queued → cancelled" in run("agents", "cancel", "queued")
    assert "archive: done → archived" in run("agents", "archive", "done")
    assert "delete: broken → deleted" in run("agents", "delete", "broken")
    assert "restore: broken →" in run("agents", "restore", "broken")
    assert store.get("broken").status is not SessionStatus.DELETED
    assert run("agents", "purge").startswith("purged: ")
    assert "Unable to cancel missing" in run("agents", "cancel", "missing", code=1)


def test_resume_and_retry_requeue_like_the_hub(
    store: BackgroundStore, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _cli(store, capsys)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        BackgroundSupervisor,
        "resume",
        lambda self, session_id: calls.append(("resume", session_id)) or store.get(session_id),
    )
    monkeypatch.setattr(
        BackgroundSupervisor,
        "retry",
        lambda self, session_id: calls.append(("retry", session_id)) or store.get(session_id),
    )
    _agent(store, "lost", SessionStatus.ORPHANED)
    _agent(store, "failed", SessionStatus.FAILED)
    assert "resume: lost" in run("agents", "resume", "lost")
    assert "retry: failed" in run("agents", "retry", "failed")
    assert calls == [("resume", "lost"), ("retry", "failed")]


def test_approval_and_input_subcommands_resolve_the_waiting_request(
    store: BackgroundStore, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _cli(store, capsys)
    for session_id in ("approve-me", "reject-me", "ask-me"):
        _agent(store, session_id, SessionStatus.RUNNING)
    _waiting_item(store, "approve-me", "approval", {"tool_name": "bash"})
    _waiting_item(store, "reject-me", "approval", {"tool_name": "bash"})
    _waiting_item(
        store,
        "ask-me",
        "input",
        {"tool_name": "ask_user", "questions": [{"id": "q", "text": "Which runtime?"}]},
    )
    assert "approve: approve-me" in run("agents", "approve", "approve-me")
    assert "reject: reject-me" in run("agents", "reject", "reject-me")
    assert "input: ask-me" in run("agents", "input", "ask-me", "Python 3.13")
    inbox = BackgroundInteractionInbox(store)
    assert {item.status for item in inbox.list_items(status="accepted")} == {"accepted"}


def test_inbox_subcommands_resolve_exact_items(
    store: BackgroundStore, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _cli(store, capsys)
    for session_id in ("one", "two", "three"):
        _agent(store, session_id, SessionStatus.RUNNING)
    approve = _waiting_item(store, "one", "approval", {"tool_name": "bash"})
    reject = _waiting_item(store, "two", "approval", {"tool_name": "write_file"})
    expire = _waiting_item(store, "three", "approval", {"tool_name": "bash"})
    listed = {row["item_id"]: row for row in json.loads(run("agents", "inbox", "list", "--json"))}
    assert set(listed) == {approve, reject, expire}

    def version(item_id: str) -> str:
        return str(listed[item_id]["version"])

    assert "accepted" in run("agents", "inbox", "approve", approve, version(approve), "key-1")
    assert "accepted" in run("agents", "inbox", "reject", reject, version(reject), "key-2")
    assert "expire:" in run("agents", "inbox", "expire", expire, version(expire), "key-3")
    inbox = BackgroundInteractionInbox(store)
    assert inbox.get(approve).status == "accepted"
    assert inbox.get(reject).status == "accepted"
    assert inbox.get(expire).status == "expired"
    # Accepted answers stay listed until their workers take them; none is pending.
    remaining = json.loads(run("agents", "inbox", "list", "--json"))
    assert {row["item_id"]: row["status"] for row in remaining} == {
        approve: "accepted",
        reject: "accepted",
    }


def test_notifications_and_environment_actions_are_reachable(
    store: BackgroundStore, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _cli(store, capsys)
    _agent(store, "plain", SessionStatus.COMPLETED)
    assert json.loads(run("agents", "notifications", "--json")) == []
    assert "No background notification deliveries." in run("agents", "notifications")
    # ``agents actions`` is the worktree command's alias: a session that is not
    # a worktree worker declares no actions.
    assert "No named actions are declared" in run("agents", "actions", "plain")
    assert json.loads(run("agents", "actions", "plain", "--json")) == []
