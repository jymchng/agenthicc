"""A real terminal client detaches while the real goal workflow owns live workers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from lauren_ai._transport import Completion

from agenthicc.background import BackgroundStore, BackgroundSupervisor
from agenthicc.background.inbox import BackgroundInteractionInbox, InboxConflict
from agenthicc.background.input_inbox import BackgroundInputInbox
from agenthicc.background.model import SessionStatus
from agenthicc.runners.session_lease import SessionOwnerStore, discover_interactive_runtimes
from agenthicc.tui.workspace.agents_hub.detail import SUBAGENT_ROLE, read_transcript
from tests.cli_process import CLI_PROCESS_TIMEOUT_S, cli_test_timeout
from tests.e2e.self_extension.harness import ModelRequest, reply, tool_call
from tests.e2e.test_first_run_e2e import _Terminal, _repository
from tests.subagent_sessions import ScenarioTransport, spawn_call


def _wait_for_detach(terminal: _Terminal) -> str:
    """The detach acknowledgement as it was on screen the moment it appeared.

    ``/bg`` now ends in the agents hub, which repaints the whole screen, so the lines the
    client printed are read when they show rather than after the hub has drawn over them.
    """
    seen: list[str] = []

    def acknowledged(text: str) -> bool:
        if "running in the background" in text:
            seen.append(text)
            return True
        return False

    terminal.wait_for(acknowledged, "handoff confirmation")
    return seen[0]


def _leave_the_hub(terminal: _Terminal) -> None:
    """``/bg`` opens the agents hub with the new agent selected; Esc leaves it."""
    terminal.wait_for(lambda text: "Needs you" in text, "the agents hub after the handoff")
    terminal.send("\x1b")


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY test fixture"),
]


def scripted_transport(project: Path) -> ScenarioTransport:
    """Inject provider responses only, in the independently spawned runtime."""

    def record(lane: str, name: str) -> None:
        with (project / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"lane": lane, "name": name, "pid": os.getpid()}) + "\n")

    explorer_calls: dict[str, int] = {}

    async def worker(request: ModelRequest) -> Completion:
        first_message = str(request.messages[0]["content"])
        match = re.search(r"run marker ([0-4])", first_message)
        assert match is not None, first_message
        worker_id = match[1]
        if (project / "direct-explorers").exists():
            # A read-only Explorer: one real tool call, then a provider barrier.
            explorer_calls[worker_id] = explorer_calls.get(worker_id, 0) + 1
            if explorer_calls[worker_id] == 1:
                record(worker_id, "action")
                return tool_call("read_file", {"path": "AGENTS.md"}, use_id=f"read-{worker_id}")
            record(worker_id, "gate")
            async with asyncio.timeout(CLI_PROCESS_TIMEOUT_S * 2):
                while not (project / f"release-explorer-{worker_id}").exists():
                    await asyncio.sleep(0.02)
            record(worker_id, "released")
            return reply(f"Explorer {worker_id} read the guide.")
        if not request.contains(f"marker-written-{worker_id}"):
            record(worker_id, "action")
            return tool_call(
                "run_bash",
                {"command": f"printf marker-written-{worker_id} | tee -a worker-{worker_id}.txt"},
                use_id=f"action-{worker_id}",
            )
        record(worker_id, "gate")
        async with asyncio.timeout(CLI_PROCESS_TIMEOUT_S * 2):
            while not (project / "release-workers").exists():
                await asyncio.sleep(0.02)
        record(worker_id, "released")
        return reply(f"Worker {worker_id} finished its real command.")

    spawned = False
    awaiting_collect = ""

    async def wait_for_parent_release() -> None:
        async with asyncio.timeout(CLI_PROCESS_TIMEOUT_S * 2):
            while not (project / "release-parent").exists():
                await asyncio.sleep(0.02)

    async def main(request: ModelRequest, sequence: int) -> Completion:
        nonlocal spawned, awaiting_collect
        record("parent", str(sequence))
        if request.contains("message from the reattached operator"):
            record("parent", "reattached-input-observed")
        if (project / "ask-at-clarify").exists():
            if request.contains("operator-confirmed"):
                history = repr(request.messages)
                if "ordered message 1" not in history:
                    return tool_call(
                        "read_file", {"path": "AGENTS.md"}, use_id=f"fifo-boundary-{sequence}"
                    )
                assert history.count("ordered message 0") == history.count("ordered message 1") == 1
                assert history.index("ordered message 0") < history.index("ordered message 1")
                record("parent", "operator-confirmed")
                return reply("Received the original response and both ordered messages.")
            return tool_call(
                "ask_user",
                {
                    "questions": [
                        {
                            "id": "cutover",
                            "text": "Which answer crosses the cutover?",
                            "options": ["operator-confirmed"],
                        }
                    ]
                },
                use_id="operator-question",
            )
        if (project / "direct-explorers").exists():
            if not spawned:
                spawned = True
                return spawn_call(5, kind="explorer", task="run marker")
            last = str(request.messages[-1]["content"])
            notice = re.match(r"Background subagent (explorer #\d) of pool (\S+) ", last)
            if notice is not None:
                # One Explorer finished: the notice names it and nothing it wrote.
                record("parent", f"notified:{notice[1]}")
                assert "read the guide" not in last
                if notice[1] == "explorer #1":
                    awaiting_collect = "partial"
                    return tool_call(
                        "collect_subagent_results",
                        {"pool_id": notice[2]},
                        use_id="collect-partial",
                    )
                if "the pool is complete" in last:
                    awaiting_collect = "final"
                    return tool_call(
                        "collect_subagent_results",
                        {"pool_id": notice[2]},
                        use_id="collect-final",
                    )
                return reply(f"Noted {notice[1]}.")
            if awaiting_collect == "partial":
                awaiting_collect = ""
                # The pool is still running, yet the finished Explorer is readable.
                history = repr(request.messages)
                record(
                    "parent",
                    "partial-collect-ok"
                    if "Explorer 0 read the guide." in history
                    and "Explorer 1 read the guide." not in history
                    and "'status': 'running'" in history.replace('"', "'")
                    else "partial-collect-bad",
                )
                return reply("Read the first Explorer's report.")
            if awaiting_collect == "final" or request.contains("Explorer 0 read the guide."):
                # The whole pool: collected after the last notice, or the
                # aggregate of an explicit foreground spawn.
                awaiting_collect = ""
                history = repr(request.messages)
                if all(f"Explorer {index} read the guide." in history for index in range(5)):
                    record("parent", "explorers-collected")
                return reply("Collected all five explorer reports.")
            if (project / "hold-parent").exists() and not (project / "release-parent").exists():
                record("parent", "streaming-gate")
                await wait_for_parent_release()
            record("parent", "direct-yield")
            return reply("Five explorers are reading in the background.")
        if (project / "direct-pool").exists():
            if not spawned:
                spawned = True
                return spawn_call(1, kind="executor", task="run marker")
            record("parent", "direct-yield")
            return reply("The direct pool can finish independently.")
        if "complete_clarification" in request.tools and not request.contains("fixture clarified"):
            return tool_call(
                "complete_clarification", {"notes": "fixture clarified"}, use_id="clarify"
            )
        if "finalize_goals" in request.tools and not request.contains("Create five marker files"):
            return tool_call(
                "finalize_goals", {"goals": ["Create five marker files"]}, use_id="goals"
            )
        if "goal_implemented" in request.tools:
            if not spawned:
                spawned = True
                return spawn_call(5, kind="executor", task="run marker")
            if (
                (project / "gate-before-collect").exists()
                and request.contains("Its result is not included here")
                and not (project / "release-parent").exists()
            ):
                record("parent", "collect-gate")
                await wait_for_parent_release()
            if request.contains("the pool is complete"):
                journal = _journal_rows(
                    next(
                        (Path.home() / ".agenthicc" / "sessions").glob(
                            "*/conversation-journal.jsonl"
                        )
                    )
                )
                pool = next(
                    row["pool_id"] for row in journal if row.get("kind") == "subagent_pool_started"
                )
                if not request.contains("Worker 0 finished its real command."):
                    return tool_call(
                        "collect_subagent_results", {"pool_id": pool}, use_id="collect"
                    )
                return tool_call(
                    "goal_implemented",
                    {
                        "summary": "Workers wrote the files",
                        "files": [f"worker-{i}.txt" for i in range(5)],
                    },
                    use_id="implemented",
                )
            if (project / "explicit-foreground").exists() and request.contains(
                "Worker 0 finished its real command."
            ):
                return tool_call(
                    "goal_implemented",
                    {"summary": "Foreground workers finished", "files": []},
                    use_id="implemented",
                )
            if (project / "hold-parent").exists() and not (project / "release-parent").exists():
                record("parent", "streaming-gate")
                await wait_for_parent_release()
            record("parent", "parked")
            return reply("Waiting for live Executors.")
        if "verify_goal" in request.tools:
            if not request.contains("five markers confirmed"):
                record("parent", "verify-goal")
                return tool_call(
                    "verify_goal",
                    {"satisfied": True, "evidence": "five markers confirmed"},
                    use_id="verify",
                )
        if "complete_workflow" in request.tools and not request.contains(
            "original workflow finished"
        ):
            return tool_call(
                "complete_workflow",
                {
                    "summary": "original workflow finished",
                    "files": [f"worker-{i}.txt" for i in range(5)],
                },
                use_id="workflow-complete",
            )
        return reply("Phase response.")

    return ScenarioTransport(main, worker)


def _journal_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def install_cutover_fault(project: Path) -> None:
    """Gate the real durable operations in the runtime subprocess."""
    fault_path = project / "cutover-fault"
    if not fault_path.is_file():
        return
    fault = fault_path.read_text(encoding="utf-8")
    prepare = BackgroundSupervisor.prepare_live_handoff
    commit = BackgroundSupervisor.commit_live_handoff

    def reach(phase: str) -> None:
        if phase != fault:
            return
        (project / "cutover-blocked").touch()
        until = time.monotonic() + 20
        while not (project / "cutover-release").exists():
            if time.monotonic() >= until:
                raise TimeoutError("test did not release cutover")
            time.sleep(0.01)

    def prepare_at_gate(supervisor, *args, **kwargs):
        reach("before-prepare")
        result = prepare(supervisor, *args, **kwargs)
        reach("after-prepare")
        return result

    def commit_at_gate(supervisor, *args, **kwargs):
        result = commit(supervisor, *args, **kwargs)
        reach("after-commit")
        return result

    BackgroundSupervisor.prepare_live_handoff = prepare_at_gate
    BackgroundSupervisor.commit_live_handoff = commit_at_gate


def _scripted_project(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    project = _repository(tmp_path)
    (project / "agenthicc.toml").write_text(
        '[skills]\ninstall_default_skills = false\n[subagents]\nisolation = "shared"\n',
        encoding="utf-8",
    )
    scripts = tmp_path / "provider"
    scripts.mkdir()
    (scripts / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import lauren_ai._module\n"
        "from tests.e2e.test_live_subagent_handoff import install_cutover_fault, scripted_transport\n"
        "install_cutover_fault(Path.cwd())\n"
        "lauren_ai._module._build_transport = lambda config: scripted_transport(Path.cwd())\n"
        "if (Path.cwd() / 'inject-input-ack').is_file():\n"
        "    from agenthicc.background.input_inbox import BackgroundInputInbox\n"
        "    original = BackgroundInputInbox.acknowledge\n"
        "    def acknowledge(self, *args, **kwargs):\n"
        "        marker = Path.cwd() / 'ack-failed'\n"
        "        if not marker.exists():\n"
        "            marker.touch()\n"
        "            raise OSError('test input acknowledgement failure')\n"
        "        return original(self, *args, **kwargs)\n"
        "    BackgroundInputInbox.acknowledge = acknowledge\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[2]
    environment = {
        "PYTHONPATH": os.pathsep.join((str(scripts), str(repository), str(repository / "src"))),
        "PATH": os.environ["PATH"],
        "ANTHROPIC_API_KEY": "fixture-key",
    }
    for name in ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return home, project, environment


@pytest.mark.parametrize(
    "alias,next_controller",
    [
        ("/detach", "service"),
        ("/bg", "reattach"),
        ("/background", "service"),
        ("/detach", "recovery"),
        ("/detach", "cancel"),
        ("/detach", "worker-stop"),
        ("/detach", "streaming"),
        ("/bg", "streaming"),
        ("/background", "streaming"),
        ("/detach", "terminal"),
        ("/bg", "terminal"),
        ("/background", "terminal"),
        ("/detach", "foreground"),
    ],
)
@cli_test_timeout(5)
def test_goal_flow_detach_preserves_five_live_executor_attempts(
    tmp_path: Path, alias: str, next_controller: str
) -> None:
    home, project, environment = _scripted_project(tmp_path)
    if next_controller == "streaming":
        (project / "hold-parent").touch()
    elif next_controller == "terminal":
        (project / "gate-before-collect").touch()
    elif next_controller == "foreground":
        (project / "explicit-foreground").touch()
        with (project / "agenthicc.toml").open("a", encoding="utf-8") as configuration:
            configuration.write('spawn_mode = "foreground"\n')
    terminal = _Terminal(project, home, environment)
    attached = None
    sessions = home / ".agenthicc" / "sessions"
    store = BackgroundStore(home / ".agenthicc" / "background")
    lease_pid = None

    def requests() -> list[dict[str, object]]:
        return _journal_rows(project / "requests.jsonl")

    # Status and control commands never use the scripted provider. Keep their
    # fresh interpreters free of the fixture imports, especially under coverage.
    cli_environment = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }

    def status(session_id: str) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, "-m", "agenthicc", "agents", "status", session_id, "--json"],
            cwd=project,
            env=cli_environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=CLI_PROCESS_TIMEOUT_S,
        )
        return json.loads(result.stdout)

    try:
        terminal.wait_for_input()
        terminal.send("/workflow goal_flow\r")
        terminal.pump(0.25)
        terminal.send("Create the five Executor markers in this workspace.\r")
        terminal.wait_for(
            lambda _: sum(row["name"] == "gate" for row in requests()) == 5,
            "five real Executor tool results at the provider barrier",
        )
        (journal,) = sessions.glob("*/conversation-journal.jsonl")
        session_id = journal.parent.name
        owner = SessionOwnerStore(sessions).inspect(session_id).owner
        assert owner is not None and owner.pid != terminal.pid
        lease_pid = owner.pid
        started = [row for row in _journal_rows(journal) if row["kind"] == "subagent_pool_started"]
        assert len(started) == 1
        pool_id, phase_owner = started[0]["pool_id"], started[0]["phase_owner"]
        assert isinstance(phase_owner, dict)
        assert len(started[0]["tasks"]) == 5
        assert all(
            (project / f"worker-{i}.txt").read_text() == f"marker-written-{i}" for i in range(5)
        )
        if next_controller == "terminal":
            (project / "release-workers").touch()
        gate = {
            "streaming": "streaming-gate",
            "terminal": "collect-gate",
        }.get(next_controller, "parked")
        if next_controller != "foreground":
            terminal.wait_for(
                lambda _: any(row["name"] == gate for row in requests()), f"parent {gate}"
            )
        before_runtime = status(session_id)["runtime"]
        assert isinstance(before_runtime, dict) and before_runtime["attached"] is True
        assert before_runtime["pid"] == lease_pid
        before = requests()
        sent_at = time.monotonic()
        terminal.send(f"{alias}\r")
        acknowledgement = _wait_for_detach(terminal)
        assert acknowledgement.count("is running in the background.") == 1
        _leave_the_hub(terminal)
        deadline = sent_at + min(CLI_PROCESS_TIMEOUT_S / 2, 10) + 5
        while time.monotonic() < deadline:
            exit_status = terminal.poll()
            if exit_status is not None:
                assert exit_status == 0
                break
            terminal.pump(0.03)
        else:
            pytest.fail("terminal did not exit while the provider gates were held")
        record = store.get(session_id)
        assert record.status is SessionStatus.RUNNING
        assert record.worker_pid == lease_pid
        assert record.run_id == phase_owner["run_id"]
        runtime = status(session_id)["runtime"]
        assert isinstance(runtime, dict) and runtime["pid"] == lease_pid
        assert runtime["attached"] is False
        assert runtime["pools"] == before_runtime["pools"]
        assert runtime["transcript_cursor"] >= before_runtime["transcript_cursor"]
        (live_pool,) = runtime["pools"]
        assert live_pool["pool_id"] == pool_id
        assert live_pool["phase_owner"] == phase_owner
        assert len(live_pool["workers"]) == 5
        expected_worker_status = "done" if next_controller == "terminal" else "running"
        assert all(
            worker["attempt"] == 1 and worker["status"] == expected_worker_status
            for worker in live_pool["workers"]
        )
        assert SessionOwnerStore(sessions).inspect(session_id).owner == owner
        assert sum(row["name"] == "released" for row in requests()) == (
            5 if next_controller == "terminal" else 0
        )
        assert [row for row in requests() if row["lane"] != "parent"] == [
            row for row in before if row["lane"] != "parent"
        ]

        stopped_worker = None
        if next_controller == "worker-stop":
            stopped_worker = str(live_pool["workers"][-1]["worker_id"])
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agenthicc",
                    "agents",
                    "stop-worker",
                    session_id,
                    str(pool_id),
                    stopped_worker,
                ],
                cwd=project,
                env=cli_environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=CLI_PROCESS_TIMEOUT_S,
            )
            assert "Worker stop requested" in result.stdout
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(
                    row["kind"] == "subagent_worker_result" and row["task_id"] == stopped_worker
                    for row in _journal_rows(journal)
                ):
                    break
                time.sleep(0.025)
            else:
                pytest.fail("Requested worker did not settle while its siblings stayed gated")
            assert store.get(session_id).status is SessionStatus.RUNNING
            assert not any(row["name"] == "released" for row in requests())
        if next_controller == "cancel":
            cancelled = BackgroundSupervisor(store, cancel_grace_s=12).cancel(session_id)
            assert cancelled.status is SessionStatus.CANCELLED
            assert cancelled.cancellation_reason == "user requested cancellation"
            records = _journal_rows(journal)
            assert not any(row["kind"] == "subagent_pool_interrupted" for row in records)
            workers = [row for row in records if row["kind"] == "subagent_worker_result"]
            assert len(workers) == 5 and {row["status"] for row in workers} == {"cancelled"}
            assert all(row["pool_id"] == pool_id for row in workers)
            assert not any(row["name"] == "released" for row in requests())
            return

        if next_controller == "recovery":
            os.kill(lease_pid, signal.SIGKILL)
            replacement = BackgroundSupervisor(BackgroundStore(store.root))
            limit = time.monotonic() + 5
            while (
                time.monotonic() < limit and store.get(session_id).status is SessionStatus.RUNNING
            ):
                replacement.recover_stale()
                time.sleep(0.05)
            assert store.get(session_id).status is SessionStatus.ORPHANED
            interrupts = [
                row for row in _journal_rows(journal) if row["kind"] == "subagent_pool_interrupted"
            ]
            assert len(interrupts) == 1 and interrupts[0]["pool_id"] == pool_id
            assert (
                len(
                    [
                        row
                        for row in _journal_rows(journal)
                        if row["kind"] == "subagent_pool_started"
                    ]
                )
                == 1
            )
            worker_results = [
                row for row in _journal_rows(journal) if row["kind"] == "subagent_worker_result"
            ]
            assert {row["status"] for row in worker_results} == {"interrupted"}
            assert len(worker_results) == 5
            assert replacement.recover_stale() == []
            assert [row for row in requests() if row["lane"] != "parent"] == [
                row for row in before if row["lane"] != "parent"
            ]
            lease_pid = None
            return

        if next_controller == "reattach":
            attached = _Terminal(project, home, environment, "attach", session_id)
            attached.wait_for_input()
            assert store.get(session_id).status is SessionStatus.ATTACHED
            reattached = status(session_id)["runtime"]
            assert isinstance(reattached, dict) and reattached["pid"] == lease_pid
            assert reattached["attached"] is True
            assert reattached["generation"] == runtime["generation"] + 1
            assert reattached["pools"] == runtime["pools"]
            assert reattached["transcript_cursor"] >= runtime["transcript_cursor"]
            attached.send("message from the reattached operator\r")
            attached.wait_for(
                lambda _: any(row["name"] == "reattached-input-observed" for row in requests()),
                "the reattached controller sending input to the original workflow",
            )

        (project / "release-parent").touch()
        (project / "release-workers").touch()
        deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S / 2
        while time.monotonic() < deadline:
            if any(row["name"] == "verify-goal" for row in requests()):
                break
            time.sleep(0.025)
        else:
            pytest.fail(f"Goal flow did not advance after release: {requests()}")
        after = _journal_rows(journal)
        assert [row for row in after if row["kind"] == "subagent_pool_started"] == started
        finished = [row for row in after if row["kind"] == "subagent_worker_result"]
        assert len(finished) == 5
        assert all(row["pool_id"] == pool_id and row.get("attempt", 1) == 1 for row in finished)
        assert {row["task_id"] for row in finished if row["status"] == "cancelled"} == (
            {stopped_worker} if stopped_worker is not None else set()
        )
        assert sum(row["ok"] is True for row in finished) == (4 if stopped_worker else 5)
        assert {row["task_id"] for row in finished} == {
            worker["worker_id"] for worker in live_pool["workers"]
        }
        assert sum(row["name"] == "verify-goal" for row in requests()) == 1
        assert sum(pool_id in row.get("phase_pool_receipts", {}) for row in after) == 1
        assert {row["pid"] for row in requests()} == {lease_pid}
        for index in range(5):
            calls = [row["name"] for row in requests() if row["lane"] == str(index)]
            if stopped_worker == f"task-{index}":
                assert calls == ["action", "gate"]
            else:
                assert calls == ["action", "gate", "released"]
            assert (project / f"worker-{index}.txt").read_text() == f"marker-written-{index}"
        if attached is not None:
            from agenthicc.runners.hosted_session import hosted_terminal_status

            attached.wait_for(
                lambda _: (
                    asyncio.run(hosted_terminal_status(store.get(session_id)))["active_turn"]
                    is False
                ),
                "the original workflow finishing in the attached view",
            )
            attached.send("\x04")
        completion_deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S / 2
        while time.monotonic() < completion_deadline:
            if store.get(session_id).status is SessionStatus.COMPLETED:
                break
            if attached is not None:
                attached.pump(0.05)
            else:
                time.sleep(0.05)
        else:
            pytest.fail("The original runtime did not settle the completed workflow")
        lease_pid = None
    finally:
        if attached is not None:
            attached.close()
        terminal.close()
        if lease_pid is not None:
            try:
                os.kill(lease_pid, 15)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "alias,parent_state",
    [
        ("/detach", "yielded"),
        ("/bg", "yielded"),
        ("/background", "yielded"),
        ("/bg", "streaming"),
        ("/detach", "foreground"),
    ],
)
@cli_test_timeout(3)
def test_direct_session_explorers_keep_running_through_background_handoff(
    tmp_path: Path, alias: str, parent_state: str
) -> None:
    """Five live Explorers in an ordinary session are never cancelled by ``/bg``.

    The original report: after ``Successfully backgrounded`` every Explorer row
    read ``parent_cancelled``. The same workers must instead stay at their
    provider barrier across the cutover and finish under the service. Each one
    that finishes then notifies the parent on its own, with the terminal gone.
    """
    home, project, environment = _scripted_project(tmp_path)
    (project / "direct-explorers").touch()
    if parent_state == "streaming":
        (project / "hold-parent").touch()
    elif parent_state == "foreground":
        with (project / "agenthicc.toml").open("a", encoding="utf-8") as configuration:
            configuration.write('spawn_mode = "foreground"\n')
    terminal = _Terminal(project, home, environment, "--mode", "Yolo")
    sessions = home / ".agenthicc" / "sessions"
    store = BackgroundStore(home / ".agenthicc" / "background")
    lease_pid = None

    def requests() -> list[dict[str, object]]:
        return _journal_rows(project / "requests.jsonl")

    try:
        terminal.wait_for_input()
        terminal.send("Explore the repository with five Explorers.\r")
        terminal.wait_for(
            lambda _: sum(row["name"] == "gate" for row in requests()) == 5,
            "five Explorers holding at the provider barrier",
        )
        if parent_state != "foreground":
            parent_row = "streaming-gate" if parent_state == "streaming" else "direct-yield"
            terminal.wait_for(
                lambda _: any(row["name"] == parent_row for row in requests()),
                f"the parent {parent_state}",
            )
        (journal,) = sessions.glob("*/conversation-journal.jsonl")
        session_id = journal.parent.name
        owner = SessionOwnerStore(sessions).inspect(session_id).owner
        assert owner is not None and owner.pid != terminal.pid
        lease_pid = owner.pid
        (started,) = [
            row for row in _journal_rows(journal) if row["kind"] == "subagent_pool_started"
        ]
        assert "phase_owner" not in started and len(started["tasks"]) == 5
        before = [row for row in requests() if row["lane"] != "parent"]
        assert sum(row["name"] == "action" for row in before) == 5
        # Attached and never detached, the session has no background record. The
        # hub must still list it as a live agent rather than as a saved session.
        assert store.list() == []
        (attached,) = discover_interactive_runtimes(sessions)
        assert attached.session_id == session_id and attached.pid == lease_pid
        assert attached.session_dir == journal.parent

        terminal.send(f"{alias}\r")
        screen = _wait_for_detach(terminal)
        _leave_the_hub(terminal)
        deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S / 2
        while terminal.poll() is None and time.monotonic() < deadline:
            terminal.pump(0.03)
        assert terminal.poll() == 0, "the terminal client should have exited"
        assert "parent_cancelled" not in screen and "cancelled" not in screen.lower()
        assert "5 live workers across 1 pools" in screen

        record = store.get(session_id)
        assert record.status is SessionStatus.RUNNING and record.worker_pid == lease_pid
        assert SessionOwnerStore(sessions).inspect(session_id).owner == owner
        rows = _journal_rows(journal)
        assert not [
            row
            for row in rows
            if row["kind"] in {"subagent_worker_result", "subagent_pool_cancelled"}
        ], "no worker may settle or be cancelled by the handoff"
        assert [row for row in requests() if row["lane"] != "parent"] == before
        assert "parent_cancelled" not in journal.read_text(encoding="utf-8")
        # The agents hub reads the same session while it is detached and live.
        hub = read_transcript(journal.parent / "conversation.jsonl", tail_bytes=2_000_000)
        assert [line.text for line in hub if line.role == SUBAGENT_ROLE][:1] == [
            "5 workers started · " + ", ".join(f"explorer #{number}" for number in range(1, 6))
        ]

        (project / "release-parent").touch()
        notified_rows = [f"notified:explorer #{number}" for number in range(1, 6)]
        for index in range(5):
            (project / f"release-explorer-{index}").touch()
            if parent_state == "foreground":
                continue
            # Explorer index finishes: the parent hears about that Explorer
            # before the next one is released.
            deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
            while not any(row["name"] == notified_rows[index] for row in requests()):
                assert time.monotonic() < deadline, f"no notice for explorer #{index + 1}"
                time.sleep(0.05)
        deadline = time.monotonic() + CLI_PROCESS_TIMEOUT_S
        while time.monotonic() < deadline:
            if store.get(session_id).status is SessionStatus.COMPLETED:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"the detached session did not finish: {store.get(session_id).status}")
        rows = _journal_rows(journal)
        results = [row for row in rows if row["kind"] == "subagent_worker_result"]
        assert len(results) == 5 and all(row["ok"] is True for row in results)
        assert all(row["pool_id"] == started["pool_id"] for row in results)
        assert [row["name"] for row in requests() if row["name"] == "released"] == ["released"] * 5
        assert any(row["name"] == "explorers-collected" for row in requests())
        names = [row["name"] for row in requests()]
        if parent_state == "foreground":
            # A blocking spawn returns its aggregate to the waiting parent; its
            # workers send no notices of their own.
            assert not [name for name in names if name.startswith("notified:")]
        else:
            # Every Explorer notified the parent, each after that Explorer's
            # own release, in the order they finished, and the parent read the
            # first one's report while the pool was still running.
            assert [name for name in names if name.startswith("notified:")] == notified_rows
            released = [row["lane"] for row in requests() if row["name"] == "released"]
            assert released == ["0", "1", "2", "3", "4"]
            for index, notice in enumerate(notified_rows):
                released_at = next(
                    at
                    for at, row in enumerate(requests())
                    if row["lane"] == str(index) and row["name"] == "released"
                )
                assert names.index(notice) > released_at
            assert "partial-collect-ok" in names and "partial-collect-bad" not in names
        assert sum(row["kind"] == "subagent_pool_started" for row in rows) == 1
        assert "parent_cancelled" not in journal.read_text(encoding="utf-8")
        assert {row["pid"] for row in requests()} == {lease_pid}
        # The runtime has exited, so the hub no longer lists it as running.
        assert discover_interactive_runtimes(sessions) == []
        # ... and shows each Explorer finishing and the notice the parent received.
        hub = read_transcript(journal.parent / "conversation.jsonl", tail_bytes=2_000_000)
        shown = [(line.status, line.text) for line in hub if line.role == SUBAGENT_ROLE]
        finished = [text for status, text in shown if status == "✓" and text.startswith("[")]
        assert len(finished) == 5 and all("read the guide." in text for text in finished)
        assert ("✓", "5/5 done") in shown
        notified = [text for _, text in shown if text.startswith("parent notified")]
        assert len(notified) == (0 if parent_state == "foreground" else 5)
        assert [match[1] for text in notified if (match := re.search(r"(explorer #\d)", text))] == (
            [] if parent_state == "foreground" else [f"explorer #{n}" for n in range(1, 6)]
        )
        assert not [
            line for line in hub if line.role == "you" and "Background subagent" in line.text
        ]
    finally:
        terminal.close()
        (project / "release-parent").touch()
        for index in range(5):
            (project / f"release-explorer-{index}").touch()
        if lease_pid is not None:
            try:
                os.kill(lease_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


@cli_test_timeout(2)
def test_pending_operator_question_and_fifo_input_cross_detach(tmp_path: Path) -> None:
    home, project, environment = _scripted_project(tmp_path)
    (project / "ask-at-clarify").touch()
    (project / "inject-input-ack").touch()
    terminal = _Terminal(project, home, environment)
    store = BackgroundStore(home / ".agenthicc" / "background")
    sessions = home / ".agenthicc" / "sessions"
    lease_pid: int | None = None
    try:
        terminal.wait_for_input()
        terminal.send("/workflow goal_flow\r")
        terminal.pump(0.25)
        terminal.send("Ask the operator a question.\r")
        terminal.wait_for(
            lambda text: "Which answer crosses the cutover?" in text,
            "the original question overlay",
        )
        (journal,) = sessions.glob("*/conversation-journal.jsonl")
        session_id = journal.parent.name
        owner = SessionOwnerStore(sessions).inspect(session_id).owner
        assert owner is not None
        lease_pid = owner.pid
        wait_started = next(
            row
            for row in _journal_rows(journal.parent / "conversation.jsonl")
            if row.get("kind") == "question_wait_started"
        )
        terminal.send("/bg\r")
        terminal.wait_for(lambda text: "running in the background" in text, "question handoff")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record = store.get(session_id)
            if record.status is SessionStatus.WAITING_INPUT:
                break
            time.sleep(0.025)
        else:
            pytest.fail("The live question did not appear in the background inbox")
        inbox = BackgroundInteractionInbox(store)
        while time.monotonic() < deadline and inbox.get(record.input_item_id).status != "pending":
            time.sleep(0.025)
        item = inbox.get(record.input_item_id)
        assert item.request_id == wait_started["payload"]["request_id"]
        assert item.expires_at == wait_started["payload"]["deadline_at"]
        inputs = BackgroundInputInbox(store)
        for index in range(2):
            inputs.enqueue(
                session_id,
                f"ordered message {index}",
                owner_attempt=record.attempt,
                lease_token=record.lease_token,
                message_id=f"message-{index}",
            )
        answer = {"answers": {"cutover": "operator-confirmed"}}
        accepted, replay = inbox.respond(
            item.item_id, answer, expected_version=item.version, idempotency_key="winning-reply"
        )
        assert accepted.status == "accepted" and not replay
        _, replay = inbox.respond(
            item.item_id, answer, expected_version=item.version, idempotency_key="winning-reply"
        )
        assert replay
        with pytest.raises(InboxConflict):
            inbox.respond(
                item.item_id,
                {"answers": {"cutover": "late reply"}},
                expected_version=item.version,
                idempotency_key="stale-reply",
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            logged = _journal_rows(project / "requests.jsonl")
            received = [
                row["payload"]["message_id"]
                for row in _journal_rows(journal.parent / "conversation.jsonl")
                if row.get("kind") == "background_input_received"
            ]
            if received == ["message-0", "message-1"] and any(
                row["name"] == "operator-confirmed" for row in logged
            ):
                break
            time.sleep(0.025)
        else:
            pytest.fail("The original turn did not receive the accepted input and answer")
        assert (project / "ack-failed").exists()
        assert [receipt.state for receipt in inputs.receipts(session_id)] == [
            "delivered",
            "delivered",
        ]
        assert inbox.get(item.item_id).status == "delivered"
        assert SessionOwnerStore(sessions).inspect(session_id).owner == owner
        assert all(row["pid"] == lease_pid for row in logged)
    finally:
        terminal.close()
        if lease_pid is not None:
            try:
                os.kill(lease_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


@cli_test_timeout(2)
def test_hosted_editor_receives_the_terminal_and_returns_to_the_composer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = _repository(tmp_path)
    editor = tmp_path / "editor.py"
    editor.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "assert all(os.isatty(fd) for fd in (0, 1, 2))\n"
        "path = Path(sys.argv[1])\n"
        "assert path.read_text() == 'original draft'\n"
        "response = input('Editor waiting on the physical terminal: ')\n"
        "path.write_text(response)\n",
        encoding="utf-8",
    )
    terminal = _Terminal(project, home, {"VISUAL": f"{sys.executable} {editor}"})
    try:
        terminal.wait_for_input()
        terminal.send("original draft")
        terminal.send("\x18\x05")
        terminal.wait_for(
            lambda screen: "Editor waiting on the physical terminal" in screen,
            "the external editor on the real terminal",
        )
        terminal.send("edited draft\r")
        terminal.wait_for(lambda screen: "❯ edited draft" in screen, "restored hosted composer")
    finally:
        terminal.close()


@pytest.mark.parametrize("fault", ["before-prepare", "after-prepare", "after-commit"])
@cli_test_timeout(2)
def test_lost_client_reconciles_the_durable_handoff_without_replaying_pool(
    tmp_path: Path, fault: str
) -> None:
    home, project, environment = _scripted_project(tmp_path)
    (project / "direct-pool").touch()
    (project / "cutover-fault").write_text(fault, encoding="utf-8")
    terminal = _Terminal(project, home, environment, "--mode", "Yolo")
    sessions = home / ".agenthicc" / "sessions"
    store = BackgroundStore(home / ".agenthicc" / "background")
    lease_pid = None
    try:
        terminal.wait_for_input()
        terminal.send("Run a direct ownerless pool with a marker.\r")
        terminal.wait_for(
            lambda _: any(
                row["name"] == "gate" for row in _journal_rows(project / "requests.jsonl")
            ),
            "direct worker provider barrier",
        )
        (journal,) = sessions.glob("*/conversation-journal.jsonl")
        session_id = journal.parent.name
        started = [row for row in _journal_rows(journal) if row["kind"] == "subagent_pool_started"]
        assert len(started) == 1 and "phase_owner" not in started[0]
        owner = SessionOwnerStore(sessions).inspect(session_id).owner
        assert owner is not None
        lease_pid = owner.pid
        terminal.send("/background\r")
        terminal.wait_for(lambda _: (project / "cutover-blocked").is_file(), fault)
        if fault == "before-prepare":
            assert store.active_sessions() == []
        else:
            expected = (
                SessionStatus.RUNNING if fault == "after-commit" else SessionStatus.HANDOFF_PREPARED
            )
            assert store.get(session_id).status is expected
        terminal.close()
        # The controller socket has closed while the executor thread still holds the
        # failpoint. The original runtime remains the owner when it reconciles.
        (project / "cutover-release").touch()
        deadline = time.monotonic() + 10
        if fault == "after-commit":
            assert SessionOwnerStore(sessions).inspect(session_id).owner == owner
            replacement = BackgroundSupervisor(BackgroundStore(store.root))
            assert replacement.recover_stale() == []
            assert replacement.store.get(session_id).attempt == 1
            (project / "release-workers").touch()
            while time.monotonic() < deadline:
                if any(row["kind"] == "subagent_worker_result" for row in _journal_rows(journal)):
                    break
                time.sleep(0.025)
            else:
                pytest.fail("The committed runtime lost the original worker after client death")
            (result,) = [
                row for row in _journal_rows(journal) if row["kind"] == "subagent_worker_result"
            ]
            assert result["ok"] is True and result["pool_id"] == started[0]["pool_id"]
        else:
            while time.monotonic() < deadline:
                if (
                    store.active_sessions() == []
                    and SessionOwnerStore(sessions).inspect(session_id).state != "active"
                ):
                    break
                time.sleep(0.025)
            else:
                pytest.fail("Uncommitted handoff did not relinquish the preparation")
            assert all(record.attempt == 0 for record in store.list())
        assert [
            row for row in _journal_rows(journal) if row["kind"] == "subagent_pool_started"
        ] == started
        assert (project / "worker-0.txt").read_text() == "marker-written-0"
    finally:
        terminal.close()
        (project / "cutover-release").touch()
        if lease_pid is not None:
            try:
                os.kill(lease_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


@cli_test_timeout(2)
def test_unavailable_local_transport_starts_a_terminal_owned_session(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = _repository(tmp_path)
    script = tmp_path / "sitecustomize.py"
    script.write_text(
        "import asyncio\n"
        "async def disabled(*args, **kwargs):\n"
        "    raise OSError('local bind disabled by fixture')\n"
        "asyncio.start_server = disabled\n",
        encoding="utf-8",
    )
    source = Path(__file__).resolve().parents[2] / "src"
    terminal = _Terminal(
        project, home, {"PYTHONPATH": os.pathsep.join((str(tmp_path), str(source)))}
    )
    try:
        terminal.wait_for_input()
        assert "Starting a terminal-owned session" in terminal.text()
        sessions = home / ".agenthicc" / "sessions"
        (lease_file,) = sessions.glob("*/.owner")
        lease = SessionOwnerStore(sessions).inspect(lease_file.parent.name).owner
        assert lease is not None and lease.entrypoint == "tui" and lease.pid == terminal.pid
        assert not (lease_file.parent / ".controller").exists()
    finally:
        terminal.close()


@cli_test_timeout(2)
def test_background_capacity_refusal_leaves_the_hosted_controller_and_worker_live(
    tmp_path: Path,
) -> None:
    from agenthicc.background.model import BackgroundSession

    home, project, environment = _scripted_project(tmp_path)
    (project / "direct-pool").touch()
    with (project / "agenthicc.toml").open("a", encoding="utf-8") as configuration:
        configuration.write("[background]\nmax_workers = 1\n")
    store = BackgroundStore(home / ".agenthicc" / "background")
    store.create(
        BackgroundSession.create(
            "occupies-capacity",
            title="Other accepted session",
            cwd=str(project),
            workflow_name="",
            intent="Hold one existing background slot",
        )
    )
    sessions = home / ".agenthicc" / "sessions"
    terminal = _Terminal(project, home, environment, "--mode", "Yolo")
    pid = None
    try:
        terminal.wait_for_input()
        terminal.send("Run the direct pool.\r")
        terminal.wait_for(
            lambda _: any(
                row["name"] == "gate" for row in _journal_rows(project / "requests.jsonl")
            ),
            "worker holding at the provider gate",
        )
        (journal,) = sessions.glob("*/conversation-journal.jsonl")
        session_id = journal.parent.name
        initial = SessionOwnerStore(sessions).inspect(session_id).owner
        assert initial is not None
        pid = initial.pid
        terminal.send("/detach\r")
        terminal.wait_for(lambda screen: "capacity is full" in screen, "typed capacity refusal")
        assert terminal.poll() is None
        assert SessionOwnerStore(sessions).inspect(session_id).owner == initial
        assert len(store.active_sessions()) == 1
        store.transition("occupies-capacity", SessionStatus.FAILED)
        terminal.send("/bg\r")
        terminal.wait_for(lambda screen: "running in the background" in screen, "accepted retry")
        assert store.get(session_id).worker_pid == pid
        assert sum(row["kind"] == "subagent_pool_started" for row in _journal_rows(journal)) == 1
        (project / "release-workers").touch()
        deadline = time.monotonic() + 10
        while (
            time.monotonic() < deadline
            and store.get(session_id).status is not SessionStatus.COMPLETED
        ):
            time.sleep(0.025)
        assert store.get(session_id).status is SessionStatus.COMPLETED
        pid = None
    finally:
        terminal.close()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
