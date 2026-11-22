"""SUBAGENT-15: parallel workers that can change files run in isolated git worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lauren_ai._agents._runner import AgentRunnerBase
from lauren_ai._transport import Completion, TokenUsage
from lauren_ai._transport._mock import MockTransport

from agenthicc.agent_tools import AGENT_TOOLS
from agenthicc.config import SubagentSettings
from agenthicc.subagents.tool import make_spawn_subagents_tool
from agenthicc.tools.workspace_access import (
    WorkspaceAccessPolicy,
    WorkspaceScope,
    reset_current_workspace_access,
    set_current_workspace_access,
)
from agenthicc.tui.conversation_store import AppState
from agenthicc.tui.runtime.mode_manager import build_default_registry

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Worktrees live under ``~/.agenthicc``; keep them out of the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "shared.py").write_text("def a():\n    return 1\n")
    (root / "notes.md").write_text("notes\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _final(text: str) -> Completion:
    return Completion(
        id="final",
        model="mock-model",
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


async def _spawn(
    root: Path,
    transport: MockTransport,
    tasks: list[dict[str, str]],
    *,
    isolation: str = "auto",
) -> dict[str, object]:
    state = AppState.create()
    state.active_mode.set(build_default_registry().get("Yolo"))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(root), mode_provider=state.active_mode)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        list(AGENT_TOOLS),
        app_state=state,
        workspace_access=policy,
        subagent_settings=SubagentSettings(isolation=isolation),  # type: ignore[arg-type]
    )
    token = set_current_workspace_access(policy)
    try:
        return await spawn(tasks=tasks, max_concurrent=4, timeout_s=60)  # type: ignore[no-any-return]
    finally:
        reset_current_workspace_access(token)


def _two_implementers() -> list[dict[str, str]]:
    return [
        {"type": "implementer", "task": "Write the first file."},
        {"type": "implementer", "task": "Write the second file."},
    ]


def _worktrees(repo: Path) -> list[str]:
    return [
        line.split(" ", 1)[1]
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def _branches(repo: Path) -> list[str]:
    return _git(repo, "branch", "--format=%(refname:short)").splitlines()


# ── parallel workers on different files ──────────────────────────────────────


async def test_parallel_implementers_on_different_files_both_land(repo: Path) -> None:
    transport = MockTransport()
    transport.queue_tool_use(
        "write_file", {"path": "alpha.py", "content": "ALPHA\n"}, tool_use_id="a"
    )
    transport.queue_tool_use(
        "write_file", {"path": "beta.py", "content": "BETA\n"}, tool_use_id="b"
    )
    transport.queue_response(_final("Wrote one file."))
    transport.queue_response(_final("Wrote the other file."))

    result = await _spawn(repo, transport, _two_implementers())

    assert result["ok"] is True and result["succeeded"] == 2
    assert (repo / "alpha.py").read_text() == "ALPHA\n"
    assert (repo / "beta.py").read_text() == "BETA\n"
    assert str(result["results"]).count("applied to your workspace") == 2
    assert _worktrees(repo) == [str(repo)]  # nothing left behind
    assert _branches(repo) == ["main"]


async def test_the_parents_head_and_index_are_untouched_by_a_pool(repo: Path) -> None:
    (repo / "notes.md").write_text("parent edit\n")
    head = _git(repo, "rev-parse", "HEAD")
    transport = MockTransport()
    transport.queue_tool_use("write_file", {"path": "alpha.py", "content": "A\n"}, tool_use_id="a")
    transport.queue_tool_use("write_file", {"path": "beta.py", "content": "B\n"}, tool_use_id="b")
    transport.queue_response(_final("done"))
    transport.queue_response(_final("done"))

    await _spawn(repo, transport, _two_implementers())

    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    assert (repo / "notes.md").read_text() == "parent edit\n"


# ── parallel workers on the same file ────────────────────────────────────────


async def test_overlapping_workers_apply_one_and_report_the_other(repo: Path) -> None:
    transport = MockTransport()
    transport.queue_tool_use(
        "write_file",
        {"path": "shared.py", "content": "def a():\n    return 'A'\n"},
        tool_use_id="a",
    )
    transport.queue_tool_use(
        "write_file",
        {"path": "shared.py", "content": "def a():\n    return 'B'\n"},
        tool_use_id="b",
    )
    transport.queue_response(_final("Edited shared.py."))
    transport.queue_response(_final("Edited shared.py."))

    result = await _spawn(repo, transport, _two_implementers())

    text = str(result["results"])
    assert result["ok"] is False and result["succeeded"] == 1 and result["failed"] == 1
    landed = (repo / "shared.py").read_text()
    assert landed in {
        "def a():\n    return 'A'\n",
        "def a():\n    return 'B'\n",
    }  # one, never a blend
    assert "applied to your workspace" in text
    assert "its changes conflict with the workspace in shared.py" in text
    assert "NOT applied: kept on branch agenthicc/" in text
    # The losing worker's work is preserved and readable.
    assert len(_worktrees(repo)) == 2
    kept = next(path for path in _worktrees(repo) if path != str(repo))
    other = "'B'" if "'A'" in landed else "'A'"
    assert other in (Path(kept) / "shared.py").read_text()


# ── what a worker starts from, and what counts as its work ───────────────────


async def test_a_worker_starts_from_the_parents_uncommitted_work(repo: Path) -> None:
    (repo / "notes.md").write_text("edited, not committed\n")
    transport = MockTransport()
    transport.queue_tool_use("read_file", {"path": "notes.md"}, tool_use_id="r")
    transport.queue_response(_final("Read the notes."))

    result = await _spawn(
        repo, transport, [{"type": "implementer", "task": "Read notes.md."}], isolation="worktree"
    )

    assert "edited, not committed" in str(transport.calls[1].messages[-1])
    assert result["results"]  # the worker ran in a worktree, not the parent directory
    assert _worktrees(repo) == [str(repo)]


async def test_a_change_made_by_a_command_is_the_workers_evidence(repo: Path) -> None:
    transport = MockTransport()
    transport.queue_tool_use("run_bash", {"command": "echo generated > made.txt"}, tool_use_id="b")
    transport.queue_response(_final("Generated made.txt."))

    result = await _spawn(
        repo,
        transport,
        [{"type": "implementer", "task": "Generate made.txt with a command."}],
        isolation="worktree",
    )

    # An implementer needs a mutating call; only git saw this change, and it counts.
    assert result["ok"] is True
    assert "changed made.txt" in str(result["results"])
    assert (repo / "made.txt").read_text().strip() == "generated"


async def test_a_worker_runs_inside_its_worktree_not_the_parent_directory(repo: Path) -> None:
    transport = MockTransport()
    transport.queue_tool_use("run_bash", {"command": "pwd"}, tool_use_id="p")
    transport.queue_response(_final("Printed the directory."))

    await _spawn(
        repo, transport, [{"type": "implementer", "task": "Print pwd."}], isolation="worktree"
    )

    printed = str(transport.calls[1].messages[-1])
    assert str(repo) not in printed.replace("/worktrees/", "")  # not the parent checkout
    assert "worktrees" in printed


# ── when isolation applies ───────────────────────────────────────────────────


async def test_auto_runs_shared_when_only_one_worker_can_change_files(
    repo: Path, tmp_path: Path
) -> None:
    transport = MockTransport()
    transport.queue_tool_use("write_file", {"path": "alpha.py", "content": "A\n"}, tool_use_id="a")
    transport.queue_response(_final("Wrote alpha.py."))
    transport.queue_response(_final("Nothing found."))

    result = await _spawn(
        repo,
        transport,
        [
            {"type": "implementer", "task": "Write alpha.py."},
            {"type": "explorer", "task": "Look around."},
        ],
    )

    assert result["ok"] is True
    assert (repo / "alpha.py").read_text() == "A\n"
    assert "applied to your workspace" not in str(result["results"])  # never isolated
    assert not (tmp_path / "home" / ".agenthicc" / "worktrees").exists()


async def test_investigative_workers_never_get_a_worktree(repo: Path, tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_response(_final("Found nothing."))
    transport.queue_response(_final("Found nothing either."))

    result = await _spawn(
        repo,
        transport,
        [{"type": "explorer", "task": "Look."}, {"type": "reviewer", "task": "Review."}],
        isolation="worktree",
    )

    assert result["ok"] is True
    assert not (tmp_path / "home" / ".agenthicc" / "worktrees").exists()


async def test_auto_outside_git_runs_shared_and_says_so(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    transport = MockTransport()
    transport.queue_tool_use("write_file", {"path": "alpha.py", "content": "A\n"}, tool_use_id="a")
    transport.queue_tool_use("write_file", {"path": "beta.py", "content": "B\n"}, tool_use_id="b")
    transport.queue_response(_final("done"))
    transport.queue_response(_final("done"))

    result = await _spawn(plain, transport, _two_implementers())

    assert result["ok"] is True
    assert (plain / "alpha.py").exists() and (plain / "beta.py").exists()
    text = str(result["results"])
    assert "=== Note ===" in text
    assert "is not inside a git repository" in text
    assert "can overwrite one another" in text


async def test_worktree_mode_outside_git_refuses_the_pool(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    transport = MockTransport()

    result = await _spawn(
        plain, transport, [{"type": "implementer", "task": "Write."}], isolation="worktree"
    )

    assert result["ok"] is False and result["status"] == "refused"
    assert "is not inside a git repository" in str(result["error"])
    assert transport.calls == []


async def test_shared_mode_never_isolates(repo: Path, tmp_path: Path) -> None:
    transport = MockTransport()
    transport.queue_tool_use("write_file", {"path": "alpha.py", "content": "A\n"}, tool_use_id="a")
    transport.queue_tool_use("write_file", {"path": "beta.py", "content": "B\n"}, tool_use_id="b")
    transport.queue_response(_final("done"))
    transport.queue_response(_final("done"))

    result = await _spawn(repo, transport, _two_implementers(), isolation="shared")

    assert result["ok"] is True
    assert "applied to your workspace" not in str(result["results"])
    assert not (tmp_path / "home" / ".agenthicc" / "worktrees").exists()


def test_the_isolation_setting_is_validated() -> None:
    assert SubagentSettings().isolation == "auto"
    with pytest.raises(ValueError, match="subagents.isolation"):
        SubagentSettings(isolation="sometimes")  # type: ignore[arg-type]


# ── what the parent and the event log are told ───────────────────────────────


def test_the_spawn_description_tells_the_parent_how_to_parallelise(tmp_path: Path) -> None:
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=MockTransport()), "mock-model", list(AGENT_TOOLS)
    )

    description = str(spawn.__lauren_ai_tool__.parameters["description"])  # type: ignore[attr-defined]

    assert "Delegate independent work in parallel." in description
    assert "Give each worker different files." in description
    assert "its own git worktree" in description
    assert "overlapping edits are reported as conflicts" in description


async def test_the_worker_event_carries_the_integration_outcome(repo: Path) -> None:
    from agenthicc.tui.conversation_store import ConversationStore

    transport = MockTransport()
    transport.queue_tool_use("write_file", {"path": "alpha.py", "content": "A\n"}, tool_use_id="a")
    transport.queue_tool_use("write_file", {"path": "beta.py", "content": "B\n"}, tool_use_id="b")
    transport.queue_response(_final("done"))
    transport.queue_response(_final("done"))
    state = AppState.create()
    state.active_mode.set(build_default_registry().get("Yolo"))
    policy = WorkspaceAccessPolicy(WorkspaceScope.create(repo), mode_provider=state.active_mode)
    store = ConversationStore()
    store.begin_turn("agent", "turn-1")
    seen: list[object] = []
    store.on_event(seen.append)
    spawn = make_spawn_subagents_tool(
        AgentRunnerBase(transport=transport),
        "mock-model",
        list(AGENT_TOOLS),
        app_state=state,
        workspace_access=policy,
        conv_store=store,
    )
    token = set_current_workspace_access(policy)
    try:
        await spawn(tasks=_two_implementers(), max_concurrent=2, timeout_s=60)
    finally:
        reset_current_workspace_access(token)

    done = [
        event.payload  # type: ignore[attr-defined]
        for event in seen
        if getattr(event, "kind", "") == "subagent_worker_done"
    ]
    assert len(done) == 2
    assert {payload["integration"] for payload in done} == {"applied"}
    assert sorted(payload["changed_paths"][0] for payload in done) == ["alpha.py", "beta.py"]
