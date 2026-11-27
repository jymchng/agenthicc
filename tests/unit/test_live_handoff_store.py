"""Controller fencing for adoption of the same live session writer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agenthicc.background.model import BackgroundSession, LaunchSpec, LiveHandoff, SessionStatus
from agenthicc.background.store import BackgroundStore, InvalidSessionTransition
from agenthicc.background.supervisor import BackgroundSupervisor, QueueFullError
from agenthicc.runners.session_lease import SessionOwnerLease, SessionOwnerStore

pytestmark = pytest.mark.unit


def test_prepare_commit_and_attach_fence_the_same_writer(tmp_path: Path) -> None:
    root = tmp_path / "background"
    owners = SessionOwnerStore(tmp_path / "sessions")
    supervisor = BackgroundSupervisor(BackgroundStore(root), max_workers=1)
    with owners.acquire("session-1", entrypoint="hosted-tui") as lease:
        record = BackgroundSession.create(
            lease.session_id,
            title="Live task",
            cwd=str(tmp_path),
            workflow_name="goal_flow",
            intent="test the handoff",
            artifact_dir=str(lease.path.parent),
        )
        request = supervisor.prepare_live_handoff(
            lease,
            record,
            launch=LaunchSpec(),
            request_id="request-1",
            generation=0,
            accepted_sequence=9,
            now=100,
        )
        assert supervisor.store.get(lease.session_id).status is SessionStatus.HANDOFF_PREPARED
        assert (
            supervisor.prepare_live_handoff(
                lease,
                record,
                launch=LaunchSpec(),
                request_id="request-1",
                generation=0,
                accepted_sequence=10,
                now=101,
            )
            == request
        )
        with owners.acquire("session-2", entrypoint="hosted-tui") as other:
            other_record = BackgroundSession.create(
                other.session_id,
                title="Another task",
                cwd=str(tmp_path),
                workflow_name="",
                intent="another handoff",
            )
            with pytest.raises(QueueFullError, match="capacity"):
                supervisor.prepare_live_handoff(
                    other,
                    other_record,
                    launch=LaunchSpec(),
                    request_id="request-2",
                    generation=0,
                    accepted_sequence=0,
                    now=101,
                )

        # A replacement supervisor resolves the durable request and keeps the writer.
        supervisor = BackgroundSupervisor(BackgroundStore(root), max_workers=1)
        with pytest.raises(InvalidSessionTransition, match="stale"):
            supervisor.commit_live_handoff(lease, request, now=115)
        running = supervisor.commit_live_handoff(lease, request, now=101)
        assert running.status is SessionStatus.RUNNING
        assert running.worker_pid == os.getpid()
        assert running.attempt == 1
        assert running.live_handoff is not None
        assert running.live_handoff.generation == 1
        assert running.live_handoff.accepted_sequence == 9
        assert supervisor.commit_live_handoff(lease, request, now=116) == running
        assert owners.inspect(lease.session_id).owner == lease.info
        attempt = subprocess.run(
            [
                sys.executable,
                "-c",
                "from agenthicc.runners.session_lease import SessionOwnerLease, SessionOwnerStore;"
                "from pathlib import Path;"
                "import sys;"
                "SessionOwnerStore(Path(sys.argv[1])).acquire(sys.argv[2])",
                str(owners.root),
                lease.session_id,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert "SessionAlreadyActiveError" in attempt.stderr
        with pytest.raises(InvalidSessionTransition, match="generation"):
            supervisor.attach_live_runtime(lease, generation=0)
        attached = supervisor.attach_live_runtime(lease, generation=1)
        assert attached.status is SessionStatus.ATTACHED
        assert attached.worker_pid == running.worker_pid
        assert attached.attempt == running.attempt
        assert attached.live_handoff is not None
        assert attached.live_handoff.generation == 2
        assert supervisor.store.active_sessions() == []
        assert owners.inspect(lease.session_id).owner == lease.info
        with pytest.raises(InvalidSessionTransition, match="stale"):
            supervisor.store.heartbeat(
                lease.session_id, lease_token=running.lease_token, attempt=running.attempt
            )
        with pytest.raises(InvalidSessionTransition, match="generation"):
            supervisor.store.transition(
                lease.session_id,
                SessionStatus.COMPLETED,
                expected_live_handoff=running.live_handoff,
            )
        with pytest.raises(InvalidSessionTransition, match="stale"):
            supervisor.commit_live_handoff(lease, request, now=104)


def test_capacity_race_and_failed_prepare_release_only_the_matching_reservation(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root = tmp_path / "background"
    owners = SessionOwnerStore(tmp_path / "sessions")
    barrier = Barrier(2)

    def prepare(
        index: int, lease: SessionOwnerLease
    ) -> tuple[SessionOwnerLease, LiveHandoff] | None:
        supervisor = BackgroundSupervisor(BackgroundStore(root), max_workers=1)
        record = BackgroundSession.create(
            lease.session_id,
            title="Racing handoff",
            cwd=str(tmp_path),
            workflow_name="",
            intent="exercise reservation contention",
            artifact_dir=str(lease.path.parent),
        )
        barrier.wait(timeout=5)
        try:
            request = supervisor.prepare_live_handoff(
                lease,
                record,
                launch=LaunchSpec(),
                request_id=f"race-{index}",
                generation=0,
                accepted_sequence=index,
                now=100,
            )
        except QueueFullError:
            return None
        return lease, request

    with (
        owners.acquire("session-1", entrypoint="hosted-tui") as first,
        owners.acquire("session-2", entrypoint="hosted-tui") as second,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        outcomes = list(executor.map(prepare, (1, 2), (first, second)))
        ((lease, request),) = [item for item in outcomes if item is not None]
        supervisor = BackgroundSupervisor(BackgroundStore(root), max_workers=1)
        (record,) = supervisor.store.active_sessions()
        assert record.status is SessionStatus.HANDOFF_PREPARED and record.attempt == 0
        assert supervisor.reconcile_live_handoff(lease, request_id="wrong-request") is None
        assert supervisor.store.active_sessions() == [record]
        assert supervisor.reconcile_live_handoff(lease, request_id=request.request_id) is None
        assert supervisor.store.active_sessions() == []
        assert owners.inspect(lease.session_id).owner == lease.info
        with pytest.raises(InvalidSessionTransition, match="stale"):
            supervisor.commit_live_handoff(lease, request, now=101)
