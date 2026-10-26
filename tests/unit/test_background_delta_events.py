"""Update events carry only the fields that changed (AGX-04)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from agenthicc.background import BackgroundSession, BackgroundStore, SessionStatus
from agenthicc.background.model import ModeApplicationStatus, QueuePriority

pytestmark = pytest.mark.unit


def _session(tmp_path: Path, session_id: str) -> BackgroundSession:
    return BackgroundSession.create(
        session_id,
        title=f"title {session_id}",
        cwd=str(tmp_path),
        workflow_name="demo",
        intent="exercise delta events " * 8,
        now=1.0,
    )


def _events(store: BackgroundStore) -> list[dict[str, object]]:
    return [json.loads(line) for line in store.events_path.read_text().splitlines()]


def test_a_single_field_update_writes_a_small_event(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "one"))
    created = store.events_path.read_bytes().splitlines()[-1]

    store.update("one", pinned=True)

    event = _events(store)[-1]
    changes = event["payload"]["changes"]  # type: ignore[index]
    assert set(changes) == {"pinned", "last_active"}
    line = store.events_path.read_bytes().splitlines()[-1]
    assert len(line) <= len(created) // 4


def test_status_changes_keep_their_derived_fields_and_markers(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "one"))

    updated = store.transition("one", SessionStatus.STARTING)

    payload = _events(store)[-1]["payload"]
    assert payload["previous_status"] == "queued"  # type: ignore[index]
    assert payload["current_status"] == "starting"  # type: ignore[index]
    changes = payload["changes"]  # type: ignore[index]
    assert {"status", "state_changed_at", "last_active"} <= set(changes)
    assert updated == BackgroundStore(tmp_path / "background").get("one")


def _random_change(rng: random.Random, sequence: int) -> dict[str, object]:
    choice = rng.randrange(8)
    if choice == 7:
        # A mode request without an attested status stays pending.
        return {"mode_name": rng.choice(["", "plan", "yolo"])}
    if choice == 0:
        return {"latest_activity": f"step {sequence}"}
    if choice == 1:
        return {"pinned": rng.random() < 0.5}
    if choice == 2:
        return {"labels": tuple(rng.sample(["red", "green", "blue"], rng.randint(0, 3)))}
    if choice == 3:
        return {"title": f"renamed {sequence}"}
    if choice == 4:
        return {"current_phase": rng.choice(["", "plan", "build"]), "error": None}
    if choice == 5:
        return {"heartbeat_stale": rng.random() < 0.5, "last_active": float(sequence)}
    return {"priority": rng.choice(list(QueuePriority))}


def test_replaying_delta_events_reproduces_every_update(tmp_path: Path) -> None:
    rng = random.Random(4)
    for case in range(60):
        root = tmp_path / f"case-{case}"
        store = BackgroundStore(root)
        ids = [f"s-{index}" for index in range(rng.randint(1, 4))]
        attempts: dict[str, int] = {}
        for session_id in ids:
            store.create(_session(tmp_path, session_id))
            if rng.random() < 0.5:
                claimed = store.claim(session_id, pid=42, lease_token=f"lease-{session_id}")
                attempts[session_id] = claimed.attempt
        latest: dict[str, BackgroundSession] = {}
        for sequence in range(rng.randint(1, 30)):
            session_id = rng.choice(ids)
            attempt = attempts.get(session_id)
            if attempt is not None and rng.random() < 0.3:
                # Attested by the running attempt; often only the name changes.
                latest[session_id] = store.record_mode_application(
                    session_id,
                    requested_mode_name=rng.choice([None, "plan", "yolo"]),
                    effective_mode_name=rng.choice(["plan", "yolo"]),
                    attempt=attempt,
                    lease_token=f"lease-{session_id}",
                )
                continue
            latest[session_id] = store.update(session_id, **_random_change(rng, sequence))
        store.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{root / 'index.sqlite3'}{suffix}").unlink(missing_ok=True)

        replayed = BackgroundStore(root)
        for session_id, session in latest.items():
            assert replayed.get(session_id) == session, (case, session_id)


def test_a_mode_name_change_records_the_status_it_was_applied_with(tmp_path: Path) -> None:
    store = BackgroundStore(tmp_path / "background")
    store.create(_session(tmp_path, "one"))
    running = store.claim("one", pid=42, lease_token="lease")
    store.record_mode_application(
        "one",
        requested_mode_name="plan",
        effective_mode_name="plan",
        attempt=running.attempt,
        lease_token="lease",
    )

    # Only the name changes; replay must not read it as a pending request.
    updated = store.record_mode_application(
        "one",
        requested_mode_name="plan",
        effective_mode_name="yolo",
        attempt=running.attempt,
        lease_token="lease",
    )

    changes = _events(store)[-1]["payload"]["changes"]  # type: ignore[index]
    assert set(changes) == {"mode_name", "mode_application_status", "last_active"}
    assert changes["mode_application_status"] == "applied"  # type: ignore[index]
    store.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{tmp_path / 'background' / 'index.sqlite3'}{suffix}").unlink(missing_ok=True)
    assert BackgroundStore(tmp_path / "background").get("one") == updated
    assert updated.mode_name == "yolo"
    assert updated.mode_application_status is ModeApplicationStatus.APPLIED


def test_logs_mixing_full_record_and_delta_events_replay_identically(tmp_path: Path) -> None:
    root = tmp_path / "background"
    store = BackgroundStore(root)
    store.create(_session(tmp_path, "mixed"))
    first = store.update("mixed", latest_activity="delta")
    # An event written before AGX-04 carries the whole record.
    full = first.evolve(title="full record", pinned=True)
    changes = full.to_dict()
    changes.pop("session_id")
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "seq": 3,
                    "event_type": "updated",
                    "timestamp": 3.0,
                    "payload": {"session_id": "mixed", "changes": changes},
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    last = store.update("mixed", labels=("after",))
    store.close()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{root / 'index.sqlite3'}{suffix}").unlink(missing_ok=True)

    replayed = BackgroundStore(root).get("mixed")

    assert replayed == last
    assert replayed.title == "full record" and replayed.pinned and replayed.labels == ("after",)
