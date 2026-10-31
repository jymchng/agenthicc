from __future__ import annotations

import json
import hashlib
import os
import socket
import shlex
import subprocess
import threading
import asyncio
import time
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from pathlib import Path

import pytest

from agenthicc.worktrees.environment import (
    EnvironmentCommand,
    EnvironmentCommandResult,
    EnvironmentManifestError,
    EnvironmentRunReceipt,
    EnvironmentTrustRequired,
    EnvironmentTrustStore,
    WorktreeEnvironment,
    load_environment_manifest,
)


def _hub(tmp_path: Path, session: object | None = None, width: int = 100) -> object:
    from rich.console import Console

    from agenthicc.background import BackgroundSession, BackgroundStore, BackgroundSupervisor
    from agenthicc.tui.workspace.agents_hub import AgentsHub
    from agenthicc.tui.workspace.agents_hub.prefs import HubPrefs

    store = BackgroundStore(tmp_path / "sessions")
    hub = AgentsHub(
        Console(file=StringIO(), force_terminal=False, width=width),
        store=store,
        supervisor=BackgroundSupervisor(store),
        prefs=HubPrefs(path=tmp_path / "hub.json"),
    )
    if isinstance(session, BackgroundSession):
        store.create(session)
        hub._sessions[session.session_id] = session
        hub._selected_key = f"session:{session.session_id}"
        hub._detail_id = session.session_id
    return hub


def _worker(tmp_path: Path) -> object:
    from agenthicc.background import BackgroundSession

    return BackgroundSession.create(
        "worker-session",
        title="worker",
        cwd=str(tmp_path),
        workflow_name="",
        intent="task",
        role="worker",
        task_id="tests",
        worktree_id="worktree-1",
    )


def _text(hub: object, width: int, height: int) -> str:
    from rich.console import Console

    from agenthicc.tui.workspace.agents_hub import AgentsHub

    assert isinstance(hub, AgentsHub)
    console = Console(file=StringIO(), force_terminal=False, width=width)
    with console.capture() as capture:
        console.print(hub.render(width, height))
    return capture.get()


def test_agents_detail_lists_previews_and_requires_y_before_running_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub
    from agenthicc.worktrees import environment_actions

    hub = _hub(tmp_path, _worker(tmp_path))
    assert isinstance(hub, AgentsHub)
    action = EnvironmentCommand("test", "pytest -q", cwd=".", timeout_s=30, network="none_declared")
    reviewed_digest = "reviewed-manifest-digest"
    verified_workspace = tmp_path / "verified-worker"
    monkeypatch.setattr(
        environment_actions,
        "available_session_actions_with_context",
        lambda target: ((action,), reviewed_digest, verified_workspace),
    )
    approval: list[tuple[bool, str]] = []

    def run_action(
        *args: object,
        approved: bool,
        expected_manifest_digest: str,
        **kwargs: object,
    ) -> object:
        del args, kwargs
        approval.append((approved, expected_manifest_digest))
        return SimpleNamespace(result="succeeded")

    monkeypatch.setattr(environment_actions, "run_session_action", run_action)
    operation: list[object] = []
    hub._start_operation = lambda _target, _label, callback: operation.append(callback)  # type: ignore[method-assign]

    hub.handle_key(Key.CHAR, "x")
    assert hub._environment_action_mode == "select"
    hub.handle_key(Key.ENTER, "")
    assert hub._environment_action_mode == "preview"
    preview = _text(hub, 100, 20)
    assert "pytest -q" in preview
    assert "none_declared" in preview
    assert "".join(str(verified_workspace).split()) in "".join(preview.split())
    assert not approval

    hub.handle_key(Key.CHAR, "y")
    assert len(operation) == 1
    asyncio.run(operation[0]())  # type: ignore[operator]
    assert approval == [(True, reviewed_digest)]


def test_cancelling_agents_action_signals_its_subprocess_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub
    from agenthicc.worktrees import environment_actions

    hub = _hub(tmp_path, _worker(tmp_path))
    assert isinstance(hub, AgentsHub)
    hub._environment_actions = (EnvironmentCommand("slow", "sleep 20", timeout_s=30),)
    hub._environment_action_mode = "preview"
    started = threading.Event()
    observed_cancel = threading.Event()

    def blocking_action(*args: object, cancel_event: threading.Event, **kwargs: object) -> object:
        del args, kwargs
        started.set()
        cancel_event.wait(timeout=2)
        if cancel_event.is_set():
            observed_cancel.set()
        return SimpleNamespace(result="cancelled")

    monkeypatch.setattr(environment_actions, "run_session_action", blocking_action)

    async def cancel_the_ui_task() -> None:
        hub.handle_key(Key.CHAR, "y")
        operation = next(iter(hub._operation_tasks.values()))
        await asyncio.to_thread(started.wait, 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(cancel_the_ui_task())
    assert observed_cancel.is_set()


def test_agents_action_notice_surfaces_truncated_output(tmp_path: Path) -> None:
    from agenthicc.tui.workspace.agents_hub import AgentsHub

    hub = _hub(tmp_path)
    assert isinstance(hub, AgentsHub)
    receipt = EnvironmentRunReceipt(
        "digest",
        "action:test",
        "started",
        "ended",
        "succeeded",
        (
            EnvironmentCommandResult(
                "test",
                "started",
                "ended",
                0,
                "succeeded",
                stdout="tests passed",
                truncated=True,
            ),
        ),
    )

    async def operation() -> object:
        return receipt

    asyncio.run(hub._run_operation("operation", "session", "environment-action", operation))

    assert "tests passed" in hub.notice
    assert "output truncated" in hub.notice


def test_long_action_preview_is_scrollable_without_truncating_command(tmp_path: Path) -> None:
    from agenthicc.tui.cbreak_reader import Key
    from agenthicc.tui.workspace.agents_hub import AgentsHub

    command = "command-" + "0123456789" * 24
    hub = _hub(tmp_path, _worker(tmp_path), width=48)
    assert isinstance(hub, AgentsHub)
    hub._environment_action_mode = "preview"
    hub._environment_actions = (EnvironmentCommand("long", command),)
    hub._environment_manifest_digest = "reviewed-digest"
    hub._environment_workspace_root = tmp_path / "worker"

    first_page = _text(hub, 48, 30)
    assert command in "".join(
        line[14:].strip() for line in first_page.splitlines() if "command-" in line or "012" in line
    )
    hub.handle_key(Key.DOWN, "")
    assert hub._detail_scroll == 1
    assert _text(hub, 48, 30) != first_page
    hub.handle_key(Key.END, "")
    assert hub._detail_scroll > 1


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Environment tests")
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "initial")
    (root / ".agenthicc").mkdir()
    return root


def _manifest(root: Path, body: str) -> Path:
    path = root / ".agenthicc" / "environment.toml"
    path.write_text("version = 1\n" + body, encoding="utf-8")
    return path


# Sandboxed commands use only the shell and base system tools. The interpreter
# running the tests can live where the sandbox cannot reach it — another user's
# private home when the suite runs as root — and would then fail every command.
def _command_line(command: str) -> str:
    return "command = " + json.dumps(command) + "\n"


def test_manifest_parses_setup_actions_and_cache_declarations(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        """
[[setup]]
id = "install"
command = "python -m pip install -r requirements.txt"
timeout_s = 120
network = "requires_network"
expected_outputs = [".venv"]

[actions.test]
command = "pytest -q"
cwd = "."
timeout_s = 30
network = "none_declared"

[[caches]]
name = "pip"
env_var = "PIP_CACHE_DIR"
path = "pip"
sharing = "isolated"
""",
    )

    parsed = load_environment_manifest(root)

    assert parsed is not None
    assert len(parsed.digest) == 64
    assert parsed.setup[0].command_id == "install"
    assert parsed.actions[0].command_id == "test"
    assert parsed.caches[0].sharing == "isolated"
    assert any("requires_network" in line for line in parsed.preview())


def test_missing_environment_dependency_and_platform_mismatch_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTHICC_TEST_REQUIRED_MISSING", raising=False)
    root = _repo(tmp_path)
    _manifest(
        root,
        "[actions.missing_env]\ncommand = 'true'\nenv = ['AGENTHICC_TEST_REQUIRED_MISSING']\n"
        + "[actions.platform]\ncommand = 'touch should-not-exist'\n"
        + "platforms = ['unsupported-agenthicc-test-platform']\n",
    )
    environment = WorktreeEnvironment(root, "wt-platform")

    missing = environment.run_action("missing_env", approved=True).commands[0]
    mismatch = environment.run_action("platform", approved=True).commands[0]

    assert missing.result == "failed"
    assert "AGENTHICC_TEST_REQUIRED_MISSING" in missing.error
    assert mismatch.result == "skipped_platform"
    assert not (root / "should-not-exist").exists()


def test_default_isolated_caches_are_partitioned_per_worker(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        '[[caches]]\nname = "build"\nenv_var = "BUILD_CACHE"\npath = "build"\n',
    )
    manifest = load_environment_manifest(root)
    assert manifest is not None
    first, _ = WorktreeEnvironment(root, "worker-a", cache_root=tmp_path / "caches")._cache_values(
        manifest
    )
    second, _ = WorktreeEnvironment(root, "worker-b", cache_root=tmp_path / "caches")._cache_values(
        manifest
    )

    assert first["BUILD_CACHE"] != second["BUILD_CACHE"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[actions.test]\ncommand = 'pytest'\n", "version = 1"),
        ("version = true\n", "version = 1"),
        (
            'version = 1\n[actions.test]\ncommand = "pytest"\ntimeout_s = 9000\n',
            "timeout_s",
        ),
        (
            'version = 1\n[actions.test]\ncommand = "pytest"\nenv = ["TOKEN"]\n'
            'secret_env = ["OTHER"]\n',
            "must also appear",
        ),
        (
            'version = 1\n[[caches]]\nname="build"\nenv_var="BUILD_CACHE"\n'
            'path="build"\nsharing="mutable_shared"\n',
            "exclusive_lock",
        ),
        (
            'version = 1\n[[caches]]\nname="one"\nenv_var="CACHE_DIR"\npath="one"\n'
            '[[caches]]\nname="two"\nenv_var="CACHE_DIR"\npath="two"\n',
            "environment variable names must be unique",
        ),
    ],
)
def test_manifest_validation_rejects_invalid_contracts(
    tmp_path: Path, body: str, message: str
) -> None:
    root = _repo(tmp_path)
    (root / ".agenthicc" / "environment.toml").write_text(body, encoding="utf-8")
    with pytest.raises(EnvironmentManifestError, match=message):
        load_environment_manifest(root)


def test_manifest_symlink_cannot_escape_repository(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside.toml"
    outside.write_text("version = 1\n", encoding="utf-8")
    (root / ".agenthicc" / "environment.toml").symlink_to(outside)

    with pytest.raises(EnvironmentManifestError, match="inside repository"):
        load_environment_manifest(root)


def test_manifest_preview_rejects_working_directory_escape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(root, "[actions.safe]\ncommand = 'true'\n")
    manifest = load_environment_manifest(root)
    assert manifest is not None
    unsafe_action = replace(manifest.actions[0], cwd="../../outside")
    unsafe_manifest = replace(manifest, actions=(unsafe_action,))

    with pytest.raises(EnvironmentManifestError, match="escapes the worktree"):
        unsafe_manifest.preview(workspace_root=tmp_path / "planned-worker")


def test_untrusted_manifest_requires_explicit_approval_and_digest_trust(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest_path = _manifest(root, '[actions.test]\ncommand = "echo safe"\n')
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    environment = WorktreeEnvironment(root, "wt-1", trust_store=trust)
    manifest = environment.load()
    assert manifest is not None

    with pytest.raises(EnvironmentTrustRequired, match="exact digest"):
        environment.run_action("test")

    trust.trust(manifest)
    receipt = environment.run_action("test")
    assert receipt.result == "succeeded", receipt.commands
    manifest_path.write_text(
        'version = 1\n[actions.test]\ncommand = "echo changed"\n', encoding="utf-8"
    )
    with pytest.raises(EnvironmentTrustRequired):
        environment.run_action("test")


def test_manifest_must_still_match_the_reviewed_digest_at_execution_time(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    manifest_path = _manifest(root, '[actions.test]\ncommand = "echo original"\n')
    environment = WorktreeEnvironment(root, "wt-digest")
    reviewed = environment.load()
    assert reviewed is not None

    manifest_path.write_text(
        'version = 1\n[actions.test]\ncommand = "echo changed"\n',
        encoding="utf-8",
    )
    with pytest.raises(EnvironmentManifestError, match="changed after the action was reviewed"):
        environment.run_action(
            "test",
            approved=True,
            expected_manifest_digest=reviewed.digest,
        )


def test_setup_rejects_manifest_created_after_preflight_approved_absence(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    environment = WorktreeEnvironment(root, "wt-absent-digest")
    (root / ".agenthicc" / "environment.toml").write_text(
        'version = 1\n[[setup]]\ncommand = "echo must-not-run"\n',
        encoding="utf-8",
    )

    with pytest.raises(EnvironmentManifestError, match="changed after it was reviewed"):
        environment.run_setup(approved=True, expected_manifest_digest="")


def test_git_worktree_uses_same_repository_trust_identity(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(root, '[actions.test]\ncommand = "echo safe"\n')
    _git(root, "add", ".agenthicc/environment.toml")
    _git(root, "commit", "-qm", "add environment manifest")
    worktree = tmp_path / "worker"
    _git(root, "worktree", "add", "-q", str(worktree), "-b", "agenthicc/test")
    trust = EnvironmentTrustStore(tmp_path / "trust.json")
    from agenthicc.worktrees.environment import load_environment_manifest as load_manifest

    main_manifest = load_manifest(root)
    worker_manifest = load_manifest(worktree)
    assert main_manifest is not None and worker_manifest is not None
    trust.trust(main_manifest)

    assert trust.is_trusted(worker_manifest)


def test_action_redacts_declared_secret_and_persists_no_command_output(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    secret = "env-value-that-must-not-escape"
    _manifest(
        root,
        "[actions.print_token]\n"
        + _command_line("printenv TEST_TOKEN")
        + 'env = ["TEST_TOKEN"]\nsecret_env = ["TEST_TOKEN"]\n',
    )
    os.environ["TEST_TOKEN"] = secret
    try:
        receipt = WorktreeEnvironment(root, "wt-secret").run_action("print_token", approved=True)
    finally:
        os.environ.pop("TEST_TOKEN", None)

    assert receipt.result == "succeeded"
    command_result = receipt.commands[0]
    assert "<redacted>" in command_result.stdout
    assert secret not in receipt.to_json()
    assert "printenv TEST_TOKEN" not in receipt.to_json()


def test_redaction_includes_short_configured_secret_values() -> None:
    from agenthicc.worktrees.environment import _redact

    assert _redact("token=x", ("x",)) == "token=<redacted>"


def test_command_output_is_capped_and_timeout_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        "[actions.large]\n" + _command_line("yes x | head -c 1000") + "timeout_s = 3\n",
    )
    env = WorktreeEnvironment(root, "wt-output", output_limit=64)
    manifest = env.load()
    assert manifest is not None
    output_result = env.run_action("large", approved=True).commands[0]
    assert output_result.truncated
    assert len(output_result.stdout.encode()) <= 64

    _manifest(
        root,
        "[actions.slow]\n" + _command_line("sleep 5") + "timeout_s = 0.1\n",
    )
    timeout_result = (
        WorktreeEnvironment(root, "wt-timeout").run_action("slow", approved=True).commands[0]
    )
    assert timeout_result.result == "timed_out"
    assert timeout_result.exit_code is not None


def test_cancelling_action_terminates_its_process_group(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        "[actions.cancel]\n" + _command_line("sleep 20") + "timeout_s = 30\n",
    )
    cancel_event = threading.Event()
    threading.Timer(0.1, cancel_event.set).start()

    receipt = WorktreeEnvironment(root, "wt-cancel").run_action(
        "cancel",
        approved=True,
        cancel_event=cancel_event,
    )

    assert receipt.result == "cancelled"
    assert receipt.commands[0].result == "cancelled"


def test_successful_action_does_not_leave_background_processes_running(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        '[actions.background]\ncommand = "sleep 30 & echo $! > child.pid"\n',
    )

    receipt = WorktreeEnvironment(root, "wt-background-child").run_action(
        "background",
        approved=True,
    )

    assert receipt.result == "succeeded"
    child_pid = int((root / "child.pid").read_text(encoding="utf-8"))
    proc_stat = Path(f"/proc/{child_pid}/stat")
    deadline = time.monotonic() + 2
    while proc_stat.exists() and time.monotonic() < deadline:
        state = proc_stat.read_text(encoding="utf-8").split()[2]
        if state == "Z":
            break
        time.sleep(0.025)
    assert not proc_stat.exists() or proc_stat.read_text(encoding="utf-8").split()[2] == "Z"


def test_action_cannot_write_outside_its_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "coordinator-checkout" / "marker.txt"
    outside.parent.mkdir()
    _manifest(
        root,
        "[actions.escape]\n" + _command_line(f"printf escaped > {shlex.quote(str(outside))}"),
    )

    receipt = WorktreeEnvironment(root, "wt-isolation").run_action(
        "escape",
        approved=True,
    )

    assert receipt.result == "failed"
    assert not outside.exists()


def test_immutable_shared_cache_is_mounted_read_only(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    cache_root = tmp_path / "cache-root"
    repo_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]
    cache = cache_root / "shared" / repo_key / "immutable"
    cache.mkdir(parents=True)
    _manifest(
        root,
        "[[caches]]\n"
        'name = "shared"\n'
        'env_var = "SHARED_CACHE"\n'
        'path = "immutable"\n'
        'sharing = "immutable_shared"\n'
        "[actions.write_cache]\n" + _command_line('printf bad > "$SHARED_CACHE/unexpected"'),
    )

    receipt = WorktreeEnvironment(
        root,
        "wt-immutable-cache",
        cache_root=cache_root,
    ).run_action("write_cache", approved=True)

    assert receipt.result == "failed"
    assert not (cache / "unexpected").exists()


def test_none_declared_network_action_cannot_reach_host_loopback(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    # The sandbox shell is bash, whose /dev/tcp opens a connection. The control
    # action shares the host network and must connect, so "blocked" from the
    # isolated action can only come from the network isolation.
    probe = (
        f"if (exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null; "
        "then echo connected; else echo blocked; fi"
    )
    _manifest(
        root,
        "[actions.network_check]\n"
        + _command_line(probe)
        + 'network = "none_declared"\n'
        + "timeout_s = 2\n"
        + "[actions.network_control]\n"
        + _command_line(probe)
        + "timeout_s = 2\n",
    )
    environment = WorktreeEnvironment(root, "wt-no-network")
    try:
        isolated = environment.run_action("network_check", approved=True)
        control = environment.run_action("network_control", approved=True)
    finally:
        listener.close()

    assert control.result == "succeeded"
    assert "connected" in control.commands[0].stdout
    assert isolated.result == "succeeded"
    assert "blocked" in isolated.commands[0].stdout


def test_manifest_rejects_worktree_relative_cwd_symlink_escape(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    _manifest(
        root,
        "[actions.bad]\ncommand = 'true'\ncwd = 'escape'\n",
    )

    with pytest.raises(EnvironmentManifestError, match="escapes the worktree"):
        WorktreeEnvironment(root, "wt-path").run_action("bad", approved=True)


def test_manifest_rejects_expected_output_symlink_escape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    _manifest(
        root,
        "[actions.bad]\ncommand = 'true'\nexpected_outputs = ['escape/result']\n",
    )

    with pytest.raises(EnvironmentManifestError, match="escapes the worktree"):
        load_environment_manifest(root)


def test_mutable_shared_cache_requires_and_uses_exclusive_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        "[[caches]]\n"
        'name = "build"\n'
        'env_var = "BUILD_CACHE"\n'
        'path = "build"\n'
        'sharing = "mutable_shared"\n'
        'isolation_strategy = "exclusive_lock"\n'
        "[actions.test]\ncommand = 'true'\n",
    )
    environment = WorktreeEnvironment(root, "wt-cache", cache_root=tmp_path / "cache")
    manifest = environment.load()
    assert manifest is not None
    values, locks = environment._cache_values(manifest)
    assert values["BUILD_CACHE"].endswith("/build")
    assert len(locks) == 1
    assert locks[0].name.endswith(".agenthicc.lock")


def test_cache_must_not_be_inside_or_around_the_worker_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _manifest(
        root,
        "[[caches]]\n"
        'name = "build"\n'
        'env_var = "BUILD_CACHE"\n'
        'path = "build"\n'
        'sharing = "isolated"\n',
    )
    manifest = load_environment_manifest(root)
    assert manifest is not None

    environment = WorktreeEnvironment(
        root,
        "wt-cache-overlap",
        cache_root=root / ".agenthicc" / "cache",
    )
    with pytest.raises(EnvironmentManifestError, match="must not overlap the worker worktree"):
        environment._cache_values(manifest)
