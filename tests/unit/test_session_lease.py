"""Unit coverage for PRD-171 session ownership and process liveness."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from agenthicc.runners.session_lease import (
    SessionAlreadyActiveError,
    SessionIndexError,
    SessionOpenCoordinator,
    SessionOwnerStore,
    SessionStorageError,
)

pytestmark = pytest.mark.unit


def test_live_owner_is_exclusive_and_release_is_owner_safe(tmp_path: Path) -> None:
    store = SessionOwnerStore(tmp_path)
    first = store.acquire("session-1", entrypoint="tui")
    try:
        payload = json.loads(first.path.read_text(encoding="utf-8"))
        assert payload["session_id"] == "session-1"
        assert payload["owner_id"] == first.owner_id
        assert payload["pid"] == os.getpid()
        assert payload["host"] == socket.gethostname()
        assert list(first.path.parent.glob(".owner-*.tmp")) == []

        # Simulate a distinct process owner while keeping this test in one
        # pytest process; real distinct processes cannot share the in-process
        # re-entry registry.
        payload["owner_id"] = "other-process-owner"
        first.path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SessionAlreadyActiveError) as caught:
            SessionOwnerStore(tmp_path).acquire("session-1", entrypoint="tui")
        assert caught.value.code == "session_already_active"
        assert caught.value.owner is not None
        assert caught.value.owner.owner_id == "other-process-owner"

        # A different owner token cannot release the live record.
        assert SessionOwnerStore(tmp_path).owner_path("session-1").exists()
    finally:
        first.release()
        assert first.path.exists()
        first.path.unlink(missing_ok=True)

    second = SessionOwnerStore(tmp_path).acquire("session-1", entrypoint="headless")
    second.release()
    assert not second.path.exists()


def test_same_process_reentry_is_reference_counted(tmp_path: Path) -> None:
    first = SessionOwnerStore(tmp_path).acquire("session-1")
    second = SessionOwnerStore(tmp_path).acquire("session-1")
    try:
        assert first.owner_id == second.owner_id
        first.release()
        assert second.path.exists()
    finally:
        second.release()
    assert not second.path.exists()


def test_dead_owner_is_reclaimed_but_unknown_owner_fails_closed(tmp_path: Path) -> None:
    store = SessionOwnerStore(tmp_path)
    path = store.owner_path("session-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "session-1",
                "owner_id": "dead-owner",
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
                "acquired_at": 1.0,
                "entrypoint": "tui",
            }
        ),
        encoding="utf-8",
    )

    reclaimed = store.acquire("session-1")
    reclaimed.release()

    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(SessionAlreadyActiveError) as caught:
        store.acquire("session-1")
    assert caught.value.reason == "owner_unverifiable"
    assert path.read_text(encoding="utf-8") == "not-json"


def test_cross_host_and_identity_failure_fail_closed(tmp_path: Path) -> None:
    store = SessionOwnerStore(tmp_path)
    path = store.owner_path("session-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {
        "schema_version": 1,
        "session_id": "session-1",
        "owner_id": "remote-owner",
        "pid": os.getpid(),
        "host": "another-host",
        "process_start_token": "old-start",
        "acquired_at": 1.0,
        "entrypoint": "tui",
    }
    path.write_text(json.dumps(base), encoding="utf-8")
    assert store.inspect("session-1").state == "active"
    with pytest.raises(SessionAlreadyActiveError) as remote_conflict:
        store.acquire("session-1")
    assert remote_conflict.value.reason == "live_owner"
    assert path.read_text(encoding="utf-8") == json.dumps(base)

    local = {**base, "host": socket.gethostname(), "owner_id": "unverifiable-owner"}
    path.write_text(json.dumps(local), encoding="utf-8")

    def unavailable_identity(_pid: int) -> tuple[str, str] | None:
        raise OSError("process identity unavailable")

    fail_closed = SessionOwnerStore(tmp_path, identity_resolver=unavailable_identity)
    with pytest.raises(SessionAlreadyActiveError) as unknown_conflict:
        fail_closed.acquire("session-1")
    assert unknown_conflict.value.reason == "live_owner"
    assert path.read_text(encoding="utf-8") == json.dumps(local)


def test_pid_reuse_and_zombie_are_reclaimable(tmp_path: Path) -> None:
    def identity(_pid: int) -> tuple[str, str]:
        return ("R", "new-start")

    store = SessionOwnerStore(tmp_path, identity_resolver=identity)
    path = store.owner_path("session-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "session-1",
                "owner_id": "old-owner",
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "process_start_token": "old-start",
                "acquired_at": 1.0,
                "entrypoint": "tui",
            }
        ),
        encoding="utf-8",
    )
    reclaimed = store.acquire("session-1")
    reclaimed.release()

    zombie_store = SessionOwnerStore(tmp_path, identity_resolver=lambda _pid: ("Z", "same"))
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "session-1",
                "owner_id": "zombie-owner",
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "process_start_token": "same",
                "acquired_at": 1.0,
                "entrypoint": "tui",
            }
        ),
        encoding="utf-8",
    )
    zombie = zombie_store.acquire("session-1")
    zombie.release()


def test_inspection_classifies_available_active_recoverable_and_unknown(tmp_path: Path) -> None:
    store = SessionOwnerStore(tmp_path)
    assert store.inspect("available").state == "available"
    lease = store.acquire("active")
    try:
        active = store.inspect("active")
        assert active.state == "active"
        assert active.owner is not None
    finally:
        lease.release()

    dead_path = store.owner_path("recoverable")
    dead_path.parent.mkdir(parents=True, exist_ok=True)
    dead_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "recoverable",
                "owner_id": "dead",
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
                "acquired_at": 1.0,
                "entrypoint": "tui",
            }
        ),
        encoding="utf-8",
    )
    assert store.inspect("recoverable").state == "recoverable"

    unknown_path = store.owner_path("unknown")
    unknown_path.parent.mkdir(parents=True, exist_ok=True)
    unknown_path.write_text("{}", encoding="utf-8")
    assert store.inspect("unknown").state == "unknown"


def test_latest_selection_is_deterministic_and_claimed(tmp_path: Path) -> None:
    index = tmp_path / "index.json"
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    index.write_text(
        json.dumps(
            {
                "old": {"cwd": str(tmp_path), "last_active": 10.0},
                "new": {"cwd": str(tmp_path), "last_active": 20.0},
            }
        ),
        encoding="utf-8",
    )
    selected = SessionOpenCoordinator(tmp_path).select_latest_for_cwd(tmp_path)
    assert selected is not None
    session_id, lease = selected
    assert session_id == "new"
    assert lease.path.exists()
    lease.release()


def test_latest_selection_rejects_corrupt_index(tmp_path: Path) -> None:
    (tmp_path / "index.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(SessionIndexError):
        SessionOpenCoordinator(tmp_path).select_latest_for_cwd(tmp_path)


def test_explicit_resume_requires_an_existing_session(tmp_path: Path) -> None:
    with pytest.raises(SessionStorageError, match="was not found"):
        SessionOpenCoordinator(tmp_path).acquire_existing("missing")


def test_conflict_diagnostic_is_bounded_and_secret_free(tmp_path: Path) -> None:
    lease = SessionOwnerStore(tmp_path).acquire("session-1", entrypoint="headless")
    try:
        payload = json.loads(lease.path.read_text(encoding="utf-8"))
        payload["owner_id"] = "other-owner"
        lease.path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SessionAlreadyActiveError) as caught:
            SessionOwnerStore(tmp_path).acquire("session-1")
    finally:
        lease.release()
        lease.path.unlink(missing_ok=True)
    error = caught.value
    rendered = str(error) + json.dumps(error.to_dict())
    assert error.code == "session_already_active"
    assert "prompt" not in rendered.lower()
    assert "api_key" not in rendered.lower()
    assert "headless" in rendered


def test_oversized_owner_record_is_unknown_and_not_reclaimed(tmp_path: Path) -> None:
    path = SessionOwnerStore(tmp_path).owner_path("session-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{" + "x" * (64 * 1024) + "}", encoding="utf-8")

    inspection = SessionOwnerStore(tmp_path).inspect("session-1")
    assert inspection.state == "unknown"
    with pytest.raises(SessionAlreadyActiveError) as caught:
        SessionOwnerStore(tmp_path).acquire("session-1")
    assert caught.value.reason == "owner_unverifiable"


def test_non_finite_acquisition_time_is_unverifiable(tmp_path: Path) -> None:
    path = SessionOwnerStore(tmp_path).owner_path("session-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "session-1",
                "owner_id": "bad-time",
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": float("nan"),
                "entrypoint": "tui",
            }
        ),
        encoding="utf-8",
    )
    assert SessionOwnerStore(tmp_path).inspect("session-1").state == "unknown"
