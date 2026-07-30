"""Deterministic coverage for terminal redaction, persistence, and rejection paths."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from agenthicc.background import terminals
from agenthicc.background.terminals import (
    TerminalManager,
    TerminalRecord,
    TerminalState,
    TerminalStore,
    get_current_terminal_manager,
    get_current_terminal_wait_policy,
    reset_current_terminal_manager,
    reset_current_terminal_wait_policy,
    set_current_terminal_manager,
    set_current_terminal_wait_policy,
)

pytestmark = pytest.mark.unit


def _record(tmp_path: Path, **overrides: object) -> TerminalRecord:
    values: dict[str, object] = {
        "terminal_id": "term-1",
        "session_id": "session-1",
        "project_root": str(tmp_path),
        "cwd": str(tmp_path),
        "kind": "exec",
        "command": "echo token=secret",
        "label": "label",
        "state": TerminalState.EXITED,
        "created_at": 1.0,
        "finished_at": 2.0,
        "returncode": 0,
    }
    values.update(overrides)
    return TerminalRecord(**values)  # type: ignore[arg-type]


def test_terminal_redaction_bounds_and_state_properties() -> None:
    assert terminals._is_loopback("localhost") is True
    assert terminals._is_loopback("127.0.0.1") is True
    assert terminals._is_loopback("::1") is True
    assert terminals._is_loopback("example.com") is False
    assert terminals.redact_text("--token secret password=hush") == (
        "--token=<redacted> password=<redacted>"
    )
    assert terminals.redact_text("x" * 10, limit=4) == "xxx…"
    assert terminals._redact_readiness({"url": "http://localhost", "port": 80}) == {
        "url": "http://localhost",
        "port": 80,
    }
    assert terminals._redact_readiness(None) is None
    assert terminals._redact_output("token=secret; password=hush") == (
        "token=<redacted>; password=<redacted>"
    )
    assert terminals._bounded_append("old", "", 3) == ("old", False)
    bounded, truncated = terminals._bounded_append("old", "x" * 30, 20)
    assert truncated is True
    assert "output truncated" in bounded

    record = _record(Path("."), state=TerminalState.RUNNING, started_at=1.0)
    assert record.active is True
    assert record.elapsed_s >= 0
    public = record.to_dict()
    assert public["state"] == "running"
    result = record.result()
    assert result["ok"] is False
    assert result["deadline"] is None


def test_terminal_record_mapping_is_tolerant_and_redacts() -> None:
    record = TerminalRecord.from_mapping(
        {
            "terminal_id": "term-2",
            "session_id": "s",
            "state": "not-a-state",
            "command": "--api-key=secret",
            "label": "password=hush",
            "pid": True,
            "pgid": 4,
            "returncode": False,
            "created_at": "invalid",
            "output_bytes": 2.7,
            "readiness": {"marker": "token=secret"},
            "ready": True,
            "deadline_owner": "command",
            "termination_reason": "done",
            "spawn_failure": "spawn",
        }
    )
    assert record.state is TerminalState.ORPHANED
    assert "secret" not in record.command
    assert record.pid is None
    assert record.pgid == 4
    assert record.returncode is None
    assert record.output_bytes == 2
    assert record.ready is True
    assert record.readiness == {"marker": "token=secret"}


def test_terminal_store_folds_invalid_events_and_prunes_old_records(tmp_path: Path) -> None:
    store = TerminalStore(tmp_path / "store")
    store.events_path.parent.mkdir(parents=True)
    store.events_path.write_text(
        "not-json\n"
        + json.dumps({"event": "ignored"})
        + "\n"
        + json.dumps({"record": {"terminal_id": ""}})
        + "\n",
        encoding="utf-8",
    )
    assert store.records() == []
    assert store.prune(older_than_s=0) == 0
    old = _record(tmp_path, terminal_id="old", created_at=1.0)
    active = _record(tmp_path, terminal_id="active", created_at=1.0, state=TerminalState.RUNNING)
    fresh = _record(tmp_path, terminal_id="fresh", created_at=time.time() + 10_000.0)
    store.upsert(old)
    store.upsert(active)
    store.upsert(fresh)
    removed = store.prune(older_than_s=100)
    assert removed == 1
    assert {item.terminal_id for item in store.records()} == {"active", "fresh"}
    assert store.get("fresh") is not None


def test_terminal_context_bindings_restore_previous_values(tmp_path: Path) -> None:
    manager = TerminalManager(session_id="session", cwd=tmp_path, store_root=tmp_path / "store")
    manager_token = set_current_terminal_manager(manager)
    policy_token = set_current_terminal_wait_policy("background")
    assert get_current_terminal_manager() is manager
    assert get_current_terminal_wait_policy() == "background"
    reset_current_terminal_wait_policy(policy_token)
    reset_current_terminal_manager(manager_token)
    assert get_current_terminal_manager() is None
    assert get_current_terminal_wait_policy() == "foreground"
    asyncio.run(manager.close())


@pytest.mark.asyncio
async def test_terminal_manager_rejects_invalid_starts_and_service_duplicates(
    tmp_path: Path,
) -> None:
    manager = TerminalManager(session_id="session", cwd=tmp_path, store_root=tmp_path / "store")
    cases = [
        (await manager.start(argv=["echo"], timeout=-1), "timeout"),
        (await manager.start(command="echo hi", argv=["echo", "hi"]), "provide command or argv"),
        (await manager.start(), "provide command or argv"),
        (await manager.start(argv=["echo"], lifecycle="invalid"), "lifecycle"),
        (
            await manager.start(argv=["echo"], env={"BAD": 1}),
            "environment",
        ),
        (await manager.start(argv=["echo"], cwd=tmp_path / "missing"), "cwd"),
    ]
    for result, message in cases:
        assert result["ok"] is False
        assert message in str(result.get("error", ""))

    duplicate = _record(
        tmp_path,
        terminal_id="service-1",
        state=TerminalState.RUNNING,
        lifecycle="service",
        command="echo service",
        command_digest=__import__("hashlib").sha256(b"echo service").hexdigest(),
    )
    manager._records[duplicate.terminal_id] = duplicate
    result = await manager.start(command="echo service", lifecycle="service")
    assert result["state"] == TerminalState.REJECTED.value
    assert result["terminal_id"] == "service-1"
    await manager.close()


@pytest.mark.asyncio
async def test_terminal_readiness_validation_and_marker_probe(tmp_path: Path) -> None:
    manager = TerminalManager(session_id="session", cwd=tmp_path, store_root=tmp_path / "store")
    record = _record(tmp_path, state=TerminalState.RUNNING)
    manager._records[record.terminal_id] = record
    assert (await manager.wait_readiness("unknown"))["state"] == "unknown"
    assert (await manager.wait_readiness(record.terminal_id, {}))["ok"] is False
    invalid_timeout = await manager.wait_readiness(
        record.terminal_id, {"marker": "x", "timeout": -1}
    )
    assert invalid_timeout["state"] == TerminalState.REJECTED.value
    invalid_shape = await manager.wait_readiness(
        record.terminal_id, {"marker": "x", "url": "http://localhost"}
    )
    assert invalid_shape["state"] == TerminalState.REJECTED.value
    record.stdout = "service ready"
    ready = await manager.wait_readiness(record.terminal_id, {"marker": "ready", "timeout": 1})
    assert ready["ready"] is True
    assert "output marker matched" in str(ready["readiness_evidence"])
    await manager.close()


@pytest.mark.asyncio
async def test_terminal_probe_rejects_external_tcp_and_url_without_network(tmp_path: Path) -> None:
    manager = TerminalManager(session_id="session", cwd=tmp_path, store_root=tmp_path / "store")
    record = _record(tmp_path, state=TerminalState.RUNNING)
    assert (
        await manager._probe_readiness(record, {"tcp": {"host": "example.com", "port": 80}}) is None
    )
    assert (
        await manager._probe_readiness(record, {"tcp": {"host": "127.0.0.1", "port": "80"}}) is None
    )
    assert await manager._probe_readiness(record, {"url": "https://example.com/"}) is None
    await manager.close()


def test_stop_persisted_terminal_records_uses_owned_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TerminalStore(tmp_path / "store")
    store.upsert(_record(tmp_path, state=TerminalState.RUNNING, pid=123, pgid=456))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(terminals.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(terminals, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(terminals.time, "sleep", lambda _seconds: None)
    stopped = terminals.stop_persisted_session_terminals(
        "session-1", store_root=tmp_path / "store", grace_s=0
    )
    assert stopped == 1
    assert killed
    assert store.get("term-1").state is TerminalState.STOPPED  # type: ignore[union-attr]
